"""Core layer — UI-agnostic Telegram client, models, events, auth."""

from tg_messenger.core.auth import LOGIN_HINT, LoginFlow, SessionStore
from tg_messenger.core.client import StandaloneTelegramClient
from tg_messenger.core.events import EventBus
from tg_messenger.core.flood import HandledFloodWaitError, run_with_flood_wait_retry
from tg_messenger.core.models import (
    Dialog,
    IncomingEvent,
    MediaRef,
    Message,
    MessagesDeletedEvent,
    OutgoingEvent,
    User,
)
from tg_messenger.core.watch import DeletionWatcher

__all__ = [
    "StandaloneTelegramClient",
    "DeletionWatcher",
    "Dialog",
    "Message",
    "User",
    "MediaRef",
    "IncomingEvent",
    "OutgoingEvent",
    "MessagesDeletedEvent",
    "SessionStore",
    "LoginFlow",
    "LOGIN_HINT",
    "EventBus",
    "run_with_flood_wait_retry",
    "HandledFloodWaitError",
]
