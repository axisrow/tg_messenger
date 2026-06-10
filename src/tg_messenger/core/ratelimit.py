"""TokenBucket — one global cap on outgoing sends across every subsystem.

After the #6 decomposition four subsystems send (agent, heartbeat #19, moderator
warns #16, interop worker #20). FloodWait is retried (#8), but a systematically high
send rate risks an account ban — worse than any single wait. One token bucket in core
caps them all. ``acquire()`` never raises — it WAITS for the next token (sends slow
down, nothing is lost) and logs a WARNING. ``clock``/``sleep`` are injected so tests
advance a fake clock and never sleep for real. Concurrent acquirers form a fair queue
(one ``asyncio.Lock``). Reads (dialogs/history) are NOT limited — the #8 cache covers them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class TokenBucket:
    def __init__(
        self,
        rate_per_min: float,
        *,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._rate_per_sec = rate_per_min / 60.0 if rate_per_min > 0 else 0.0
        self._burst = max(1, burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self._burst)
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._rate_per_sec > 0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate_per_sec)
            self._updated = now

    async def acquire(self) -> None:
        """Take one token, waiting (never erroring) until one is available."""
        if not self.enabled:
            return  # rate 0/None disables the limiter entirely
        async with self._lock:  # fair queue: concurrent acquirers wait their turn
            self._refill()
            if self._tokens < 1.0:
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate_per_sec
                logger.warning("outgoing rate limit: waiting %.1fs for next token", wait)
                await self._sleep(wait)
                self._refill()
            self._tokens -= 1.0
