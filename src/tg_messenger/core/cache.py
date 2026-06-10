"""TTLCache — dependency-free TTL cache with maxsize-eviction and async single-flight.

Stdlib-only. ``clock`` is injected (defaults to ``time.monotonic``) so tests
never sleep — they advance a fake clock instead. ``get_or_fetch`` coalesces
concurrent misses on the same key behind a per-key lock: a single in-flight
fetch serves all waiters; a failing fetch is neither cached nor left holding a
lock (no deadlock on retry).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_MISSING = object()


class TTLCache(Generic[K, V]):
    def __init__(
        self,
        ttl: float,
        *,
        maxsize: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl
        self._maxsize = maxsize
        self._clock = clock
        self._store: OrderedDict[K, tuple[V, float]] = OrderedDict()
        self._locks: dict[K, asyncio.Lock] = {}

    def get(self, key: K, default=None):
        entry = self._store.get(key)
        if entry is None:
            return default
        value, expires_at = entry
        if self._clock() >= expires_at:
            # lazily drop expired entries
            self._store.pop(key, None)
            return default
        return value

    def set(self, key: K, value: V) -> None:
        # re-set moves the key to newest and refreshes its expiry (pattern: watch.py)
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (value, self._clock() + self._ttl)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def invalidate(self, key: K | None = None) -> None:
        """Drop one key, or everything when ``key`` is None. Missing key is a no-op."""
        if key is None:
            self._store.clear()
        else:
            self._store.pop(key, None)

    def invalidate_if(self, predicate: Callable[[K], bool]) -> None:
        """Drop every key for which ``predicate(key)`` is true.

        Collects keys first, then deletes — never mutates the dict mid-iteration.
        """
        doomed = [k for k in self._store if predicate(k)]
        for k in doomed:
            self._store.pop(k, None)

    async def get_or_fetch(self, key: K, fetch: Callable[[], Awaitable[V]]) -> V:
        """Return the cached value or fetch it, coalescing concurrent misses.

        A per-key lock serialises misses on the same key; the value is
        double-checked under the lock so only the first waiter calls ``fetch``.
        A failing ``fetch`` propagates, is not cached, and releases its lock.
        """
        hit = self.get(key, _MISSING)
        if hit is not _MISSING:
            return hit  # type: ignore[return-value]
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        try:
            async with lock:
                hit = self.get(key, _MISSING)
                if hit is not _MISSING:
                    return hit  # type: ignore[return-value]
                value = await fetch()
                self.set(key, value)
                return value
        finally:
            # drop the per-key lock once no one else is waiting on it, so the
            # lock dict can't grow unboundedly; a failed fetch cleans up too.
            if not lock.locked() and not getattr(lock, "_waiters", None):
                self._locks.pop(key, None)
