"""Pydantic v2 domain models shared across all interfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

MediaKind = Literal["photo", "voice", "document", "other"]


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
    id: int
    title: str
    username: str | None = None
    unread: int = 0
    last_message_at: datetime | None = None
    last_text: str | None = None


class Message(BaseModel):
    id: int
    dialog_id: int
    sender_id: int
    out: bool
    date: datetime
    text: str | None = None
    media: MediaRef | None = None


class IncomingEvent(BaseModel):
    dialog_id: int
    message: Message


class OutgoingEvent(BaseModel):
    """Own message sent from any device (groups included — no DM filter)."""

    dialog_id: int
    message: Message


class MessagesDeletedEvent(BaseModel):
    """Telegram only names the chat for channels/supergroups; elsewhere it's None."""

    chat_id: int | None = None
    message_ids: list[int]


def message_line(m: Message) -> str:
    """One-line text rendering shared by text UIs: '← [id] text' (→ for own messages)."""
    who = "→" if m.out else "←"
    return f"{who} [{m.id}] {m.text or '<media>'}"
