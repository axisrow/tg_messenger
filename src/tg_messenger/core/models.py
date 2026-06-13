"""Pydantic v2 domain models shared across all interfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

MediaKind = Literal["photo", "voice", "document", "other"]
DialogKind = Literal["dm", "group", "channel", "bot"]


class User(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None


class MediaRef(BaseModel):
    kind: MediaKind
    file_name: str | None = None
    size: int | None = None
    mime_type: str | None = None
    downloadable: bool = False


class Dialog(BaseModel):
    """``id`` is Telethon's marked peer id: negative for groups/channels —
    the same value events carry in ``chat_id`` and history/send accept."""

    id: int
    title: str
    kind: DialogKind = "dm"
    username: str | None = None
    unread: int = 0
    last_message_at: datetime | None = None
    last_text: str | None = None
    is_contact: bool | None = None
    archived: bool = False
    telegram_lang_code: str | None = None


class Message(BaseModel):
    id: int
    dialog_id: int
    sender_id: int
    out: bool
    date: datetime
    text: str | None = None
    media: MediaRef | None = None
    reply_to_id: int | None = None  # id of the message this one replies to, if any
    is_forward: bool = False  # True if this message was forwarded from elsewhere
    translated_text: str | None = None


class IncomingEvent(BaseModel):
    dialog_id: int
    message: Message
    # grouped_id of an album — same value across the album's messages, None otherwise.
    # v1 only marks it; consumers group by album_id themselves (no aggregator).
    album_id: int | None = None


class OutgoingEvent(BaseModel):
    """Own message sent from any device (groups included — no DM filter)."""

    dialog_id: int
    message: Message


class MessagesDeletedEvent(BaseModel):
    """Telegram only names the chat for channels/supergroups; elsewhere it's None."""

    chat_id: int | None = None
    message_ids: list[int]


ChatActionKind = Literal["join", "leave", "kick", "title", "pin", "photo", "other"]


class ChatActionEvent(BaseModel):
    """A participant/structure change in a chat (events.ChatAction) — moderator signal."""

    dialog_id: int
    kind: ChatActionKind
    user: User | None = None    # who joined/left/was acted on
    actor: User | None = None   # who added/kicked them (None when self-action)
    raw_text: str | None = None


class MessageReadEvent(BaseModel):
    """A read-receipt (events.MessageRead).

    ``outbox=True`` means the OTHER party read OUR messages up to ``max_id`` — the
    key "they've seen it" signal for the suggester.
    """

    dialog_id: int
    max_id: int
    outbox: bool = False


class ReactionEvent(BaseModel):
    """A reaction added to a message. Custom/premium reactions map to emoticon=None."""

    dialog_id: int
    message_id: int
    emoticon: str | None = None
    actor_id: int | None = None


def message_line(m: Message) -> str:
    """One-line text rendering shared by text UIs: '← [id] text' (→ for own messages)."""
    who = "→" if m.out else "←"
    return f"{who} [{m.id}] {m.text or '<media>'}"
