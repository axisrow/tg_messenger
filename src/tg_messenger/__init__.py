"""tg_messenger — standalone reusable Telegram messenger client."""

from tg_messenger.core import (
    Dialog,
    IncomingEvent,
    MediaRef,
    Message,
    StandaloneTelegramClient,
    User,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "StandaloneTelegramClient",
    "Dialog",
    "Message",
    "User",
    "MediaRef",
    "IncomingEvent",
]
