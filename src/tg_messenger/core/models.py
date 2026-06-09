"""Pydantic v2 domain models shared across all interfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

MediaKind = Literal["photo", "document", "other"]


class User(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None


class MediaRef(BaseModel):
    kind: MediaKind
    file_name: str | None = None
    size: int | None = None
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
