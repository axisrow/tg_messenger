from __future__ import annotations

from datetime import datetime, timezone

from tg_messenger.core.message_store import MessageStore, register_message_store_migrations
from tg_messenger.core.models import Message, MessagesDeletedEvent
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


async def _storage(tmp_path):
    storage = Storage(tmp_path / "messages.db")
    register_message_store_migrations(storage)
    return storage


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
    assert client.calls == [(7, 0, 50), (7, 3, 50)]


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
