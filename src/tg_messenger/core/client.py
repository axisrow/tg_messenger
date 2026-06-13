"""StandaloneTelegramClient — the UI-agnostic core.

Thin async wrapper over a single Telethon client: dialogs (DM-only by default,
``dm_only=False`` for every kind), history, send, media, plus event streams
fanned out through EventBus (``listen`` private-only, ``listen_all`` every chat).
All network calls route through the vendored flood-wait retry.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon import utils as tl_utils
from telethon.errors import (
    ChatAdminRequiredError,
    ChatGuestSendForbiddenError,
    ChatRestrictedError,
    ChatSendGifsForbiddenError,
    ChatSendMediaForbiddenError,
    ChatSendPollForbiddenError,
    ChatSendStickersForbiddenError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameOccupiedError,
    VoiceMessagesForbiddenError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import CheckUsernameRequest, UpdateUsernameRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji, UpdateMessageReactions

from tg_messenger.core.auth import DEFAULT_SESSION_DIR, SessionStore
from tg_messenger.core.cache import TTLCache
from tg_messenger.core.events import EventBus
from tg_messenger.core.flood import run_with_flood_wait_retry
from tg_messenger.core.languages import clean_supported_lang_code
from tg_messenger.core.models import (
    ChatActionEvent,
    Dialog,
    DialogKind,
    IncomingEvent,
    MediaRef,
    Message,
    MessageReadEvent,
    MessagesDeletedEvent,
    OutgoingEvent,
    ReactionEvent,
    User,
)
from tg_messenger.core.ratelimit import TokenBucket

logger = logging.getLogger(__name__)

# read-side anti-flood: a full GetDialogsRequest / iter_messages is expensive and
# floods on rapid repeats (tab switching) — cache them with a short TTL.
DEFAULT_DIALOGS_TTL_SEC = 30.0
DEFAULT_HISTORY_TTL_SEC = 15.0
_DIALOGS_CACHE_KEY = "all"  # non-archived full mapped list, dm_only filters from it
_ARCHIVED_DIALOGS_CACHE_KEY = "archived"
_CHANNEL_ID_THRESHOLD = -1_000_000_000_000


class MessageDeleteValidationError(ValueError):
    """Raised before a delete call when Telegram could delete outside the intended peer."""


class SendForbiddenError(ValueError):
    """Raised by the send wrappers when Telegram rejects a write on a rights basis
    (read-only channel, banned/restricted member, admin required).

    A ``ValueError`` subclass so the UIs map ONE error type to a clean "you can't
    post here" message — mirroring ``MessageDeleteValidationError``. The ``can_send``
    dialog flag pre-disables the composer; this is the authoritative TOCTOU net when
    the (cached) flag is stale. Slow-mode is NOT folded in here — it's transient.
    """


# The single user-facing read-only message, shared by every UI (Russian per the
# project's user-communication language). Avoids drift across cli/tui/web.
READ_ONLY_MESSAGE = "Сюда писать нельзя — чат только для чтения."


# Telegram rights-rejection errors reclassified into SendForbiddenError at the send seam.
# SlowModeWaitError is deliberately EXCLUDED — it's a transient wait, not a read-only
# state; folding it in would mislabel a perfectly writable slow-mode chat as read-only.
_SEND_FORBIDDEN_ERRORS = (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    ChatSendMediaForbiddenError,
    UserBannedInChannelError,
    ChatGuestSendForbiddenError,
    ChatRestrictedError,
    ChatSendGifsForbiddenError,
    ChatSendStickersForbiddenError,
    ChatSendPollForbiddenError,
    VoiceMessagesForbiddenError,
)


def is_channel_or_megagroup_id(peer: int) -> bool:
    """Telethon marks channels/supergroups as ``-(10^12 + channel_id)``."""
    return int(peer) <= _CHANNEL_ID_THRESHOLD


def _message_dialog_id(raw) -> int | None:
    chat_id = getattr(raw, "chat_id", None)
    if chat_id is not None:
        return int(chat_id)
    peer = getattr(raw, "peer_id", None)
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    return int(tl_utils.get_peer_id(peer))


def _default_factory(session, api_id, api_hash):
    # flood_sleep_threshold=0: Telethon never sleeps silently on a FloodWait —
    # every wait surfaces as an exception routed through run_with_flood_wait_retry.
    return TelegramClient(session, api_id, api_hash, flood_sleep_threshold=0)


def _dialog_kind(entity) -> DialogKind:
    """Classify a Telethon entity: bot beats User; title means group/channel."""
    if getattr(entity, "bot", False):
        return "bot"
    if getattr(entity, "title", None) is not None:
        # Chat (small group) has no broadcast attr; Channel: megagroup vs broadcast
        return "channel" if getattr(entity, "broadcast", False) else "group"
    if hasattr(entity, "first_name") or hasattr(entity, "last_name"):
        return "dm"
    return "group"  # unknown entity — fail-safe NOT a DM (keeps the old filter's semantics)


def _entity_can_send(entity) -> bool:
    """Whether OUR account may post in this entity, computed PURELY from the
    already-fetched dialog entity (zero extra network calls — rides the dialogs TTL
    cache). Fail-safe: any unknown/missing shape returns True so a writable chat is
    never wrongly locked; only positively-detected restrictions return False.

    NOT a slow-mode check — slow-mode is transient and surfaces at send time as
    SendForbiddenError, never as a permanent read-only state here.
    """
    # users/bots (no title) are always writable
    if getattr(entity, "title", None) is None:
        return True
    # we left the chat/channel — can't post
    if getattr(entity, "left", False):
        return False
    # creator and any admin may post — checked BEFORE banned/broadcast, since an admin
    # posts despite a chat-wide restriction. In a broadcast, an admin needs post_messages.
    if getattr(entity, "creator", False):
        return True
    admin = getattr(entity, "admin_rights", None)
    if admin is not None:
        if getattr(entity, "broadcast", False):
            return bool(getattr(admin, "post_messages", True))
        return True
    # personal restriction on us (per-user ChatBannedRights) — True flag == forbidden.
    # Checked AFTER admin: an admin posts regardless (Telegram doesn't restrict admins
    # this way), so admin-wins precedence is deliberate.
    banned = getattr(entity, "banned_rights", None)
    if banned is not None and getattr(banned, "send_messages", False):
        return False
    # broadcast channel: a plain subscriber (no creator/admin) is read-only
    if getattr(entity, "broadcast", False):
        return False
    # group / megagroup: default_banned_rights.send_messages True == everyone restricted
    default_banned = getattr(entity, "default_banned_rights", None)
    if default_banned is not None and getattr(default_banned, "send_messages", False):
        return False
    return True


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
        encryption_key: str | None = None,
        client_factory: Callable = _default_factory,
        dialogs_ttl: float = DEFAULT_DIALOGS_TTL_SEC,
        history_ttl: float = DEFAULT_HISTORY_TTL_SEC,
        send_rate_per_min: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._store = SessionStore(session_dir, encryption_key=encryption_key)
        if external_session is not None:
            session_string = self._store.from_external(external_session)
        else:
            session_string = self._store.load(session_name)
        self._session_name = session_name
        self._client = client_factory(StringSession(session_string or None), api_id, api_hash)
        self._bus: EventBus[IncomingEvent] = EventBus()
        self._bus_all: EventBus[IncomingEvent] = EventBus()
        self._bus_out: EventBus[OutgoingEvent] = EventBus()
        self._bus_deleted: EventBus[MessagesDeletedEvent] = EventBus()
        # #14 event streams: chat actions / read receipts / reactions — lazy buses,
        # same fan-out pattern; publishing without subscribers is a no-op.
        self._bus_chat_actions: EventBus[ChatActionEvent] = EventBus()
        self._bus_reads: EventBus[MessageReadEvent] = EventBus()
        self._bus_reactions: EventBus[ReactionEvent] = EventBus()
        self._handler_registered = False
        # one non-archived full list + one archived full list; dm_only filters from it — see dialogs()
        self._dialogs_cache: TTLCache[str, list[Dialog]] = TTLCache(
            dialogs_ttl, maxsize=2, clock=clock
        )
        # keyed by (int(peer), limit, offset_id); invalidated per-peer on writes/events
        self._history_cache: TTLCache[tuple[int, int, int], list[Message]] = TTLCache(
            history_ttl, maxsize=64, clock=clock
        )
        # one global cap on outgoing sends across every caller (#25); 0 explicitly disables.
        # burst = the rate (1 minute's worth) so a quiet account isn't throttled.
        self._send_bucket = TokenBucket(
            send_rate_per_min, burst=max(1, int(send_rate_per_min)), clock=clock
        )

    # --- connection ---
    async def connect(self) -> None:
        await run_with_flood_wait_retry(lambda: self._client.connect(), operation="connect")
        self._ensure_handler()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def is_authorized(self) -> bool:
        return await run_with_flood_wait_retry(lambda: self._client.is_user_authorized(), operation="is_authorized")

    async def log_out(self) -> bool:
        """Log this session out of Telegram — the server invalidates the auth key (#11)."""
        return await run_with_flood_wait_retry(lambda: self._client.log_out(), operation="log_out")

    def save_session(self) -> None:
        self._store.save(self._session_name, self._client.session.save())

    def export_session_string(self) -> str:
        """Return the current plaintext StringSession — full account access; never log it."""
        return self._client.session.save()

    def import_session_string(self, session_string: str) -> None:
        """Validate an externally supplied StringSession and persist it under this session name."""
        self._store.save(self._session_name, self._store.from_external(session_string))

    async def get_me(self) -> User:
        raw = await run_with_flood_wait_retry(lambda: self._client.get_me(), operation="get_me")
        return self._to_user(raw) or User(id=0)

    async def entity_title(self, peer: int) -> str:
        """Human-readable name of a user/group/channel (group title wins)."""
        entity = await run_with_flood_wait_retry(
            lambda: self._client.get_entity(int(peer)), operation="entity_title"
        )
        return _entity_title(entity)

    # --- dialogs / history ---
    async def dialogs(self, dm_only: bool = True) -> list[Dialog]:
        """Mapped dialog list, served from a short-TTL cache.

        The cache holds ONE full list (every kind); ``dm_only`` filters from it,
        so tab switching after the first load makes zero network calls.
        Concurrent first-loads coalesce (single-flight). The returned list is a
        fresh copy — the cached models are shared (UIs render, never mutate).
        """
        full = await self._dialogs_cache.get_or_fetch(
            _DIALOGS_CACHE_KEY, lambda: self._fetch_dialogs(archived=False)
        )
        if dm_only:
            return [d for d in full if d.kind == "dm"]
        return list(full)

    async def archived_dialogs(self) -> list[Dialog]:
        """Archived dialogs of every kind, served from the dialogs TTL cache."""
        full = await self._dialogs_cache.get_or_fetch(
            _ARCHIVED_DIALOGS_CACHE_KEY, lambda: self._fetch_dialogs(archived=True)
        )
        return list(full)

    async def group_dialogs(self) -> list[Dialog]:
        """Every non-DM dialog (groups, supergroups, channels, bots).

        The "Группы" tab in every UI — same cache as ``dialogs()``, filtered the
        opposite way, so the kind filter lives in one place, not in each front-end.
        """
        full = await self._dialogs_cache.get_or_fetch(
            _DIALOGS_CACHE_KEY, lambda: self._fetch_dialogs(archived=False)
        )
        return [d for d in full if d.kind != "dm"]

    async def _fetch_dialogs(self, *, archived: bool = False) -> list[Dialog]:
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_dialogs(archived=archived),
            operation="archived_dialogs" if archived else "dialogs",
        )
        result = []
        for d in raw:
            entity = d.entity
            last_msg = getattr(d, "message", None)
            kind = _dialog_kind(entity)
            result.append(
                Dialog(
                    # telethon Dialog.id is the marked peer id (negative for groups/
                    # channels) — the same value events carry and history/send accept
                    id=int(getattr(d, "id", entity.id)),
                    title=_entity_title(entity),
                    kind=kind,
                    username=getattr(entity, "username", None),
                    unread=getattr(d, "unread_count", 0) or 0,
                    last_text=getattr(last_msg, "text", None),
                    last_message_at=getattr(last_msg, "date", None),
                    is_contact=bool(getattr(entity, "contact", False)) if kind == "dm" else None,
                    archived=archived or getattr(d, "folder_id", None) == 1,
                    telegram_lang_code=(
                        clean_supported_lang_code(getattr(entity, "lang_code", None))
                        if kind == "dm" else None
                    ),
                    can_send=_entity_can_send(entity),
                )
            )
        return result

    async def _collect_dialogs(self, *, archived: bool = False) -> list:
        return [d async for d in self._client.iter_dialogs(archived=archived)]

    async def history(self, peer: int, limit: int = 50, offset_id: int = 0) -> list[Message]:
        """Return messages in chronological order (oldest first), TTL-cached.

        Telethon yields newest-first; reversed here so UIs can render top-down
        and append live messages at the bottom. Cached per ``(peer, limit,
        offset_id)``; the cache is invalidated for a peer on every write and live
        event so freshly-sent/received messages never go missing. Returns a copy.
        """
        key = (int(peer), int(limit), int(offset_id))
        msgs = await self._history_cache.get_or_fetch(
            key, lambda: self._fetch_history(peer, limit, offset_id)
        )
        return list(msgs)

    async def _fetch_history(self, peer, limit, offset_id) -> list[Message]:
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_history(peer, limit, offset_id), operation="history"
        )
        return [self._to_message(m, dialog_id=int(peer)) for m in reversed(raw)]

    async def _collect_history(self, peer, limit, offset_id) -> list:
        return [m async for m in self._client.iter_messages(peer, limit=limit, offset_id=offset_id)]

    async def history_since(self, peer: int, min_id: int = 0, limit: int = 50) -> list[Message]:
        """Uncached history page newer than ``min_id``, chronological.

        The persistent message store owns its own cooldown/watermarks. It must not
        build those watermarks from the short-lived UI TTL cache used by ``history``.
        """
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_history_since(peer, min_id, limit), operation="history_since"
        )
        return [self._to_message(m, dialog_id=int(peer)) for m in reversed(raw)]

    async def _collect_history_since(self, peer, min_id, limit) -> list:
        return [m async for m in self._client.iter_messages(peer, limit=limit, min_id=min_id)]

    def _invalidate_history(self, peer: int) -> None:
        """Drop every cached history page of ``peer`` (any limit/offset)."""
        p = int(peer)
        self._history_cache.invalidate_if(lambda k: k[0] == p)

    async def search_messages(self, peer: int, query: str, limit: int = 20) -> list[Message]:
        """Server-side message search within a dialog (Telegram's own ``search=``).

        NOT cached (a one-off lookup, not a page the UIs re-read); routed through
        ``run_with_flood_wait_retry`` like every other network read.
        """
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_search(peer, query, limit), operation="search_messages"
        )
        return [self._to_message(m, dialog_id=int(peer)) for m in raw]

    async def _collect_search(self, peer, query, limit) -> list:
        return [m async for m in self._client.iter_messages(peer, search=query, limit=limit)]

    # --- sending ---
    async def send_text(
        self,
        peer: int,
        text: str,
        reply_to: int | None = None,
        schedule: timedelta | datetime | None = None,
    ) -> Message:
        """Send ``text`` to ``peer``; ``schedule`` (a delay or absolute time) defers it server-side."""
        await self._send_bucket.acquire()  # global outgoing cap (#25), before the retry loop
        try:
            msg = await run_with_flood_wait_retry(
                lambda: self._client.send_message(peer, text, reply_to=reply_to, schedule=schedule),
                operation="send_text",
            )
        except _SEND_FORBIDDEN_ERRORS as exc:
            raise SendForbiddenError(str(exc)) from exc
        self._invalidate_history(peer)  # the new message must show on reopen
        return self._to_message(msg, dialog_id=int(peer))

    async def forward(self, from_peer: int, message_ids: list[int], to_peer: int) -> list[Message]:
        """Forward ``message_ids`` from ``from_peer`` to ``to_peer``.

        Invalidates the history cache of BOTH peers (source can change too via
        Telegram's own behaviour, and the destination gains the new messages).
        """
        await self._send_bucket.acquire()  # global outgoing cap (#25)
        sent = await run_with_flood_wait_retry(
            lambda: self._client.forward_messages(to_peer, message_ids, from_peer),
            operation="forward",
        )
        self._invalidate_history(from_peer)
        self._invalidate_history(to_peer)
        raw_sent = list(sent or [])
        missing_count = sum(1 for m in raw_sent if m is None)
        if missing_count:
            logger.warning(
                "forward returned %s missing message(s) for from_peer=%s to_peer=%s",
                missing_count,
                from_peer,
                to_peer,
            )
        return [self._to_message(m, dialog_id=int(to_peer)) for m in raw_sent if m is not None]

    async def edit_text(self, peer: int, message_id: int, text: str) -> Message:
        msg = await run_with_flood_wait_retry(
            lambda: self._client.edit_message(peer, int(message_id), text),
            operation="edit_text",
        )
        self._invalidate_history(peer)
        return self._to_message(msg, dialog_id=int(peer))

    async def delete_messages(self, peer: int, message_ids: list[int], revoke: bool = True) -> None:
        """Delete messages; ``revoke=True`` removes them for everyone (default)."""
        if not revoke and is_channel_or_megagroup_id(peer):
            raise MessageDeleteValidationError(
                "--for-me is not supported for channels/supergroups; Telegram deletes there for everyone"
            )
        await self._validate_delete_messages(peer, message_ids)
        await run_with_flood_wait_retry(
            lambda: self._client.delete_messages(peer, message_ids, revoke=revoke),
            operation="delete_messages",
        )
        self._invalidate_history(peer)

    async def _validate_delete_messages(self, peer: int, message_ids: list[int]) -> None:
        wanted = {int(mid) for mid in message_ids}
        if not wanted:
            return
        raw = await run_with_flood_wait_retry(
            lambda: self._collect_messages_by_ids(peer, list(wanted)),
            operation="delete_messages_validate",
        )
        by_id = {int(getattr(m, "id")): m for m in raw if m is not None and getattr(m, "id", None) is not None}
        missing = wanted - set(by_id)
        if missing:
            joined = ", ".join(str(mid) for mid in sorted(missing))
            raise MessageDeleteValidationError(
                f"message ids not found in dialog {int(peer)}: {joined}"
            )
        for mid, msg in by_id.items():
            dialog_id = _message_dialog_id(msg)
            if dialog_id != int(peer):
                raise MessageDeleteValidationError(
                    f"message {mid} belongs to dialog {dialog_id}, not {int(peer)}"
                )

    async def _collect_messages_by_ids(self, peer, message_ids) -> list:
        return [m async for m in self._client.iter_messages(peer, ids=message_ids)]

    async def mute_user(self, peer: int, user_id: int, until_sec: int) -> None:
        """Restrict a user from sending messages in ``peer`` for ``until_sec`` seconds.

        Thin wrapper over Telethon ``edit_permissions``; omitted boolean permissions
        mean "do not restrict", so every send-related permission is explicitly
        revoked for the future ``until_date``. The moderator engine calls it.
        Flood-wait retried.
        """
        until_date = datetime.now(timezone.utc) + timedelta(seconds=int(until_sec))
        await run_with_flood_wait_retry(
            lambda: self._client.edit_permissions(
                peer,
                int(user_id),
                until_date,
                send_messages=False,
                send_media=False,
                send_stickers=False,
                send_gifs=False,
                send_games=False,
                send_inline=False,
                send_polls=False,
                embed_link_previews=False,
            ),
            operation="mute_user",
        )

    async def ban_user(self, peer: int, user_id: int) -> None:
        """Ban a user from ``peer`` (revoke view_messages — kicks and blocks re-entry)."""
        await run_with_flood_wait_retry(
            lambda: self._client.edit_permissions(peer, int(user_id), view_messages=False),
            operation="ban_user",
        )

    async def moderation_rights(self, peer: int) -> dict[str, bool]:
        """Moderation-capable rights for OUR account in ``peer``.

        Best-effort: any failure (not a participant, not a group, network) → False,
        logged — never raises. The moderator uses it to disable rules in chats we
        can't act on instead of crashing.
        """
        try:
            me = await run_with_flood_wait_retry(lambda: self._client.get_me(), operation="is_admin_me")
            perms = await run_with_flood_wait_retry(
                lambda: self._client.get_permissions(peer, me), operation="is_admin"
            )
        except Exception:
            logger.warning("could not read permissions for chat %s — assuming not admin",
                           peer, exc_info=True)
            perms = None
        if perms is None:
            return {"delete_messages": False, "ban_users": False}
        return {
            "delete_messages": bool(getattr(perms, "delete_messages", False)),
            "ban_users": bool(getattr(perms, "ban_users", False)),
        }

    async def is_admin(self, peer: int) -> bool:
        """Whether OUR account has any moderation-capable right in ``peer``."""
        rights = await self.moderation_rights(peer)
        return rights["delete_messages"] or rights["ban_users"]

    async def mark_read(self, peer: int, max_id: int | None = None) -> None:
        """Mark a dialog read (clears its unread counter), routed through flood retry."""
        await run_with_flood_wait_retry(
            lambda: self._client.send_read_acknowledge(peer, max_id=max_id),
            operation="mark_read",
        )
        self._dialogs_cache.invalidate(_DIALOGS_CACHE_KEY)
        self._dialogs_cache.invalidate(_ARCHIVED_DIALOGS_CACHE_KEY)

    async def check_username(self, username: str) -> bool:
        """Whether ``username`` is free for OUR account (True = available).

        Telethon ``account.CheckUsernameRequest`` through flood-wait retry.
        An invalid username maps to ``ValueError`` (clear message, not a raw
        Telethon traceback); occupied/unavailable returns ``False``.
        """
        try:
            return bool(
                await run_with_flood_wait_retry(
                    lambda: self._client(CheckUsernameRequest(username=username)),
                    operation="check_username",
                )
            )
        except UsernameInvalidError as exc:
            raise ValueError(f"invalid username: {username}") from exc

    async def set_username(self, username: str) -> None:
        """Set OUR account's public username; flood-wait retried.

        Occupied or invalid usernames map to ``ValueError`` with a clear message.
        """
        try:
            await run_with_flood_wait_retry(
                lambda: self._client(UpdateUsernameRequest(username=username)),
                operation="set_username",
            )
        except UsernameOccupiedError as exc:
            raise ValueError(f"username already taken: {username}") from exc
        except UsernameInvalidError as exc:
            raise ValueError(f"invalid username: {username}") from exc

    async def clear_username(self) -> None:
        """Remove OUR account's public username (sets it to empty), flood-wait retried."""
        await run_with_flood_wait_retry(
            lambda: self._client(UpdateUsernameRequest(username="")),
            operation="clear_username",
        )

    async def send_media(
        self,
        peer: int,
        file_path: str | Path,
        *,
        caption: str | None = None,
        voice_note: bool = False,
        video_note: bool = False,
        force_document: bool = False,
    ) -> Message:
        """Upload ``file_path`` to ``peer``.

        ``voice_note``/``video_note``/``force_document`` map straight onto Telethon
        ``send_file``. The path is validated BEFORE any network call — a missing path
        or a directory raises ``ValueError`` (no FloodWait budget burned on a typo).
        """
        path = Path(file_path)
        if not path.is_file():
            raise ValueError(f"file not found: {file_path}")
        await self._send_bucket.acquire()  # global outgoing cap (#25), after the cheap path check
        try:
            msg = await run_with_flood_wait_retry(
                lambda: self._client.send_file(
                    peer, str(path), caption=caption, voice_note=voice_note,
                    video_note=video_note, force_document=force_document,
                ),
                operation="send_media",
            )
        except _SEND_FORBIDDEN_ERRORS as exc:
            raise SendForbiddenError(str(exc)) from exc
        self._invalidate_history(peer)
        return self._to_message(msg, dialog_id=int(peer))

    async def send_reaction(self, peer: int, message_id: int, emoticon: str) -> None:
        """React to a message with a standard emoji, routed through flood-wait retry."""
        await self._send_bucket.acquire()  # global outgoing cap (#25)
        try:
            await run_with_flood_wait_retry(
                lambda: self._client(
                    SendReactionRequest(
                        peer=peer, msg_id=int(message_id),
                        reaction=[ReactionEmoji(emoticon=emoticon)],
                    )
                ),
                operation="send_reaction",
            )
        except _SEND_FORBIDDEN_ERRORS as exc:
            raise SendForbiddenError(str(exc)) from exc

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
            lambda: self._collect_messages_by_ids(peer, message_id),
            operation="download_message_media",
        )
        if not raw or getattr(raw[0], "media", None) is None:
            return None
        return await self.download_media(raw[0], dest)

    # --- realtime ---
    def _ensure_handler(self) -> None:
        if self._handler_registered:
            return
        self._client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        # own messages from ANY device + deletions — the watch feature's raw streams;
        # publishing into a bus without subscribers is a no-op, so eager is free
        self._client.add_event_handler(self._on_outgoing_message, events.NewMessage(outgoing=True))
        self._client.add_event_handler(self._on_deleted, events.MessageDeleted())
        # #14: chat actions (joins/leaves/title/pin/photo), read receipts, reactions
        self._client.add_event_handler(self._on_chat_action, events.ChatAction())
        self._client.add_event_handler(self._on_message_read, events.MessageRead())
        self._client.add_event_handler(self._on_message_read, events.MessageRead(inbox=True))
        # reactions arrive as a raw update (no high-level event for user accounts)
        self._client.add_event_handler(self._on_reaction, events.Raw(UpdateMessageReactions))
        self._handler_registered = True

    async def _on_new_message(self, event) -> None:
        try:
            dialog_id = int(getattr(event, "chat_id", 0) or 0)
            # invalidate BEFORE mapping: a broken event still drops stale history
            self._invalidate_history(dialog_id)
            message = self._to_message(event.message, dialog_id=dialog_id)
            album_id = getattr(event.message, "grouped_id", None)
            incoming = IncomingEvent(
                dialog_id=dialog_id,
                message=message,
                album_id=int(album_id) if album_id is not None else None,
            )
            self._bus_all.publish(incoming)  # every chat — the UIs' groups tab
            if getattr(event, "is_private", True):
                # listen() stays private-only (DMs + bots) — the agent relies on it
                self._bus.publish(incoming)
        except Exception:
            # don't depend on Telethon's logger config; record it ourselves
            logger.exception("failed to handle incoming message")

    async def _on_outgoing_message(self, event) -> None:
        # no is_private filter: groups are the whole point of the watch feature
        try:
            dialog_id = int(getattr(event, "chat_id", 0) or 0)
            self._invalidate_history(dialog_id)
            message = self._to_message(event.message, dialog_id=dialog_id)
            self._bus_out.publish(OutgoingEvent(dialog_id=dialog_id, message=message))
        except Exception:
            logger.exception("failed to handle outgoing message")

    async def _on_deleted(self, event) -> None:
        try:
            ids = [int(i) for i in (getattr(event, "deleted_ids", None) or [])]
            chat_id = getattr(event, "chat_id", None)
            # deletions are rare: drop the named peer, else wipe all history
            if chat_id is not None:
                self._invalidate_history(int(chat_id))
            else:
                self._history_cache.invalidate()
            self._bus_deleted.publish(
                MessagesDeletedEvent(
                    chat_id=int(chat_id) if chat_id is not None else None, message_ids=ids
                )
            )
        except Exception:
            logger.exception("failed to handle deleted-messages event")

    @staticmethod
    def _to_user(raw) -> User | None:
        """Best-effort map a Telethon user entity → User (None if absent)."""
        if raw is None:
            return None
        return User(
            id=getattr(raw, "id", 0) or 0,
            first_name=getattr(raw, "first_name", None),
            last_name=getattr(raw, "last_name", None),
            username=getattr(raw, "username", None),
        )

    @staticmethod
    def _chat_action_kind(event) -> str:
        if getattr(event, "user_joined", False) or getattr(event, "user_added", False):
            return "join"
        if getattr(event, "user_kicked", False):
            return "kick"
        if getattr(event, "user_left", False):
            return "leave"
        if getattr(event, "new_title", None):
            return "title"
        if getattr(event, "new_pin", False):
            return "pin"
        if getattr(event, "new_photo", False):
            return "photo"
        return "other"

    async def _on_chat_action(self, event) -> None:
        try:
            dialog_id = int(getattr(event, "chat_id", 0) or 0)
            kind = self._chat_action_kind(event)
            user = self._to_user(getattr(event, "user", None))
            # actor = whoever added/kicked them (None for self-actions)
            actor = self._to_user(
                getattr(event, "added_by", None) or getattr(event, "kicked_by", None)
            )
            raw_text = getattr(getattr(event, "action_message", None), "message", None) \
                or getattr(event, "new_title", None)
            self._bus_chat_actions.publish(
                ChatActionEvent(
                    dialog_id=dialog_id, kind=kind, user=user, actor=actor, raw_text=raw_text
                )
            )
        except Exception:
            logger.exception("failed to handle chat-action event")

    async def _on_message_read(self, event) -> None:
        try:
            dialog_id = int(getattr(event, "chat_id", 0) or 0)
            self._bus_reads.publish(
                MessageReadEvent(
                    dialog_id=dialog_id,
                    max_id=int(getattr(event, "max_id", 0) or 0),
                    outbox=bool(getattr(event, "outbox", False)),
                )
            )
        except Exception:
            logger.exception("failed to handle message-read event")

    async def _on_reaction(self, update) -> None:
        try:
            peer = getattr(update, "peer", None)
            dialog_id = int(tl_utils.get_peer_id(peer)) if peer is not None else 0
            emoticon = self._recent_emoticon(getattr(update, "reactions", None))
            self._bus_reactions.publish(
                ReactionEvent(
                    dialog_id=dialog_id,
                    message_id=int(getattr(update, "msg_id", 0) or 0),
                    emoticon=emoticon,
                    actor_id=None,  # raw update carries no reliable single actor — best-effort None
                )
            )
        except Exception:
            # unknown update shape: log and skip, never break the stream
            logger.warning("failed to handle reaction update — skipping", exc_info=True)

    @staticmethod
    def _recent_emoticon(reactions) -> str | None:
        """Pull the changed standard-emoji reaction's emoticon; custom/premium → None."""
        recent = getattr(reactions, "recent_reactions", None) or []
        if not recent:
            return None
        reaction = getattr(recent[0], "reaction", None)
        return getattr(reaction, "emoticon", None)  # ReactionCustomEmoji has no .emoticon

    async def listen(self) -> AsyncIterator[IncomingEvent]:
        """Incoming from private chats only (DMs + bots)."""
        async for ev in self._bus.subscribe():
            yield ev

    async def listen_all(self) -> AsyncIterator[IncomingEvent]:
        """Incoming from every chat — groups and channels included."""
        async for ev in self._bus_all.subscribe():
            yield ev

    async def listen_outgoing(self) -> AsyncIterator[OutgoingEvent]:
        async for ev in self._bus_out.subscribe():
            yield ev

    async def listen_deleted(self) -> AsyncIterator[MessagesDeletedEvent]:
        async for ev in self._bus_deleted.subscribe():
            yield ev

    async def listen_chat_actions(self) -> AsyncIterator[ChatActionEvent]:
        """Participant/structure changes (joins, leaves, kicks, title/pin/photo)."""
        async for ev in self._bus_chat_actions.subscribe():
            yield ev

    async def listen_reads(self) -> AsyncIterator[MessageReadEvent]:
        """Read receipts; ``outbox=True`` means the other party read OUR messages."""
        async for ev in self._bus_reads.subscribe():
            yield ev

    async def listen_reactions(self) -> AsyncIterator[ReactionEvent]:
        """Reactions added to messages (custom/premium reactions map to emoticon=None)."""
        async for ev in self._bus_reactions.subscribe():
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
        # best-effort: Telethon exposes .reply_to (MessageReplyHeader) with .reply_to_msg_id
        reply_to = getattr(raw, "reply_to", None)
        reply_to_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to is not None else None
        return Message(
            id=getattr(raw, "id", 0),
            dialog_id=dialog_id,
            sender_id=getattr(raw, "sender_id", 0) or 0,
            out=bool(getattr(raw, "out", False)),
            date=raw.date,
            text=getattr(raw, "text", None) or getattr(raw, "message", None),
            media=media,
            reply_to_id=int(reply_to_id) if reply_to_id is not None else None,
            is_forward=getattr(raw, "forward", None) is not None,
        )


def client_from_env(**kwargs) -> StandaloneTelegramClient:
    """Build a client from the environment — the single env→client plumbing for
    CLI/web/TUI (``TG_API_ID``, ``TG_API_HASH``, ``TG_SESSION_DIR``,
    ``SESSION_ENCRYPTION_KEY``, ``TG_SEND_RATE``). Explicit kwargs win over env.
    """
    # optional at-rest session encryption (shared SESSION_ENCRYPTION_KEY = SSO with the factory)
    kwargs.setdefault("encryption_key", os.environ.get("SESSION_ENCRYPTION_KEY") or None)
    kwargs.setdefault("session_dir", os.environ.get("TG_SESSION_DIR") or DEFAULT_SESSION_DIR)
    # global outgoing rate cap (#25): default 20/min; TG_SEND_RATE=0 explicitly disables.
    kwargs.setdefault("send_rate_per_min", float(os.environ.get("TG_SEND_RATE", "20") or 20))
    return StandaloneTelegramClient(
        api_id=int(os.environ.get("TG_API_ID", "0")),
        api_hash=os.environ.get("TG_API_HASH", ""),
        **kwargs,
    )
