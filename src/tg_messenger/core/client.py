"""StandaloneTelegramClient — the UI-agnostic core.

Thin async wrapper over a single Telethon client: dialogs (DM-only), history,
send, media, plus a single NewMessage handler fanned out through EventBus.
All network calls route through the vendored flood-wait retry.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from tg_messenger.core.auth import DEFAULT_SESSION_DIR, SessionStore
from tg_messenger.core.events import EventBus
from tg_messenger.core.flood import run_with_flood_wait_retry
from tg_messenger.core.models import Dialog, IncomingEvent, MediaRef, Message

logger = logging.getLogger(__name__)


def _default_factory(session, api_id, api_hash):
    return TelegramClient(session, api_id, api_hash)


def _is_dm_entity(entity) -> bool:
    """DM = a User (has first/last name), not a channel/chat (has title), not a bot."""
    if getattr(entity, "bot", False):
        return False
    if getattr(entity, "title", None) is not None:
        return False
    return hasattr(entity, "first_name") or hasattr(entity, "last_name")


def _entity_title(entity) -> str:
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or getattr(entity, "username", None) or str(getattr(entity, "id", ""))


class StandaloneTelegramClient:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        *,
        session_name: str = "default",
        external_session: str | None = None,
        session_dir: Path | str = DEFAULT_SESSION_DIR,
        client_factory: Callable = _default_factory,
    ):
        self._store = SessionStore(session_dir)
        if external_session is not None:
            session_string = self._store.from_external(external_session)
        else:
            session_string = self._store.load(session_name)
        self._session_name = session_name
        self._client = client_factory(StringSession(session_string or None), api_id, api_hash)
        self._bus = EventBus()
        self._handler_registered = False

    # --- connection ---
    async def connect(self) -> None:
        await self._client.connect()
        self._ensure_handler()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def is_authorized(self) -> bool:
        return await self._client.is_user_authorized()

    def save_session(self) -> None:
        self._store.save(self._session_name, self._client.session.save())

    # --- dialogs / history ---
    async def dialogs(self, dm_only: bool = True) -> list[Dialog]:
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_dialogs(), operation="dialogs"
        )
        result = []
        for d in raw:
            entity = d.entity
            if dm_only and not _is_dm_entity(entity):
                continue
            last_msg = getattr(d, "message", None)
            result.append(
                Dialog(
                    id=entity.id,
                    title=_entity_title(entity),
                    username=getattr(entity, "username", None),
                    unread=getattr(d, "unread_count", 0) or 0,
                    last_text=getattr(last_msg, "text", None),
                    last_message_at=getattr(last_msg, "date", None),
                )
            )
        return result

    async def _collect_dialogs(self) -> list:
        return [d async for d in self._client.iter_dialogs()]

    async def history(self, peer: int, limit: int = 50, offset_id: int = 0) -> list[Message]:
        """Return messages in chronological order (oldest first).

        Telethon yields newest-first; reversed here so UIs can render top-down
        and append live messages at the bottom.
        """
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_history(peer, limit, offset_id), operation="history"
        )
        return [self._to_message(m, dialog_id=int(peer)) for m in reversed(raw)]

    async def _collect_history(self, peer, limit, offset_id) -> list:
        return [m async for m in self._client.iter_messages(peer, limit=limit, offset_id=offset_id)]

    # --- sending ---
    async def send_text(self, peer: int, text: str) -> Message:
        msg = await run_with_flood_wait_retry(
            lambda: self._client.send_message(peer, text), operation="send_text"
        )
        return self._to_message(msg, dialog_id=int(peer))

    async def send_media(self, peer: int, file_path: str | Path, caption: str | None = None) -> Message:
        msg = await run_with_flood_wait_retry(
            lambda: self._client.send_file(peer, str(file_path), caption=caption),
            operation="send_media",
        )
        return self._to_message(msg, dialog_id=int(peer))

    async def download_media(self, message, dest: str | Path) -> str:
        return await run_with_flood_wait_retry(
            lambda: self._client.download_media(message, str(dest)), operation="download_media"
        )

    async def download_message_media(self, peer: int, message_id: int, dest: str | Path) -> str | None:
        """Fetch the raw message by id and download its media to ``dest``.

        Returns the saved path, or ``None`` if the message carries no media.
        """
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_history_ids(peer, message_id), operation="download_message_media"
        )
        if not raw or getattr(raw[0], "media", None) is None:
            return None
        return await self.download_media(raw[0], dest)

    async def _collect_history_ids(self, peer, message_id) -> list:
        return [m async for m in self._client.iter_messages(peer, ids=message_id)]

    # --- realtime ---
    def _ensure_handler(self) -> None:
        if self._handler_registered:
            return
        self._client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        self._handler_registered = True

    async def _on_new_message(self, event) -> None:
        # DM-only product: drop group/channel traffic at the source
        if not getattr(event, "is_private", True):
            return
        try:
            dialog_id = int(getattr(event, "chat_id", 0) or 0)
            message = self._to_message(event.message, dialog_id=dialog_id)
            self._bus.publish(IncomingEvent(dialog_id=dialog_id, message=message))
        except Exception:
            # don't depend on Telethon's logger config; record it ourselves
            logger.exception("failed to handle incoming message")

    async def listen(self) -> AsyncIterator[IncomingEvent]:
        async for ev in self._bus.subscribe():
            yield ev

    # --- mapping ---
    @staticmethod
    def _to_media_ref(raw) -> MediaRef | None:
        if getattr(raw, "media", None) is None:
            return None
        if getattr(raw, "photo", None) is not None:
            kind = "photo"
        elif getattr(raw, "document", None) is not None:
            kind = "document"
        else:
            kind = "other"
        file = getattr(raw, "file", None)
        return MediaRef(
            kind=kind,
            file_name=getattr(file, "file_name", None) or getattr(file, "name", None),
            size=getattr(file, "size", None),
            downloadable=True,
        )

    @staticmethod
    def _to_message(raw, *, dialog_id: int) -> Message:
        media = StandaloneTelegramClient._to_media_ref(raw)
        return Message(
            id=getattr(raw, "id", 0),
            dialog_id=dialog_id,
            sender_id=getattr(raw, "sender_id", 0) or 0,
            out=bool(getattr(raw, "out", False)),
            date=raw.date,
            text=getattr(raw, "text", None) or getattr(raw, "message", None),
            media=media,
        )
