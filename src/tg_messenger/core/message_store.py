"""Persistent message cache above the Telegram client.

The store owns SQLite persistence and sync watermarks; ``client.py`` stays a
thin network wrapper. History sync is incremental and deliberately separate from
the client's short TTL cache so DB contiguity is not built from stale pages.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from tg_messenger.core.client import is_channel_or_megagroup_id
from tg_messenger.core.models import MediaRef, Message, MessagesDeletedEvent, User

logger = logging.getLogger(__name__)

MESSAGE_STORE_MIGRATIONS = [
    "CREATE TABLE messages ("
    " dialog_id INTEGER NOT NULL,"
    " id INTEGER NOT NULL,"
    " sender_id INTEGER NOT NULL DEFAULT 0,"
    " out INTEGER NOT NULL DEFAULT 0,"
    " date TEXT NOT NULL,"
    " text TEXT,"
    " media TEXT,"
    " reply_to_id INTEGER,"
    " is_forward INTEGER NOT NULL DEFAULT 0,"
    " translated_text TEXT,"
    " translated_lang TEXT,"
    " PRIMARY KEY (dialog_id, id))",
    "CREATE TABLE message_sync ("
    " dialog_id INTEGER PRIMARY KEY,"
    " low_id INTEGER NOT NULL,"
    " high_id INTEGER NOT NULL)",
    # #108: author enrichment, so a store-backed group history (the default serve/tui path)
    # shows 'userid @username First Last', not just the bare id. Nullable, backward-compatible.
    "ALTER TABLE messages ADD COLUMN sender_username TEXT",
    "ALTER TABLE messages ADD COLUMN sender_first_name TEXT",
    "ALTER TABLE messages ADD COLUMN sender_last_name TEXT",
]
MESSAGE_STORE_PEER_STATE_MAX = 1000


def register_message_store_migrations(storage) -> None:
    """Register message-cache tables on ``storage`` before ``connect()``."""
    storage.register_migrations(MESSAGE_STORE_MIGRATIONS)


class MessageStore:
    """SQLite-backed recent-history cache with incremental Telegram sync."""

    def __init__(
        self,
        *,
        client,
        storage,
        sync_ttl: float = 15.0,
        clock=time.monotonic,
    ):
        self._client = client
        self._storage = storage
        self._sync_ttl = float(sync_ttl)
        self._clock = clock
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._sync_locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
        self._last_sync: OrderedDict[int, float] = OrderedDict()

    @property
    def storage(self):
        return self._storage

    async def connect(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if not self._connected:
                await self._storage.connect()
                self._connected = True

    async def close(self) -> None:
        async with self._connect_lock:
            if self._connected:
                await self._storage.close()
                self._connected = False

    async def history(self, peer: int, limit: int = 50) -> list[Message]:
        """Sync newer messages when stale, then serve the contiguous DB window."""
        await self.connect()
        peer = int(peer)
        limit = int(limit)
        lock = self._peer_lock(peer)
        async with lock:
            row = await self._sync_row(peer)
            if (
                row is not None
                and int(row[0]) > 0
                and await self._window_count(peer, int(row[0]), int(row[1])) < limit
            ):
                await self._sync_full_window(peer, limit)
                row = await self._sync_row(peer)
            last = self._last_sync.get(peer)
            if row is None or last is None or self._clock() - last >= self._sync_ttl:
                await self._sync_history(peer, limit, row)
        return await self._load_window(peer, limit)

    async def ingest(self, message: Message) -> None:
        """Persist a live message without advancing the contiguous sync window."""
        await self.connect()
        lock = self._peer_lock(message.dialog_id)
        async with lock:
            await self._upsert_message(message)

    async def apply_deletion(self, event: MessagesDeletedEvent) -> None:
        await self.connect()
        ids = [int(i) for i in event.message_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        if event.chat_id is not None:
            await self._storage.execute(
                f"DELETE FROM messages WHERE dialog_id = ? AND id IN ({placeholders})",
                (int(event.chat_id), *ids),
            )
            return
        rows = await self._storage.fetchall(
            f"SELECT dialog_id, id FROM messages WHERE id IN ({placeholders})",
            tuple(ids),
        )
        for dialog_id, message_id in rows:
            if is_channel_or_megagroup_id(int(dialog_id)):
                continue
            await self._storage.execute(
                "DELETE FROM messages WHERE dialog_id = ? AND id = ?",
                (int(dialog_id), int(message_id)),
            )

    async def run(self) -> None:
        """Drain existing live streams; stop cleanly when a stream is interrupted."""
        await self.connect()
        tasks = {
            asyncio.create_task(self._consume_until_done(self._consume_incoming)): "incoming",
            asyncio.create_task(self._consume_until_done(self._consume_outgoing)): "outgoing",
            asyncio.create_task(self._consume_until_done(self._consume_deleted)): "deleted",
        }
        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    interrupted = task.result()
                    if interrupted:
                        for pending_task in pending:
                            pending_task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return
                tasks = {task: tasks[task] for task in pending}
        finally:
            for task in tasks:
                task.cancel()

    async def _consume_until_done(self, consume_fn) -> bool:
        try:
            await consume_fn()
            return False
        except asyncio.CancelledError:
            logger.info("message store consumer cancelled")
            return True
        except KeyboardInterrupt:
            logger.info("message store interrupted")
            return True
        except Exception:
            logger.exception("message store consumer failed")
            return True

    async def record_outgoing(
        self,
        dialog_id: int,
        message: Message,
        *,
        source_text: str,
        source_lang: str,
    ) -> None:
        """Persist an outgoing translated send with its original user-language draft."""
        await self.connect()
        peer = int(dialog_id)
        msg = message.model_copy(update={"dialog_id": peer})
        lock = self._peer_lock(peer)
        async with lock:
            await self._upsert_message(
                msg,
                translated_text=source_text,
                translated_lang=source_lang,
                preserve_translation=False,
            )

    def _peer_lock(self, peer: int) -> asyncio.Lock:
        peer = int(peer)
        lock = self._sync_locks.get(peer)
        if lock is None:
            lock = asyncio.Lock()
            self._sync_locks[peer] = lock
        self._sync_locks.move_to_end(peer)
        self._evict_idle_peer_state()
        return lock

    def _remember_sync(self, peer: int) -> None:
        peer = int(peer)
        self._last_sync[peer] = self._clock()
        self._last_sync.move_to_end(peer)
        self._evict_idle_peer_state()

    def _evict_idle_peer_state(self) -> None:
        checked = 0
        while len(self._sync_locks) > MESSAGE_STORE_PEER_STATE_MAX and checked < len(self._sync_locks):
            old_peer, old_lock = next(iter(self._sync_locks.items()))
            if old_lock.locked():
                self._sync_locks.move_to_end(old_peer)
                checked += 1
                continue
            self._sync_locks.popitem(last=False)
            self._last_sync.pop(old_peer, None)
            checked = 0
        while len(self._last_sync) > MESSAGE_STORE_PEER_STATE_MAX:
            old_peer, _ = self._last_sync.popitem(last=False)
            if old_peer not in self._sync_locks:
                continue
            lock = self._sync_locks[old_peer]
            if lock.locked():
                self._last_sync[old_peer] = self._clock()
                self._last_sync.move_to_end(old_peer)
                break
            self._sync_locks.pop(old_peer, None)

    async def _consume_incoming(self) -> None:
        async for ev in self._client.listen_all():
            try:
                await self.ingest(ev.message)
            except Exception:
                logger.exception(
                    "message store failed to ingest incoming message in dialog %s",
                    getattr(ev, "dialog_id", "?"),
                )

    async def _consume_outgoing(self) -> None:
        async for ev in self._client.listen_outgoing():
            try:
                await self.ingest(ev.message)
            except Exception:
                logger.exception(
                    "message store failed to ingest outgoing message in dialog %s",
                    getattr(ev, "dialog_id", "?"),
                )

    async def _consume_deleted(self) -> None:
        async for ev in self._client.listen_deleted():
            try:
                await self.apply_deletion(ev)
            except Exception:
                logger.exception("message store failed to apply deletion event")

    async def _sync_history(self, peer: int, limit: int, row: tuple[int, int] | None) -> None:
        high_id = int(row[1]) if row is not None else 0
        fetched = await self._client.history_since(peer, min_id=high_id, limit=limit)
        if fetched:
            if row is None or len(fetched) >= limit:
                await self._replace_window(peer, fetched, limit)
            else:
                await self._sync_full_window(peer, limit)
                return
        elif row is None:
            await self._replace_window(peer, fetched, limit)
        else:
            await self._sync_full_window(peer, limit)
            return
        self._remember_sync(peer)

    async def _sync_full_window(self, peer: int, limit: int) -> None:
        fetched = await self._client.history_since(peer, min_id=0, limit=limit)
        await self._replace_window(peer, fetched, limit)
        self._remember_sync(peer)

    async def _replace_window(self, peer: int, fetched: list[Message], limit: int) -> tuple[int, int]:
        if not fetched:
            await self._storage.execute("DELETE FROM messages WHERE dialog_id = ?", (int(peer),))
            await self._set_sync_row(peer, 0, 0)
            return 0, 0
        for message in fetched:
            await self._upsert_message(message)
        ids = [int(m.id) for m in fetched]
        low, high = (0 if len(fetched) < limit else min(ids)), max(ids)
        await self._prune_window(peer, low, high, ids)
        await self._set_sync_row(peer, low, high)
        return low, high

    async def _prune_window(self, peer: int, low_id: int, high_id: int, ids: list[int]) -> None:
        placeholders = ",".join("?" for _ in ids)
        await self._storage.execute(
            f"DELETE FROM messages WHERE dialog_id = ? AND id >= ? AND id <= ? AND id NOT IN ({placeholders})",
            (int(peer), int(low_id), int(high_id), *ids),
        )

    async def _window_count(self, peer: int, low_id: int, high_id: int) -> int:
        row = await self._storage.fetchone(
            "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND id >= ? AND id <= ?",
            (int(peer), int(low_id), int(high_id)),
        )
        return int(row[0]) if row is not None else 0

    async def _sync_row(self, peer: int) -> tuple[int, int] | None:
        row = await self._storage.fetchone(
            "SELECT low_id, high_id FROM message_sync WHERE dialog_id = ?",
            (int(peer),),
        )
        if row is None:
            return None
        return int(row[0]), int(row[1])

    async def _set_sync_row(self, peer: int, low_id: int, high_id: int) -> None:
        await self._storage.execute(
            "INSERT INTO message_sync (dialog_id, low_id, high_id) VALUES (?, ?, ?) "
            "ON CONFLICT(dialog_id) DO UPDATE SET low_id = excluded.low_id, high_id = excluded.high_id",
            (int(peer), int(low_id), int(high_id)),
        )

    async def _load_window(self, peer: int, limit: int) -> list[Message]:
        row = await self._sync_row(peer)
        if row is None:
            return []
        low_id, high_id = int(row[0]), int(row[1])
        rows = await self._storage.fetchall(
            "SELECT dialog_id, id, sender_id, out, date, text, media, reply_to_id, "
            "is_forward, translated_text, sender_username, sender_first_name, sender_last_name "
            "FROM messages WHERE dialog_id = ? AND id >= ? AND id <= ? ORDER BY id DESC LIMIT ?",
            (int(peer), low_id, high_id, int(limit)),
        )
        return [self._row_to_message(r) for r in reversed(rows)]

    async def _upsert_message(
        self,
        message: Message,
        *,
        translated_text: str | None = None,
        translated_lang: str | None = None,
        preserve_translation: bool = True,
    ) -> None:
        media = message.media.model_dump_json() if message.media is not None else None
        text = translated_text if translated_text is not None else message.translated_text
        lang = translated_lang
        update_translation = (
            "translated_text = CASE "
            "WHEN messages.text IS excluded.text "
            "THEN COALESCE(messages.translated_text, excluded.translated_text) "
            "ELSE excluded.translated_text END, "
            "translated_lang = CASE "
            "WHEN messages.text IS excluded.text "
            "THEN COALESCE(messages.translated_lang, excluded.translated_lang) "
            "ELSE excluded.translated_lang END"
            if preserve_translation
            else "translated_text = excluded.translated_text, translated_lang = excluded.translated_lang"
        )
        s = message.sender
        await self._storage.execute(
            "INSERT INTO messages (dialog_id, id, sender_id, out, date, text, media, "
            "reply_to_id, is_forward, translated_text, translated_lang, "
            "sender_username, sender_first_name, sender_last_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dialog_id, id) DO UPDATE SET "
            "sender_id = excluded.sender_id, out = excluded.out, date = excluded.date, "
            "text = excluded.text, media = excluded.media, reply_to_id = excluded.reply_to_id, "
            # #108 (Codex review): preserve a resolved author across a later sender=None
            # re-upsert. raw.sender is best-effort — a cold-session/evicted re-sync of the same
            # message can arrive with sender=None, and an unconditional overwrite would NULL the
            # stored "@username First Last" back to a bare id. COALESCE keeps the existing value
            # when the incoming one is NULL (mirrors the translated_text preserve pattern above);
            # a genuinely changed author (non-NULL incoming) still writes through.
            "is_forward = excluded.is_forward, "
            "sender_username = COALESCE(excluded.sender_username, messages.sender_username), "
            "sender_first_name = COALESCE(excluded.sender_first_name, messages.sender_first_name), "
            f"sender_last_name = COALESCE(excluded.sender_last_name, messages.sender_last_name), {update_translation}",
            (
                int(message.dialog_id),
                int(message.id),
                int(message.sender_id),
                1 if message.out else 0,
                message.date.isoformat(),
                message.text,
                media,
                int(message.reply_to_id) if message.reply_to_id is not None else None,
                1 if message.is_forward else 0,
                text,
                lang,
                s.username if s is not None else None,
                s.first_name if s is not None else None,
                s.last_name if s is not None else None,
            ),
        )

    @staticmethod
    def _row_to_message(row: tuple[Any, ...]) -> Message:
        media = MediaRef.model_validate_json(row[6]) if row[6] is not None else None
        # #108: reconstruct the author when any sender field was persisted; sender_id is always
        # available, so a row with no username/name still yields a User carrying the id.
        username, first_name, last_name = row[10], row[11], row[12]
        sender = None
        if username is not None or first_name is not None or last_name is not None:
            sender = User(id=int(row[2]), username=username,
                          first_name=first_name, last_name=last_name)
        return Message(
            dialog_id=int(row[0]),
            id=int(row[1]),
            sender_id=int(row[2]),
            out=bool(row[3]),
            date=datetime.fromisoformat(row[4]),
            text=row[5],
            media=media,
            reply_to_id=int(row[7]) if row[7] is not None else None,
            is_forward=bool(row[8]),
            translated_text=row[9],
            sender=sender,
        )


async def upsert_message_for_translation(storage, message: Message) -> None:
    """Ensure a live message row exists before caching translation metadata."""
    media = message.media.model_dump_json() if message.media is not None else None
    s = message.sender
    await storage.execute(
        "INSERT INTO messages (dialog_id, id, sender_id, out, date, text, media, "
        "reply_to_id, is_forward, sender_username, sender_first_name, sender_last_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dialog_id, id) DO UPDATE SET "
        "sender_id = excluded.sender_id, out = excluded.out, date = excluded.date, "
        "text = excluded.text, media = excluded.media, reply_to_id = excluded.reply_to_id, "
        # #108 (Codex review): COALESCE so a sender=None translation upsert never NULLs a
        # previously-resolved author (mirrors _upsert_message; raw.sender is best-effort).
        "is_forward = excluded.is_forward, "
        "sender_username = COALESCE(excluded.sender_username, messages.sender_username), "
        "sender_first_name = COALESCE(excluded.sender_first_name, messages.sender_first_name), "
        "sender_last_name = COALESCE(excluded.sender_last_name, messages.sender_last_name), "
        "translated_text = CASE WHEN messages.text IS excluded.text THEN messages.translated_text ELSE NULL END, "
        "translated_lang = CASE WHEN messages.text IS excluded.text THEN messages.translated_lang ELSE NULL END",
        (
            int(message.dialog_id),
            int(message.id),
            int(message.sender_id),
            1 if message.out else 0,
            message.date.isoformat(),
            message.text,
            media,
            int(message.reply_to_id) if message.reply_to_id is not None else None,
            1 if message.is_forward else 0,
            s.username if s is not None else None,
            s.first_name if s is not None else None,
            s.last_name if s is not None else None,
        ),
    )


async def set_message_translation(
    storage,
    dialog_id: int,
    message_id: int,
    *,
    lang: str,
    text: str | None,
) -> None:
    await storage.execute(
        "UPDATE messages SET translated_text = ?, translated_lang = ? "
        "WHERE dialog_id = ? AND id = ?",
        (text, lang, int(dialog_id), int(message_id)),
    )


async def get_message_translation(
    storage,
    dialog_id: int,
    message_id: int,
    lang: str,
    *,
    source_text: str | None = None,
):
    row = await storage.fetchone(
        "SELECT text, translated_text, translated_lang FROM messages WHERE dialog_id = ? AND id = ?",
        (int(dialog_id), int(message_id)),
    )
    if row is None or row[2] != lang or (source_text is not None and row[0] != source_text):
        return None
    return {"text": row[1], "lang": row[2]}
