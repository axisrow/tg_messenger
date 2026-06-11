"""TokenBucket — global outgoing rate limiter (one cap across agent/heartbeat/moderator/worker).

stdlib-only, ``clock`` injected (tests advance a fake clock, never sleep for real).
``acquire()`` never errors — it WAITS for the next token (a send is slower, not lost).
The wait is exercised by injecting both the clock and the sleep function: the sleep
spy records how long acquire would have slept, and advances the fake clock by that much.
"""

from __future__ import annotations

import asyncio

from tg_messenger.core.ratelimit import TokenBucket


def _fake_time():
    t = {"now": 0.0}
    return t


def _make_sleeper(t):
    """A fake async sleep: records durations and advances the fake clock."""
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        t["now"] += seconds

    return sleep, slept


# --- цикл 127: TokenBucket ---

async def test_burst_consumed_instantly():
    t = _fake_time()
    sleep, slept = _make_sleeper(t)
    bucket = TokenBucket(rate_per_min=60, burst=3, clock=lambda: t["now"], sleep=sleep)
    # 3 tokens available immediately → no sleeping
    for _ in range(3):
        await bucket.acquire()
    assert slept == []


async def test_exhaustion_waits_for_next_token():
    t = _fake_time()
    sleep, slept = _make_sleeper(t)
    # 60/min = 1 token/sec; burst 1 → after the first, the next waits ~1s
    bucket = TokenBucket(rate_per_min=60, burst=1, clock=lambda: t["now"], sleep=sleep)
    await bucket.acquire()  # instant
    await bucket.acquire()  # must wait ~1s
    assert len(slept) == 1
    assert abs(slept[0] - 1.0) < 0.01


async def test_refill_over_time():
    t = _fake_time()
    sleep, slept = _make_sleeper(t)
    bucket = TokenBucket(rate_per_min=60, burst=1, clock=lambda: t["now"], sleep=sleep)
    await bucket.acquire()
    t["now"] += 5.0  # 5 seconds pass → tokens refilled (capped at burst)
    await bucket.acquire()  # token available again → no sleep
    assert slept == []


async def test_rate_zero_is_noop():
    t = _fake_time()
    sleep, slept = _make_sleeper(t)
    bucket = TokenBucket(rate_per_min=0, burst=1, clock=lambda: t["now"], sleep=sleep)
    for _ in range(100):
        await bucket.acquire()
    assert slept == []  # disabled → never waits


async def test_concurrent_acquire_is_serialised():
    t = _fake_time()
    sleep, slept = _make_sleeper(t)
    bucket = TokenBucket(rate_per_min=60, burst=1, clock=lambda: t["now"], sleep=sleep)
    # 5 concurrent acquires, burst 1 → 4 of them must wait, total ~4s
    await asyncio.gather(*(bucket.acquire() for _ in range(5)))
    assert len(slept) == 4
    assert abs(sum(slept) - 4.0) < 0.05
