import pytest

from tg_messenger.core.flood import (
    HandledFloodWaitError,
    is_transient_flood_wait_seconds,
    run_with_flood_wait_retry,
)


class FakeFloodWaitError(Exception):
    """Mimics telethon.errors.FloodWaitError (.seconds attribute)."""

    def __init__(self, seconds):
        super().__init__(f"flood {seconds}s")
        self.seconds = seconds


@pytest.fixture(autouse=True)
def _patch_flood_error(monkeypatch):
    # core.flood catches telethon's FloodWaitError; point it at our fake.
    import tg_messenger.core.flood as flood

    monkeypatch.setattr(flood, "FloodWaitError", FakeFloodWaitError)


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
    import tg_messenger.core.flood as flood

    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(flood.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeFloodWaitError(2)
        return "ok"

    result = await run_with_flood_wait_retry(flaky, operation="t")
    assert result == "ok"
    assert calls["n"] == 2
    assert slept  # it waited once


async def test_non_transient_raises_handled():
    async def big_flood():
        raise FakeFloodWaitError(9999)

    with pytest.raises(HandledFloodWaitError) as exc:
        await run_with_flood_wait_retry(big_flood, operation="t")
    assert exc.value.wait_seconds == 9999


async def test_non_flood_error_propagates():
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await run_with_flood_wait_retry(boom, operation="t")
