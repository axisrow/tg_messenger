"""tg_messenger — standalone reusable Telegram messenger client."""

from tg_messenger.core import (
    LOGIN_HINT,
    Dialog,
    EventBus,
    HandledFloodWaitError,
    IncomingEvent,
    LoginFlow,
    MediaRef,
    Message,
    SessionStore,
    StandaloneTelegramClient,
    User,
    run_with_flood_wait_retry,
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
    "SessionStore",
    "LoginFlow",
    "LOGIN_HINT",
    "EventBus",
    "run_with_flood_wait_retry",
    "HandledFloodWaitError",
]
