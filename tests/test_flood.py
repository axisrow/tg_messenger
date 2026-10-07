import pytest
import telethon_floodgate

from tests.conftest import flood_wait_error
from tg_messenger.core.flood import (
    HandledFloodWaitError,
    is_transient_flood_wait_seconds,
    run_with_flood_wait_retry,
)


def test_transient_classification():
    assert is_transient_flood_wait_seconds(5) is True
    assert is_transient_flood_wait_seconds(0) is False
    assert is_transient_flood_wait_seconds(None) is False
    assert is_transient_flood_wait_seconds(9999) is False


async def test_returns_result_without_error():
    async def ok():
        return "value"

    assert await run_with_flood_wait_retry(ok, operation="t") == "value"


async def test_retries_transient_then_succeeds(monkeypatch):
    import telethon_floodgate.flood_wait as flood

    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(flood.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise flood_wait_error(2)
        return "ok"

    result = await run_with_flood_wait_retry(flaky, operation="t")
    assert result == "ok"
    assert calls["n"] == 2
    assert slept  # it waited once


async def test_non_transient_raises_handled():
    async def big_flood():
        raise flood_wait_error(9999)

    with pytest.raises(HandledFloodWaitError) as exc:
        await run_with_flood_wait_retry(big_flood, operation="t")
    assert exc.value.wait_seconds == 9999


async def test_non_flood_error_propagates():
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await run_with_flood_wait_retry(boom, operation="t")


# telethon-floodgate budget accounting (0.1.3): each retry costs
# wait + RETRY_BUFFER_SEC (1s); the budget check is inclusive (equality retries).
@pytest.mark.parametrize(
    "wait,budget,calls,sleeps",
    [
        (60, 122, 3, [61.0, 61.0]),
        (60, 120, 2, [61.0]),
        (61, 120, 1, []),
        (2, 1, 1, []),
        (0, 2, 2, [2.0]),
    ],
)
async def test_packaged_retry_boundaries_preserve_error_contract(monkeypatch, wait, budget, calls, sleeps):
    import telethon_floodgate.flood_wait as flood

    attempts = 0
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    async def fail():
        nonlocal attempts
        attempts += 1
        raise flood_wait_error(wait)

    monkeypatch.setattr(flood.asyncio, "sleep", sleep)
    with pytest.raises(HandledFloodWaitError) as caught:
        await run_with_flood_wait_retry(fail, operation="history", transient_wait_budget_sec=budget)
    assert attempts == calls
    assert slept == sleeps
    assert caught.value.operation == "history"
    assert caught.value.wait_seconds == max(1, wait)
    assert caught.value.user_message == f"Telegram flood wait {max(1, wait)}s — try again later."
    assert isinstance(caught.value.__cause__, telethon_floodgate.HandledFloodWaitError)


async def test_budget_exhaustion_warns_before_raising(caplog):
    import logging

    async def fail():
        raise flood_wait_error(2)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(HandledFloodWaitError):
            await run_with_flood_wait_retry(fail, operation="history", transient_wait_budget_sec=1)
    assert "history: flood-wait budget exhausted, last transient wait 2s" in caplog.text


async def test_adapter_delegates_policy_and_logger_to_package(monkeypatch):
    import logging
    from unittest.mock import AsyncMock

    retry = AsyncMock(return_value="delegated")
    factory = AsyncMock()
    logger = logging.getLogger("custom")
    monkeypatch.setattr(telethon_floodgate, "run_with_flood_wait_retry", retry)
    assert await run_with_flood_wait_retry(
        factory, operation="op", logger_=logger, transient_wait_max_sec=7, transient_wait_budget_sec=9,
    ) == "delegated"
    retry.assert_awaited_once_with(
        factory, operation="op", logger_=logger, transient_wait_max_sec=7, transient_wait_budget_sec=9,
    )


def test_token_bucket_is_only_a_compatibility_import():
    from tg_messenger.core.ratelimit import TokenBucket

    assert TokenBucket is telethon_floodgate.TokenBucket
