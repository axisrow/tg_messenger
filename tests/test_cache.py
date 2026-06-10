"""TTLCache — TTL + maxsize-eviction + async single-flight get_or_fetch.

Clock is injected (``t = {"now": 0.0}``) so tests never sleep; concurrency is
exercised with asyncio.gather + Event, never real time.
"""

from __future__ import annotations

import asyncio

import pytest

from tg_messenger.core.cache import TTLCache


def _clock(t):
    return lambda: t["now"]


# --- 39: get/set/TTL/eviction ---

def test_miss_returns_default():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    assert c.get("k") is None
    assert c.get("k", "fallback") == "fallback"


def test_set_then_get():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set("k", "v")
    assert c.get("k") == "v"


def test_alive_just_before_ttl():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set("k", "v")
    t["now"] = 9.999
    assert c.get("k") == "v"


def test_dead_at_ttl():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set("k", "v")
    t["now"] = 10.0  # clock() >= stored_at + ttl → expired
    assert c.get("k") is None


def test_maxsize_evicts_oldest():
    t = {"now": 0.0}
    c = TTLCache(ttl=100.0, maxsize=2, clock=_clock(t))
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)  # evicts "a" (oldest)
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_reset_set_refreshes_expiry_and_order():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, maxsize=2, clock=_clock(t))
    c.set("a", 1)
    c.set("b", 2)
    t["now"] = 5.0
    c.set("a", 10)  # refresh "a": new expiry AND moves it to newest
    c.set("c", 3)  # evicts "b" now, not "a"
    assert c.get("b") is None
    assert c.get("a") == 10
    t["now"] = 14.999  # "a" stored at 5.0 → alive until 15.0
    assert c.get("a") == 10


# --- 40: invalidate / invalidate_if ---

def test_invalidate_single_key():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set("a", 1)
    c.set("b", 2)
    c.invalidate("a")
    assert c.get("a") is None
    assert c.get("b") == 2


def test_invalidate_all():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set("a", 1)
    c.set("b", 2)
    c.invalidate()  # None → wipe everything
    assert c.get("a") is None
    assert c.get("b") is None


def test_invalidate_missing_key_noop():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set("a", 1)
    c.invalidate("zzz")  # no error
    assert c.get("a") == 1


def test_invalidate_if_by_predicate():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set((7, 50, 0), "peer7")
    c.set((7, 20, 0), "peer7b")
    c.set((9, 50, 0), "peer9")
    c.invalidate_if(lambda k: k[0] == 7)
    assert c.get((7, 50, 0)) is None
    assert c.get((7, 20, 0)) is None
    assert c.get((9, 50, 0)) == "peer9"


# --- 41: single-flight get_or_fetch ---

async def test_get_or_fetch_hit_does_not_call_fetch():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    c.set("k", "cached")
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return "fetched"

    assert await c.get_or_fetch("k", fetch) == "cached"
    assert calls["n"] == 0


async def test_get_or_fetch_miss_fetches_and_caches():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return "fetched"

    assert await c.get_or_fetch("k", fetch) == "fetched"
    assert calls["n"] == 1
    # second call hits cache, no fetch
    assert await c.get_or_fetch("k", fetch) == "fetched"
    assert calls["n"] == 1


async def test_get_or_fetch_coalesces_concurrent_same_key():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    calls = {"n": 0}
    gate = asyncio.Event()

    async def fetch():
        calls["n"] += 1
        await gate.wait()
        return "fetched"

    async def release():
        # let both coroutines reach the lock/fetch, then release
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()

    results = await asyncio.gather(
        c.get_or_fetch("k", fetch),
        c.get_or_fetch("k", fetch),
        release(),
    )
    assert results[0] == "fetched"
    assert results[1] == "fetched"
    assert calls["n"] == 1  # single-flight: only one network call


async def test_get_or_fetch_different_keys_independent():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    calls = {"n": 0}

    async def make_fetch(val):
        async def fetch():
            calls["n"] += 1
            return val

        return fetch

    f1 = await make_fetch("a")
    f2 = await make_fetch("b")
    r = await asyncio.gather(c.get_or_fetch("k1", f1), c.get_or_fetch("k2", f2))
    assert set(r) == {"a", "b"}
    assert calls["n"] == 2


async def test_get_or_fetch_failed_fetch_not_cached_no_deadlock():
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    calls = {"n": 0}

    async def failing():
        calls["n"] += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await c.get_or_fetch("k", failing)
    assert calls["n"] == 1
    # not cached, and no deadlock — a retry fetches again
    with pytest.raises(RuntimeError):
        await c.get_or_fetch("k", failing)
    assert calls["n"] == 2

    # and a subsequent succeeding fetch works (lock released cleanly)
    async def ok():
        return "ok"

    assert await c.get_or_fetch("k", ok) == "ok"
