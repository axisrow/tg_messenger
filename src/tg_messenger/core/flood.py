"""FloodWait handling for the single-account client.

Constants and classification helpers come from the shared telethon-floodgate
package (#252) instead of being vendored here. The retry loop stays local: it
must raise THIS app's ``HandledFloodWaitError`` shape (``.operation`` /
``.wait_seconds`` / ``.user_message``) and must keep catching the
``FloodWaitError`` referenced in this module (tests patch it here).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from telethon.errors import FloodWaitError
from telethon_floodgate import (
    FLOOD_WAIT_RETRY_BUFFER_SEC,
    TRANSIENT_FLOOD_WAIT_MAX_SEC,
    TRANSIENT_FLOOD_WAIT_RETRY_BUDGET_SEC,
    coerce_flood_wait_seconds,
    is_transient_flood_wait_seconds,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class HandledFloodWaitError(RuntimeError):
    """Raised when a FloodWait is non-transient (or budget exhausted)."""

    def __init__(self, operation: str, wait_seconds: int):
        super().__init__(f"{operation}: flood wait {wait_seconds}s")
        self.operation = operation
        self.wait_seconds = wait_seconds

    @property
    def user_message(self) -> str:
        """One user-facing phrasing for every UI."""
        return f"Telegram flood wait {self.wait_seconds}s — try again later."


async def run_with_flood_wait_retry(
    awaitable_factory: Callable[[], Awaitable[T]],
    *,
    operation: str,
    logger_: logging.Logger | None = None,
    transient_wait_max_sec: int = TRANSIENT_FLOOD_WAIT_MAX_SEC,
    transient_wait_budget_sec: int = TRANSIENT_FLOOD_WAIT_RETRY_BUDGET_SEC,
) -> T:
    """Run ``awaitable_factory()``, retrying transient FloodWaits within budget.

    Non-transient FloodWaits (or budget exhaustion) raise ``HandledFloodWaitError``.
    Any other exception propagates unchanged.
    """
    active_logger = logger_ or logger
    waited_seconds = 0
    while True:
        try:
            return await awaitable_factory()
        except FloodWaitError as exc:
            wait_seconds = coerce_flood_wait_seconds(getattr(exc, "seconds", 0))
            if not is_transient_flood_wait_seconds(wait_seconds, max_seconds=transient_wait_max_sec):
                active_logger.warning("%s: blocking flood wait %ss", operation, wait_seconds)
                raise HandledFloodWaitError(operation, wait_seconds) from exc
            if waited_seconds + wait_seconds > transient_wait_budget_sec:
                active_logger.warning("%s: flood-wait budget exhausted", operation)
                raise HandledFloodWaitError(operation, wait_seconds) from exc
            sleep_for = float(wait_seconds) + FLOOD_WAIT_RETRY_BUFFER_SEC
            active_logger.info("%s: transient flood wait %.1fs, retrying", operation, sleep_for)
            await asyncio.sleep(sleep_for)
            waited_seconds += wait_seconds
