"""Persistent message cache above the Telegram client.

The store owns SQLite persistence and sync watermarks; ``client.py`` stays a
thin network wrapper. History sync is incremental and deliberately separate from
the client's short TTL cache so DB contiguity is not built from stale pages.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from tg_messenger.core.client import is_channel_or_megagroup_id
from tg_messenger.core.models import MediaRef, Message, MessagesDeletedEvent

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
]


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
        self._sync_locks: dict[int, asyncio.Lock] = {}
        self._last_sync: dict[int, float] = {}

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
        if self._connected:
            await self._storage.close()
            self._connected = False

    async def history(self, peer: int, limit: int = 50) -> list[Message]:
        """Sync newer messages when stale, then serve the contiguous DB window."""
        await self.connect()
        peer = int(peer)
        limit = int(limit)
        lock = self._sync_locks.setdefault(peer, asyncio.Lock())
        async with lock:
            row = await self._sync_row(peer)
            if row is not None and int(row[0]) > 0 and await self._window_count(peer, int(row[0])) < limit:
                await self._sync_full_window(peer, limit)
                row = await self._sync_row(peer)
            last = self._last_sync.get(peer)
            if row is None or last is None or self._clock() - last >= self._sync_ttl:
                await self._sync_history(peer, limit, row)
        return await self._load_window(peer, limit)

    async def ingest(self, message: Message) -> None:
        """Persist a live message without advancing the contiguous sync window."""
        await self.connect()
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
        """Drain existing live streams; gather keeps Ctrl+C behavior simple."""
        await self.connect()
        await asyncio.gather(
            self._consume_incoming(),
            self._consume_outgoing(),
            self._consume_deleted(),
        )

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
        msg = message.model_copy(update={"dialog_id": int(dialog_id)})
        await self._upsert_message(
            msg,
            translated_text=source_text,
            translated_lang=source_lang,
            preserve_translation=False,
        )

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
            for message in fetched:
                await self._upsert_message(message)
            ids = [int(m.id) for m in fetched]
            if row is None or len(fetched) >= limit:
                low, high = (0 if len(fetched) < limit else min(ids)), max(ids)
            else:
                low, high = int(row[0]), max(high_id, max(ids))
            await self._set_sync_row(peer, low, high)
        elif row is None:
            await self._set_sync_row(peer, 0, 0)
        self._last_sync[peer] = self._clock()

    async def _sync_full_window(self, peer: int, limit: int) -> None:
        fetched = await self._client.history_since(peer, min_id=0, limit=limit)
        if not fetched:
            await self._set_sync_row(peer, 0, 0)
            self._last_sync[peer] = self._clock()
            return
        for message in fetched:
            await self._upsert_message(message)
        ids = [int(m.id) for m in fetched]
        await self._set_sync_row(peer, 0 if len(fetched) < limit else min(ids), max(ids))
        self._last_sync[peer] = self._clock()

    async def _window_count(self, peer: int, low_id: int) -> int:
        row = await self._storage.fetchone(
            "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND id >= ?",
            (int(peer), int(low_id)),
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
        low_id = int(row[0])
        rows = await self._storage.fetchall(
            "SELECT dialog_id, id, sender_id, out, date, text, media, reply_to_id, "
            "is_forward, translated_text "
            "FROM messages WHERE dialog_id = ? AND id >= ? ORDER BY id DESC LIMIT ?",
            (int(peer), low_id, int(limit)),
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
            "translated_text = COALESCE(messages.translated_text, excluded.translated_text), "
            "translated_lang = COALESCE(messages.translated_lang, excluded.translated_lang)"
            if preserve_translation
            else "translated_text = excluded.translated_text, translated_lang = excluded.translated_lang"
        )
        await self._storage.execute(
            "INSERT INTO messages (dialog_id, id, sender_id, out, date, text, media, "
            "reply_to_id, is_forward, translated_text, translated_lang) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dialog_id, id) DO UPDATE SET "
            "sender_id = excluded.sender_id, out = excluded.out, date = excluded.date, "
            "text = excluded.text, media = excluded.media, reply_to_id = excluded.reply_to_id, "
            f"is_forward = excluded.is_forward, {update_translation}",
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
            ),
        )

    @staticmethod
    def _row_to_message(row: tuple[Any, ...]) -> Message:
        media = MediaRef.model_validate_json(row[6]) if row[6] is not None else None
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
        )


async def upsert_message_for_translation(storage, message: Message) -> None:
    """Ensure a live message row exists before caching translation metadata."""
    media = message.media.model_dump_json() if message.media is not None else None
    await storage.execute(
        "INSERT INTO messages (dialog_id, id, sender_id, out, date, text, media, "
        "reply_to_id, is_forward) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dialog_id, id) DO UPDATE SET "
        "sender_id = excluded.sender_id, out = excluded.out, date = excluded.date, "
        "text = excluded.text, media = excluded.media, reply_to_id = excluded.reply_to_id, "
        "is_forward = excluded.is_forward",
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


async def get_message_translation(storage, dialog_id: int, message_id: int, lang: str):
    row = await storage.fetchone(
        "SELECT translated_text, translated_lang FROM messages WHERE dialog_id = ? AND id = ?",
        (int(dialog_id), int(message_id)),
    )
    if row is None or row[1] != lang:
        return None
    return {"text": row[0], "lang": row[1]}
