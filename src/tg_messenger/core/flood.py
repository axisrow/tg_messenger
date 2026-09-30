"""Compatibility surface for the shared telethon-floodgate retry policy."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import telethon_floodgate as floodgate
from telethon_floodgate import (
    FLOOD_WAIT_RETRY_BUFFER_SEC as FLOOD_WAIT_RETRY_BUFFER_SEC,
)
from telethon_floodgate import (
    TRANSIENT_FLOOD_WAIT_MAX_SEC,
    TRANSIENT_FLOOD_WAIT_RETRY_BUDGET_SEC,
)
from telethon_floodgate import (
    coerce_flood_wait_seconds as coerce_flood_wait_seconds,
)
from telethon_floodgate import (
    is_transient_flood_wait_seconds as is_transient_flood_wait_seconds,
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
    try:
        return await floodgate.run_with_flood_wait_retry(
            awaitable_factory,
            operation=operation,
            logger_=logger_ or logger,
            transient_wait_max_sec=transient_wait_max_sec,
            transient_wait_budget_sec=transient_wait_budget_sec,
        )
    except floodgate.HandledFloodWaitError as exc:
        # The package logs transient waits at INFO, so an exhausted budget
        # would otherwise never surface above INFO. Blocking waits are already
        # warned by the package itself.
        if is_transient_flood_wait_seconds(exc.info.wait_seconds):
            logger.warning(
                "%s: flood-wait budget exhausted, last transient wait %ss",
                exc.info.operation,
                exc.info.wait_seconds,
            )
        raise HandledFloodWaitError(exc.info.operation, exc.info.wait_seconds) from exc
