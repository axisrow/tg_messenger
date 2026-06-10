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
from tg_messenger.core.models import (
    Dialog,
    IncomingEvent,
    MediaRef,
    Message,
    MessagesDeletedEvent,
    OutgoingEvent,
    User,
)

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
    title = getattr(entity, "title", None)  # groups/channels carry a title, users don't
    if title:
        return title
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or getattr(entity, "username", None) or str(getattr(entity, "id", ""))


class _SafeChatAction:
    """Telethon chat-action CM that logs its own failures instead of raising.

    The indicator is a UX nicety — entering/exiting must never break the
    caller's work. The caller's own exceptions still propagate.
    """

    def __init__(self, telethon_client, peer: int):
        self._telethon_client = telethon_client
        self._peer = peer
        self._action = None

    async def __aenter__(self):
        try:
            action = self._telethon_client.action(self._peer, "typing")
            await action.__aenter__()
            self._action = action
        except Exception:
            logger.warning("typing action failed for dialog %s — continuing without it",
                           self._peer, exc_info=True)
        return self

    async def __aexit__(self, *exc):
        if self._action is not None:
            try:
                await self._action.__aexit__(*exc)
            except Exception:
                logger.warning("typing action cleanup failed for dialog %s",
                               self._peer, exc_info=True)
        return False


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
        self._bus: EventBus[IncomingEvent] = EventBus()
        self._bus_out: EventBus[OutgoingEvent] = EventBus()
        self._bus_deleted: EventBus[MessagesDeletedEvent] = EventBus()
        self._handler_registered = False

    # --- connection ---
    async def connect(self) -> None:
        await run_with_flood_wait_retry(lambda: self._client.connect(), operation="connect")
        self._ensure_handler()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def is_authorized(self) -> bool:
        return await run_with_flood_wait_retry(lambda: self._client.is_user_authorized(), operation="is_authorized")

    def save_session(self) -> None:
        self._store.save(self._session_name, self._client.session.save())

    async def get_me(self) -> User:
        raw = await run_with_flood_wait_retry(lambda: self._client.get_me(), operation="get_me")
        return User(
            id=getattr(raw, "id", 0),
            first_name=getattr(raw, "first_name", None),
            last_name=getattr(raw, "last_name", None),
            username=getattr(raw, "username", None),
        )

    async def entity_title(self, peer: int) -> str:
        """Human-readable name of a user/group/channel (group title wins)."""
        entity = await run_with_flood_wait_retry(
            lambda: self._client.get_entity(int(peer)), operation="entity_title"
        )
        return _entity_title(entity)

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

    def typing(self, peer: int):
        """Async context manager: show the 'typing…' chat action while the body runs.

        Best-effort by contract: the indicator's own failures are logged here and
        never raised, so callers don't need defensive wrappers. No flood-wait
        wrapper — it's a periodic fire-and-forget UX signal, not a data call.
        Exceptions raised by the caller's body propagate normally.
        """
        return _SafeChatAction(self._client, peer)

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
        # own messages from ANY device + deletions — the watch feature's raw streams;
        # publishing into a bus without subscribers is a no-op, so eager is free
        self._client.add_event_handler(self._on_outgoing_message, events.NewMessage(outgoing=True))
        self._client.add_event_handler(self._on_deleted, events.MessageDeleted())
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

    async def _on_outgoing_message(self, event) -> None:
        # no is_private filter: groups are the whole point of the watch feature
        try:
            dialog_id = int(getattr(event, "chat_id", 0) or 0)
            message = self._to_message(event.message, dialog_id=dialog_id)
            self._bus_out.publish(OutgoingEvent(dialog_id=dialog_id, message=message))
        except Exception:
            logger.exception("failed to handle outgoing message")

    async def _on_deleted(self, event) -> None:
        try:
            ids = [int(i) for i in (getattr(event, "deleted_ids", None) or [])]
            chat_id = getattr(event, "chat_id", None)
            self._bus_deleted.publish(
                MessagesDeletedEvent(
                    chat_id=int(chat_id) if chat_id is not None else None, message_ids=ids
                )
            )
        except Exception:
            logger.exception("failed to handle deleted-messages event")

    async def listen(self) -> AsyncIterator[IncomingEvent]:
        async for ev in self._bus.subscribe():
            yield ev

    async def listen_outgoing(self) -> AsyncIterator[OutgoingEvent]:
        async for ev in self._bus_out.subscribe():
            yield ev

    async def listen_deleted(self) -> AsyncIterator[MessagesDeletedEvent]:
        async for ev in self._bus_deleted.subscribe():
            yield ev

    # --- mapping ---
    @staticmethod
    def _to_media_ref(raw) -> MediaRef | None:
        if getattr(raw, "media", None) is None:
            return None
        if getattr(raw, "photo", None) is not None:
            kind = "photo"
        elif getattr(raw, "voice", None) is not None:
            # a voice note IS a document in Telethon — check .voice first
            kind = "voice"
        elif getattr(raw, "document", None) is not None:
            kind = "document"
        else:
            kind = "other"
        file = getattr(raw, "file", None)
        return MediaRef(
            kind=kind,
            file_name=getattr(file, "file_name", None) or getattr(file, "name", None),
            size=getattr(file, "size", None),
            mime_type=getattr(file, "mime_type", None),
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
