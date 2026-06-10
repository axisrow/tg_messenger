"""Core layer — UI-agnostic Telegram client, models, events, auth."""

from tg_messenger.core.client import StandaloneTelegramClient
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
]
