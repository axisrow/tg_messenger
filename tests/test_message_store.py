from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from tg_messenger.core.message_store import (
    MESSAGE_STORE_PEER_STATE_MAX,
    MessageStore,
    register_message_store_migrations,
)
from tg_messenger.core.models import Message, MessagesDeletedEvent, User
from tg_messenger.core.storage import Storage


def _msg(mid: int, dialog_id: int = 7, *, out: bool = False, text: str | None = None) -> Message:
    return Message(
        id=mid,
        dialog_id=dialog_id,
        sender_id=1 if out else dialog_id,
        out=out,
        text=text or f"m{mid}",
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class StoreClient:
    def __init__(self):
        self.calls: list[tuple[int, int, int]] = []
        self.messages = [_msg(3), _msg(2), _msg(1)]  # newest first, like Telethon

    async def history_since(self, peer: int, min_id: int = 0, limit: int = 50):
        self.calls.append((peer, min_id, limit))
        newest = [m for m in self.messages if m.id > min_id][:limit]
        return list(reversed(newest))

    async def listen_all(self):
        raise AssertionError("not used")
        yield

    async def listen_outgoing(self):
        raise AssertionError("not used")
        yield

    async def listen_deleted(self):
        raise AssertionError("not used")
        yield


class InterruptingStoreClient(StoreClient):
    async def listen_all(self):
        if False:
            yield None
        raise KeyboardInterrupt

    async def listen_outgoing(self):
        await asyncio.Event().wait()
        yield

    async def listen_deleted(self):
        await asyncio.Event().wait()
        yield


class BlockingConnectStorage:
    def __init__(self):
        self.connect_started = asyncio.Event()
        self.release_connect = asyncio.Event()
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connect_started.set()
        await self.release_connect.wait()
        self.connected = True

    async def close(self):
        self.closed = True
        self.connected = False


async def _storage(tmp_path):
    storage = Storage(tmp_path / "messages.db")
    register_message_store_migrations(storage)
    return storage


async def test_message_store_run_handles_keyboard_interrupt(tmp_path, caplog):
    store = MessageStore(client=InterruptingStoreClient(), storage=await _storage(tmp_path))
    try:
        with caplog.at_level(logging.INFO, logger="tg_messenger.core.message_store"):
            await store.run()
    finally:
        await store.close()
    assert any("message store interrupted" in rec.message for rec in caplog.records)


async def test_message_store_close_waits_for_connect_in_progress():
    storage = BlockingConnectStorage()
    store = MessageStore(client=StoreClient(), storage=storage)

    connect_task = asyncio.create_task(store.connect())
    await storage.connect_started.wait()
    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert storage.closed is False

    storage.release_connect.set()
    await asyncio.gather(connect_task, close_task)

    assert storage.closed is True
    assert storage.connected is False


async def test_consumer_cancel_returns_clean_shutdown(tmp_path):
    store = MessageStore(client=StoreClient(), storage=await _storage(tmp_path))
    started = asyncio.Event()

    async def consume():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(store._consume_until_done(consume))
    await started.wait()
    task.cancel()

    assert await task is True


async def test_message_store_first_load_then_cooldown_serves_db(tmp_path):
    t = {"now": 0.0}
    client = StoreClient()
    store = MessageStore(client=client, storage=await _storage(tmp_path), clock=lambda: t["now"])
    try:
        first = await store.history(7, limit=50)
        second = await store.history(7, limit=50)
        t["now"] = 16.0
        client.messages.insert(0, _msg(4))
        third = await store.history(7, limit=50)
    finally:
        await store.close()

    assert [m.id for m in first] == [1, 2, 3]
    assert [m.id for m in second] == [1, 2, 3]
    assert [m.id for m in third] == [1, 2, 3, 4]
    assert client.calls == [(7, 0, 50), (7, 3, 50), (7, 0, 50)]


class SenderStoreClient(StoreClient):
    """A group history whose messages carry a resolved author."""

    def __init__(self):
        super().__init__()
        self.messages = [
            Message(id=2, dialog_id=-100200, sender_id=9, out=False, text="m2",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    sender=User(id=9, username="bob", first_name="Bob", last_name="Lee")),
            Message(id=1, dialog_id=-100200, sender_id=5, out=False, text="m1",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc)),  # no sender resolved
        ]


