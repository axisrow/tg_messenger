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

# default cap for the UIs' echo-suppression caches (web SSE buckets, TUI _sent_ids/_sent_reactions)
DEFAULT_REMEMBER_CAP = 200


def bounded_remember(od: OrderedDict, key: Hashable, *, cap: int = DEFAULT_REMEMBER_CAP) -> None:
    """Record ``key`` as a recency-ordered membership marker in a bounded ``OrderedDict``.

    The shared echo-suppression primitive: the UIs track keys (a message id, or a
    ``(dialog, message, emoticon)`` triple) they themselves sent so the ``listen_outgoing()`` /
    ``listen_reactions()`` echo isn't rendered twice. The newest key moves to the end; once the
    map exceeds ``cap`` the oldest entries are dropped (same pattern as ``watch.py``'s caches).
    The value is the sentinel ``True`` so a later ``pop`` can distinguish a hit from a default.
    """
    od[key] = True
    od.move_to_end(key)
    while len(od) > cap:
        od.popitem(last=False)


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
        # per-key (lock, in-flight waiter count); the last waiter out drops the entry
        self._locks: dict[K, list] = {}
        # #125-A4: per-key monotonic generation. Bumped by every invalidate*/that targets the
        # key — INCLUDING a key present only as an in-flight fetch (not yet in _store).
        # get_or_fetch snapshots it before the await and refuses to cache a value computed
        # against a superseded generation, so an invalidation racing the fetch is not defeated.
        self._epochs: dict[K, int] = {}

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
            oldest, _ = self._store.popitem(last=False)
            self._prune_epoch(oldest)

    def _bump_epoch(self, key: K) -> None:
        """Advance a key's generation — any in-flight fetch for it is now stale (#125-A4)."""
        self._epochs[key] = self._epochs.get(key, 0) + 1

    def _prune_epoch(self, key: K) -> None:
        """Drop a key's epoch once nothing references it (not stored, no in-flight fetch).

        Keeps ``_epochs`` bounded as keys churn. Safe because a re-created key restarts at 0:
        ``get_or_fetch`` always re-snapshots the epoch under the lock right before the fetch,
        so a fresh key can never collide with a stale snapshot.
        """
        if key not in self._store and key not in self._locks:
            self._epochs.pop(key, None)

    def invalidate(self, key: K | None = None) -> None:
        """Drop one key, or everything when ``key`` is None. Missing key is a no-op.

        Bumps the epoch of every affected key (stored or not) so an invalidation racing an
        in-flight fetch defeats the post-fetch ``set()`` (#125-A4).
        """
        if key is None:
            # every in-flight fetch (for ANY key) is now stale
            for k in list(self._epochs):
                self._epochs[k] += 1
            self._store.clear()
        else:
            self._bump_epoch(key)
            self._store.pop(key, None)
            self._prune_epoch(key)

    def invalidate_if(self, predicate: Callable[[K], bool]) -> None:
        """Drop every key for which ``predicate(key)`` is true, and bump its epoch.

        Considers stored keys AND keys present only as an in-flight fetch (in ``_locks`` /
        ``_epochs``), so an invalidation racing a not-yet-stored fetch still invalidates it.
        Collects keys first, then mutates — never mutates a dict mid-iteration.
        """
        candidates = set(self._store) | set(self._locks) | set(self._epochs)
        doomed = [k for k in candidates if predicate(k)]
        for k in doomed:
            self._bump_epoch(k)
            self._store.pop(k, None)
            self._prune_epoch(k)

    async def get_or_fetch(self, key: K, fetch: Callable[[], Awaitable[V]]) -> V:
        """Return the cached value or fetch it, coalescing concurrent misses.

        A per-key lock serialises misses on the same key; the value is
        double-checked under the lock so only the first waiter calls ``fetch``.
        A failing ``fetch`` propagates, is not cached, and releases its lock.
        #125-A4: if ``invalidate``/``invalidate_if`` bumps this key's epoch DURING the
        fetch, the freshly-fetched value is returned to the caller but NOT cached
        (the invalidation wins — the next read re-fetches).
        """
        hit = self.get(key, _MISSING)
        if hit is not _MISSING:
            return hit  # type: ignore[return-value]
        # an explicit [lock, waiters] pair owns cleanup — no peeking at asyncio.Lock
        # internals: the last coroutine out of the finally drops the entry.
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                hit = self.get(key, _MISSING)
                if hit is not _MISSING:
                    return hit  # type: ignore[return-value]
                # snapshot the generation BEFORE the await; a concurrent invalidate* of this
                # key bumps it and we then refuse to cache the stale result.
                epoch = self._epochs.get(key, 0)
                value = await fetch()
                if self._epochs.get(key, 0) == epoch:
                    self.set(key, value)
                # else: invalidated mid-fetch — return the fresh value uncached.
                return value
        finally:
            entry[1] -= 1
            if entry[1] <= 0:
                self._locks.pop(key, None)  # last one out — a failed fetch too
                self._prune_epoch(key)  # epoch is dead once no fetch references it
