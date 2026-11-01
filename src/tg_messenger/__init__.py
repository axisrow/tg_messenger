"""tg_messenger — standalone reusable Telegram messenger client."""

from importlib.metadata import PackageNotFoundError, version

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

try:
    # Single source of truth is the distribution metadata (#296): a stale literal
    # here shipped 0.1.1 while reporting "0.1.0".
    __version__ = version("tg-messenger")
except PackageNotFoundError:  # bare source checkout (PYTHONPATH=src, no install)
    __version__ = "0.0.0.dev0"

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
