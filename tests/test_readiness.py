"""Readiness uses real core subscriptions over the shared fake Telegram client."""

import asyncio
from datetime import datetime, timezone

import pytest

from tg_messenger.agent.config import AgentConfig
from tg_messenger.agent.runner import AgentRunner
from tg_messenger.core.client import StandaloneTelegramClient
from tg_messenger.core.models import Dialog, IncomingEvent, Message, MessagesDeletedEvent, OutgoingEvent, User
from tg_messenger.core.watch import DeletionWatcher


@pytest.mark.parametrize("stream_name,bus_name", [
    ("listen", "_bus"),
    ("listen_outgoing", "_bus_out"),
    ("listen_deleted", "_bus_deleted"),
])
async def test_client_forwards_subscription_readiness(fake_client, stream_name, bus_name):
    client = StandaloneTelegramClient(1, "test", client_factory=lambda *args: fake_client)
    bus = getattr(client, bus_name)
    ready = asyncio.Event()
    stream = getattr(client, stream_name)(on_subscribed=ready.set)
    waiting = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert bus.subscriber_count == 1
        assert not waiting.done()  # no event is needed to announce readiness
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await stream.aclose()
    assert bus.subscriber_count == 0


async def test_agent_ready_after_allowlist_and_subscription(fake_client, monkeypatch):
    client = StandaloneTelegramClient(1, "test", client_factory=lambda *args: fake_client)
    config = AgentConfig(model="test", allow_all=False, allow_usernames=frozenset({"ann"}))
    runner = AgentRunner(client, None, config=config)
    preparing, proceed, ready, handled = (asyncio.Event() for _ in range(4))
    received = []

    def event(text):
        return IncomingEvent(dialog_id=7, message=Message(
            id=1, dialog_id=7, sender_id=7, out=False, text=text,
            date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ))

    async def dialogs(dm_only=True):
        assert dm_only
        preparing.set()
        await proceed.wait()
        return [Dialog(id=7, title="Ann", username="ann")]

    async def handle(message, allowed):
        assert allowed == frozenset({7})
        received.append(message.message.text)
        handled.set()

    monkeypatch.setattr(client, "dialogs", dialogs)
    monkeypatch.setattr(runner, "_handle_event", handle)
    task = asyncio.create_task(runner.run(on_ready=ready.set))
    try:
        await asyncio.wait_for(preparing.wait(), 1)
        assert not ready.is_set()
        assert client._bus.subscriber_count == 0
        client._bus.publish(event("during setup"))
        proceed.set()
        await asyncio.wait_for(ready.wait(), 1)
        assert client._bus.subscriber_count == 1
        client._bus.publish(event("after readiness"))
        await asyncio.wait_for(handled.wait(), 1)
        assert received == ["after readiness"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert client._bus.subscriber_count == 0


async def test_watcher_ready_after_identity_and_both_subscriptions(fake_client, monkeypatch):
    client = StandaloneTelegramClient(1, "test", client_factory=lambda *args: fake_client)
    watcher = DeletionWatcher(client)
    preparing, proceed, ready, remembered, deleted = (asyncio.Event() for _ in range(5))
    counts = []

    async def get_me():
        preparing.set()
        await proceed.wait()
        return User(id=1, first_name="Me")

    def on_ready():
        counts.append((client._bus_out.subscriber_count, client._bus_deleted.subscriber_count))
        assert watcher._self_id == 1
        ready.set()

    def remember(event):
        assert event.message.id == 12
        remembered.set()

    async def handle_deleted(event):
        assert event.message_ids == [12]
        deleted.set()

    monkeypatch.setattr(client, "get_me", get_me)
    monkeypatch.setattr(watcher, "_remember", remember)
    monkeypatch.setattr(watcher, "_handle_deleted", handle_deleted)
    task = asyncio.create_task(watcher.run(on_ready=on_ready))
    try:
        await asyncio.wait_for(preparing.wait(), 1)
        assert not ready.is_set()
        assert client._bus_out.subscriber_count == client._bus_deleted.subscriber_count == 0
        proceed.set()
        await asyncio.wait_for(ready.wait(), 1)
        assert counts == [(1, 1)]
        client._bus_out.publish(OutgoingEvent(dialog_id=7, message=Message(
            id=12, dialog_id=7, sender_id=1, out=True, text="live",
            date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )))
        await asyncio.wait_for(remembered.wait(), 1)
        client._bus_deleted.publish(MessagesDeletedEvent(chat_id=7, message_ids=[12]))
        await asyncio.wait_for(deleted.wait(), 1)
        assert counts == [(1, 1)]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert client._bus_out.subscriber_count == client._bus_deleted.subscriber_count == 0
