"""TTLCache — TTL + maxsize-eviction + async single-flight get_or_fetch.

Clock is injected (``t = {"now": 0.0}``) so tests never sleep; concurrency is
exercised with asyncio.gather + Event, never real time.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

import pytest

from tg_messenger.core.cache import TTLCache, bounded_remember


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


# --- #125-A4: invalidation racing an in-flight fetch must not be defeated ---


async def test_invalidate_during_fetch_is_not_re_cached():
    # invalidate(key) runs DURING the await fetch(); the freshly-fetched value must NOT be
    # cached (the invalidation wins). The caller still gets it; a subsequent get() is a miss.
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    started = asyncio.Event()
    proceed = asyncio.Event()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        started.set()
        await proceed.wait()  # park inside the fetch so we can invalidate mid-flight
        return "fresh"

    async def racer():
        await started.wait()
        c.invalidate("k")  # bumps the key's epoch while the fetch is parked
        proceed.set()

    result, _ = await asyncio.gather(c.get_or_fetch("k", fetch), racer())
    assert result == "fresh"  # caller still gets the freshly-fetched value
    assert calls["n"] == 1
    sentinel = object()
    assert c.get("k", sentinel) is sentinel  # NOT cached -> miss

    # a later fetch re-runs (nothing was cached behind the invalidation)
    async def fetch2():
        calls["n"] += 1
        return "second"

    assert await c.get_or_fetch("k", fetch2) == "second"
    assert calls["n"] == 2


async def test_invalidate_if_during_fetch_of_not_yet_stored_key():
    # the not-yet-stored race: invalidate_if scans while the key is in-flight (absent from
    # _store). It must still invalidate the in-flight key via _locks/_epochs, so the result
    # is not cached.
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def fetch():
        started.set()
        await proceed.wait()
        return ["msg"]

    async def racer():
        await started.wait()
        c.invalidate_if(lambda k: k[0] == 7)  # matches the in-flight key, not yet in _store
        proceed.set()

    key = (7, 50, 0)
    result, _ = await asyncio.gather(c.get_or_fetch(key, fetch), racer())
    assert result == ["msg"]
    sentinel = object()
    assert c.get(key, sentinel) is sentinel  # not cached -> re-fetch next time


async def test_invalidate_all_during_first_fetch_of_new_key_is_not_re_cached():
    # wipe-all (invalidate(None)) racing the FIRST-EVER fetch of a key (no _epochs entry yet,
    # present only in _locks): the result must NOT be cached, mirroring the per-key path. This
    # is the production DM/small-group deletion path (_on_deleted wipe-all without chat_id).
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def fetch():
        started.set()
        await proceed.wait()
        return "fetched-value"

    async def racer():
        await started.wait()
        c.invalidate(None)  # wipe-all WHILE the brand-new key's first fetch is in flight
        proceed.set()

    result, _ = await asyncio.gather(c.get_or_fetch("brand-new-key", fetch), racer())
    assert result == "fetched-value"  # caller still gets the freshly-fetched value
    sentinel = object()
    assert c.get("brand-new-key", sentinel) is sentinel  # NOT cached -> miss


async def test_invalidate_of_other_key_during_fetch_still_caches():
    # per-key epoch (not global): invalidating a DIFFERENT key mid-fetch must NOT over-skip —
    # the in-flight key's value is cached normally. This fails under a global-epoch impl.
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, clock=_clock(t))
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def fetch():
        started.set()
        await proceed.wait()
        return "kept"

    async def racer():
        await started.wait()
        c.invalidate("other")  # unrelated key
        c.invalidate_if(lambda k: k == "another-unrelated")
        proceed.set()

    result, _ = await asyncio.gather(c.get_or_fetch("k", fetch), racer())
    assert result == "kept"
    assert c.get("k") == "kept"  # cached, because only OTHER keys were invalidated


def test_epochs_do_not_leak_after_invalidate_and_eviction():
    # _epochs must stay bounded: a plain invalidate of an absent key, and eviction, both prune.
    t = {"now": 0.0}
    c = TTLCache(ttl=10.0, maxsize=2, clock=_clock(t))
    c.invalidate("never-stored")  # bumps then prunes (not stored, no in-flight)
    assert "never-stored" not in c._epochs
    for i in range(5):
        c.set(i, i)  # evicts oldest beyond maxsize=2
    # epochs for evicted keys are pruned; at most the 2 live keys could carry one
    assert set(c._epochs) <= set(c._store)


# --- #183: bounded_remember — the shared echo-suppression primitive ---


def test_bounded_remember_marks_key_present():
    od: OrderedDict = OrderedDict()
    bounded_remember(od, (7, 5))
    assert (7, 5) in od
    assert od[(7, 5)] is True  # sentinel True, not None — so a later pop distinguishes a hit


def test_bounded_remember_moves_existing_key_to_end():
    od: OrderedDict = OrderedDict()
    bounded_remember(od, "a")
    bounded_remember(od, "b")
    bounded_remember(od, "a")  # re-touch the older key
    assert list(od) == ["b", "a"]  # "a" is now most-recent


def test_bounded_remember_drops_oldest_past_cap():
    od: OrderedDict = OrderedDict()
    for i in range(5):
        bounded_remember(od, i, cap=3)
    assert list(od) == [2, 3, 4]  # oldest (0, 1) evicted, newest 3 kept in order


def test_bounded_remember_default_cap_is_200():
    od: OrderedDict = OrderedDict()
    for i in range(250):
        bounded_remember(od, i)
    assert len(od) == 200
    assert list(od) == list(range(50, 250))  # the 200 most-recent