async def test_message_store_round_trips_sender(tmp_path):
    # #108 (Codex review): the store-backed path (default serve/tui) must preserve the author,
    # so a group history shows 'userid @username First Last', not just the bare id.
    store = MessageStore(client=SenderStoreClient(), storage=await _storage(tmp_path))
    try:
        await store.history(-100200, limit=50)  # first load → persisted to SQLite
        # second read is served from the DB (within cooldown) — sender must survive the round trip
        items = await store.history(-100200, limit=50)
    finally:
        await store.close()
    by_id = {m.id: m for m in items}
    assert by_id[2].sender is not None
    assert (by_id[2].sender.username, by_id[2].sender.first_name, by_id[2].sender.last_name) \
        == ("bob", "Bob", "Lee")
    assert by_id[2].sender.id == 9
    assert by_id[1].sender is None  # no enrichment persisted → reconstructed as None
    assert by_id[1].sender_id == 5  # bare id still present


async def test_message_store_resets_window_on_possible_gap(tmp_path):
    t = {"now": 0.0}
    client = StoreClient()
    store = MessageStore(client=client, storage=await _storage(tmp_path), clock=lambda: t["now"])
    try:
        assert [m.id for m in await store.history(7, limit=3)] == [1, 2, 3]
        t["now"] = 16.0
        client.messages = [_msg(8), _msg(7), _msg(6), _msg(5), _msg(4), _msg(3), _msg(2), _msg(1)]
        assert [m.id for m in await store.history(7, limit=3)] == [6, 7, 8]
    finally:
        await store.close()


async def test_message_store_backfills_when_requested_window_grows(tmp_path):
    t = {"now": 0.0}
    client = StoreClient()
    client.messages = [_msg(i) for i in range(200, 0, -1)]  # newest first, like Telethon
    store = MessageStore(client=client, storage=await _storage(tmp_path), clock=lambda: t["now"])
    try:
        first = await store.history(7, limit=50)
        second = await store.history(7, limit=200)
    finally:
        await store.close()

    assert [m.id for m in first] == list(range(151, 201))
    assert [m.id for m in second] == list(range(1, 201))
    assert client.calls == [(7, 0, 50), (7, 0, 200)]


async def test_message_store_window_excludes_live_ingest_above_unsynced_gap(tmp_path):
    t = {"now": 0.0}
    client = StoreClient()
    client.messages = [_msg(i) for i in range(100, 0, -1)]  # newest first, like Telethon
    store = MessageStore(client=client, storage=await _storage(tmp_path), clock=lambda: t["now"])
    try:
        assert [m.id for m in await store.history(7, limit=50)] == list(range(51, 101))
        await store.ingest(_msg(151))
        assert [m.id for m in await store.history(7, limit=50)] == list(range(51, 101))
    finally:
        await store.close()


async def test_message_store_ingest_waits_for_peer_sync_lock(tmp_path):
    storage = await _storage(tmp_path)
    store = MessageStore(client=StoreClient(), storage=storage)
    await store.connect()
    lock = store._sync_locks.setdefault(7, asyncio.Lock())
    await lock.acquire()
    upsert_started = asyncio.Event()

    async def upsert(message, **kwargs):
        upsert_started.set()

    store._upsert_message = upsert
    task = asyncio.create_task(store.ingest(_msg(42)))
    await asyncio.sleep(0)
    assert upsert_started.is_set() is False

    lock.release()
    try:
        await task
    finally:
        await store.close()
    assert upsert_started.is_set() is True


