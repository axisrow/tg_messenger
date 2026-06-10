"""Slim, dependency-free FloodWait handling vendored from the main project.

No pool/DB coupling: just transient-retry with a budget. Mirrors the discipline
of ``src/telegram/flood_wait.py`` in tg_content_factory.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_FLOOD_WAIT_MAX_SEC = 60
TRANSIENT_FLOOD_WAIT_RETRY_BUDGET_SEC = 120
FLOOD_WAIT_RETRY_BUFFER_SEC = 1.0


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


def coerce_flood_wait_seconds(value: object) -> int:
    return max(1, int(value or 0))


def is_transient_flood_wait_seconds(
    wait_seconds: int | None,
    *,
    max_seconds: int = TRANSIENT_FLOOD_WAIT_MAX_SEC,
) -> bool:
    if wait_seconds is None:
        return False
    return 0 < int(wait_seconds) <= max_seconds


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
