"""Exercise messenger with the real gate and real Telethon message pagination."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import types
from telethon_floodgate import RateLimitSpec, TelegramRateLimitGate

from tests.conftest import flood_wait_error
from tg_messenger.core.client import StandaloneTelegramClient


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class OfflineTelegram(TelegramClient):
    def __init__(self, clock):
        super().__init__(StringSession(), 1, "test", flood_sleep_threshold=0)
        self.clock = clock
        self.requests = []
        self.fail_on = None

    async def get_input_entity(self, peer):
        return types.InputPeerUser(7, 0)

    async def _get_peer(self, peer):
        return types.PeerUser(7)

    async def __call__(self, request, *args, **kwargs):
        self.requests.append((self.clock.now, getattr(request, "offset_id", None)))
        if len(self.requests) == self.fail_on:
            raise flood_wait_error(1)
        if hasattr(request, "id"):
            ids = [getattr(value, "id", value) for value in request.id]
        else:
            start = request.offset_id - 1 if request.offset_id else 250
            ids = range(start, max(0, start - request.limit), -1)
        return SimpleNamespace(
            count=250, users=[], chats=[], messages=[
                types.Message(
                    id=i, peer_id=types.PeerUser(7), message=str(i), date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ) for i in ids
            ],
        )


@pytest.fixture
def offline_client(tmp_path):
    clock = Clock()
    raw = OfflineTelegram(clock)
    client = StandaloneTelegramClient(
        1, "test", client_factory=lambda *args: raw, session_dir=tmp_path, clock=clock, sleep=clock.sleep,
    )
    client._gate = TelegramRateLimitGate(category_limits={"history": RateLimitSpec(1, 10)}, time_func=clock)
    return client, raw, clock


@pytest.mark.parametrize("method", ["history", "history_since", "search_messages"])
async def test_history_pages_are_individually_reserved(offline_client, method):
    client, raw, clock = offline_client
    args = (7, "test") if method == "search_messages" else (7,)
    result = await getattr(client, method)(*args, limit=250)
    assert len(result) == 250
    assert len({message.id for message in result}) == 250
    assert raw.requests == [(0, 0), (10, 151), (20, 51)]
    assert clock.sleeps == [10, 10]
    assert client._gate.try_acquire("default", "history") == 10
    if method == "history":
        assert await client.history(7, limit=250) == result
        assert len(raw.requests) == 3  # cache hit consumes no slot


async def test_message_id_reads_use_same_gate(offline_client, tmp_path):
    client, raw, clock = offline_client
    assert await client.download_message_media(7, 250, tmp_path) is None
    assert await client.download_message_media(7, 249, tmp_path) is None
    assert raw.requests == [(0, None), (10, None)]
    assert clock.sleeps == [10]


async def test_flood_on_middle_page_retries_with_fresh_reservations(offline_client, monkeypatch):
    import telethon_floodgate.flood_wait as flood

    client, raw, clock = offline_client
    raw.fail_on = 2

    async def sleep(info, **kwargs):
        await clock.sleep(info.wait_seconds + flood.FLOOD_WAIT_RETRY_BUFFER_SEC)

    monkeypatch.setattr(flood, "sleep_for_handled_flood_wait", sleep)
    result = await client.history(7, limit=250)
    assert [message.id for message in result] == list(range(1, 251))
    assert raw.requests == [(0, 0), (10, 151), (20, 0), (30, 151), (40, 51)]
    assert clock.sleeps == [10, 2, 8, 10, 10]


@pytest.mark.parametrize("operation,method", [
    ("send_text", "send_message"), ("send_media", "send_file"),
    ("forward", "forward_messages"), ("edit_text", "edit_message"),
])
@pytest.mark.parametrize("send_rate", [20, 0])
async def test_every_send_retry_is_gated_unless_opted_out(
    fake_client, tmp_path, monkeypatch, operation, method, send_rate,
):
    import telethon_floodgate.flood_wait as flood

    clock = Clock()
    client = StandaloneTelegramClient(
        1, "test", client_factory=lambda *args: fake_client, session_dir=tmp_path,
        clock=clock, sleep=clock.sleep, send_rate_per_min=send_rate,
    )
    client._gate = TelegramRateLimitGate(category_limits={"send": RateLimitSpec(1, 10)}, time_func=clock)
    original = getattr(fake_client, method)
    attempts = []

    async def flaky(*args, **kwargs):
        attempts.append(clock.now)
        if len(attempts) == 1:
            raise flood_wait_error(1)
        return await original(*args, **kwargs)

    async def sleep(info, **kwargs):
        await clock.sleep(info.wait_seconds + flood.FLOOD_WAIT_RETRY_BUFFER_SEC)

    monkeypatch.setattr(fake_client, method, flaky)
    monkeypatch.setattr(flood, "sleep_for_handled_flood_wait", sleep)
    media = tmp_path / "test.jpg"
    media.write_bytes(b"test")
    args = {"send_text": (7, "text"), "send_media": (7, media), "forward": (7, [1], 8), "edit_text": (7, 1, "text")}
    await getattr(client, operation)(*args[operation])
    assert attempts == ([0, 10] if send_rate else [0, 2])
    assert clock.sleeps == ([2, 8] if send_rate else [2])


async def test_default_bucket_and_gate_share_injected_time(fake_client, tmp_path):
    clock = Clock()
    client = StandaloneTelegramClient(
        1, "test", client_factory=lambda *args: fake_client, session_dir=tmp_path, clock=clock, sleep=clock.sleep,
        gate_jitter_func=lambda low, high: 0,  # exact defer math below
    )
    for i in range(31):
        await client.send_text(7, str(i))
    assert len(fake_client.sent) == 31
    assert clock.sleeps == [3] * 11 + [27]  # 20-token burst, then 30/min sliding-window gate
    assert clock.now == 60