async def test_record_outgoing_waits_for_peer_sync_lock(tmp_path):
    storage = await _storage(tmp_path)
    store = MessageStore(client=StoreClient(), storage=storage)
    await store.connect()
    lock = store._sync_locks.setdefault(7, asyncio.Lock())
    await lock.acquire()
    upsert_started = asyncio.Event()

    async def upsert(message, **kwargs):
        upsert_started.set()

    store._upsert_message = upsert
    task = asyncio.create_task(
        store.record_outgoing(
            7,
            _msg(42, out=True),
            source_text="привет",
            source_lang="ru",
        )
    )
    await asyncio.sleep(0)
    assert upsert_started.is_set() is False

    lock.release()
    try:
        await task
    finally:
        await store.close()
    assert upsert_started.is_set() is True


async def test_message_store_peer_state_is_lru_bounded(tmp_path):
    store = MessageStore(client=StoreClient(), storage=await _storage(tmp_path))
    try:
        for peer in range(MESSAGE_STORE_PEER_STATE_MAX + 5):
            store._peer_lock(peer)
            store._remember_sync(peer)
    finally:
        await store.close()
    assert len(store._sync_locks) == MESSAGE_STORE_PEER_STATE_MAX
    assert len(store._last_sync) == MESSAGE_STORE_PEER_STATE_MAX
    assert 0 not in store._sync_locks
    assert 0 not in store._last_sync
    assert (MESSAGE_STORE_PEER_STATE_MAX + 4) in store._sync_locks
    assert (MESSAGE_STORE_PEER_STATE_MAX + 4) in store._last_sync


async def test_message_store_prunes_cached_window_when_no_newer_messages(tmp_path):
    t = {"now": 0.0}
    client = StoreClient()
    store = MessageStore(client=client, storage=await _storage(tmp_path), clock=lambda: t["now"])
    try:
        assert [m.id for m in await store.history(7, limit=3)] == [1, 2, 3]
        t["now"] = 16.0
        client.messages = [_msg(3), _msg(1)]
        assert [m.id for m in await store.history(7, limit=3)] == [1, 3]
    finally:
        await store.close()

    assert client.calls == [(7, 0, 3), (7, 3, 3), (7, 0, 3)]


async def test_message_store_prunes_cached_window_when_newer_messages_arrive(tmp_path):
    t = {"now": 0.0}
    client = StoreClient()
    store = MessageStore(client=client, storage=await _storage(tmp_path), clock=lambda: t["now"])
    try:
        assert [m.id for m in await store.history(7, limit=3)] == [1, 2, 3]
        t["now"] = 16.0
        client.messages = [_msg(4), _msg(3), _msg(1)]
        assert [m.id for m in await store.history(7, limit=3)] == [1, 3, 4]
    finally:
        await store.close()

    assert client.calls == [(7, 0, 3), (7, 3, 3), (7, 0, 3)]


async def test_message_store_deletion_without_chat_id_skips_channels(tmp_path):
    storage = await _storage(tmp_path)
    store = MessageStore(client=StoreClient(), storage=storage, sync_ttl=0)
    try:
        await store.ingest(_msg(10, dialog_id=7))
        await store.ingest(_msg(10, dialog_id=-1001234567890))
        await store.apply_deletion(MessagesDeletedEvent(message_ids=[10]))
        rows = await storage.fetchall("SELECT dialog_id FROM messages ORDER BY dialog_id", ())
    finally:
        await store.close()
    assert [r[0] for r in rows] == [-1001234567890]


async def test_record_outgoing_preserves_original_after_echo_ingest(tmp_path):
    storage = await _storage(tmp_path)
    store = MessageStore(client=StoreClient(), storage=storage)
    sent = _msg(20, out=True, text="hello")
    try:
        await store.record_outgoing(7, sent, source_text="привет", source_lang="ru")
        await store.ingest(sent)
        row = await storage.fetchone(
            "SELECT translated_text, translated_lang FROM messages WHERE dialog_id = ? AND id = ?",
            (7, 20),
        )
    finally:
        await store.close()
    assert row == ("привет", "ru")
