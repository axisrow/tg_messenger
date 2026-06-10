"""Цикл 29: DeletionWatcher — кэш своих сообщений + уведомления об удалениях.

Стаб core-клиента поверх asyncio.Queue: тест толкает OutgoingEvent /
MessagesDeletedEvent и смотрит, что ушло в Saved Messages (sent-журнал).
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from tg_messenger.core.models import Message, MessagesDeletedEvent, OutgoingEvent, User
from tg_messenger.core.watch import MEDIA_PLACEHOLDER, DeletionWatcher

SELF_ID = 999
SUPERGROUP = -1001234567890  # marked id канала/супергруппы (< -10^12)
SMALL_GROUP = -4123          # малая группа: chat_id в событии удаления НЕ приходит


def msg(dialog_id, msg_id, text="текст", date=None):
    return Message(id=msg_id, dialog_id=dialog_id, sender_id=SELF_ID, out=True, text=text,
                   date=date or datetime(2024, 1, 1, 12, 30, tzinfo=timezone.utc))


def out_ev(dialog_id, msg_id, text="текст"):
    return OutgoingEvent(dialog_id=dialog_id, message=msg(dialog_id, msg_id, text))


class StubCoreClient:
    def __init__(self):
        self.out_q: asyncio.Queue = asyncio.Queue()
        self.del_q: asyncio.Queue = asyncio.Queue()
        self.sent = []
        self.titles = {SUPERGROUP: "My Group", SMALL_GROUP: "Small Talk"}
        self.title_error = False
        self.send_fail_times = 0

    async def get_me(self):
        return User(id=SELF_ID, first_name="Me")

    async def listen_outgoing(self):
        while True:
            yield await self.out_q.get()

    async def listen_deleted(self):
        while True:
            yield await self.del_q.get()

    async def send_text(self, peer, text):
        if self.send_fail_times:
            self.send_fail_times -= 1
            raise RuntimeError("telegram down")
        self.sent.append((peer, text))
        return msg(peer, 900, text)

    async def entity_title(self, peer):
        if self.title_error:
            raise RuntimeError("no entity")
        return self.titles[peer]


async def _spin(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def running(watcher):
    task = asyncio.create_task(watcher.run())
    await _spin()
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def make(**kw):
    client = StubCoreClient()
    echoed = []
    watcher = DeletionWatcher(client, echo=echoed.append, **kw)
    return client, watcher, echoed


async def test_supergroup_deletion_is_backed_up_to_saved_messages():
    client, watcher, echoed = make()
    async with running(watcher):
        client.out_q.put_nowait(out_ev(SUPERGROUP, 50, "важное сообщение"))
        await _spin()
        client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[50]))
        await _spin()
    (peer, text), = client.sent
    assert peer == SELF_ID  # Saved Messages = свой id
    assert "My Group" in text
    assert str(SUPERGROUP) in text
    assert "2024-01-01 12:30" in text
    assert "важное сообщение" in text
    assert echoed  # CLI-summary напечатан


async def test_repeated_event_does_not_duplicate_notification():
    client, watcher, _ = make()
    async with running(watcher):
        client.out_q.put_nowait(out_ev(SUPERGROUP, 50))
        await _spin()
        event = MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[50])
        client.del_q.put_nowait(event)
        await _spin()
        client.del_q.put_nowait(event)  # Telegram может прислать повторно
        await _spin()
    assert len(client.sent) == 1  # матч удаляет запись из кэша


async def test_private_deletion_without_chat_id_matches_cache():
    client, watcher, _ = make()
    async with running(watcher):
        client.out_q.put_nowait(out_ev(SMALL_GROUP, 60, "в малой группе"))
        await _spin()
        client.del_q.put_nowait(MessagesDeletedEvent(message_ids=[60]))  # chat_id неизвестен
        await _spin()
    (peer, text), = client.sent
    assert "Small Talk" in text and "в малой группе" in text


async def test_no_chat_id_never_matches_supergroup_cache_entry():
    # пер-канальные id пересекаются с глобальными: событие без chat_id
    # не должно матчить закэшированное сообщение из супергруппы с тем же id
    client, watcher, _ = make()
    async with running(watcher):
        client.out_q.put_nowait(out_ev(SUPERGROUP, 70))
        await _spin()
        client.del_q.put_nowait(MessagesDeletedEvent(message_ids=[70]))
        await _spin()
    assert client.sent == []


async def test_foreign_deletion_is_ignored(caplog):
    # в супергруппе событие приходит на ЛЮБОЕ удаление — чужие фильтрует кэш
    client, watcher, _ = make()
    with caplog.at_level(logging.DEBUG, logger="tg_messenger.core.watch"):
        async with running(watcher):
            client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[80]))
            await _spin()
    assert client.sent == []
    assert any("no cached" in r.message for r in caplog.records)  # не молча


async def test_saved_messages_are_not_cached_no_notification_loop():
    client, watcher, _ = make()
    async with running(watcher):
        client.out_q.put_nowait(out_ev(SELF_ID, 90, "уведомление"))  # свой диалог
        await _spin()
        client.del_q.put_nowait(MessagesDeletedEvent(message_ids=[90]))
        await _spin()
    assert client.sent == []


async def test_batch_deletion_sends_one_notification_per_dialog():
    client, watcher, _ = make()
    async with running(watcher):
        client.out_q.put_nowait(out_ev(SUPERGROUP, 50, "первое"))
        client.out_q.put_nowait(out_ev(SUPERGROUP, 51, "второе"))
        await _spin()
        client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[50, 51]))
        await _spin()
    (peer, text), = client.sent  # одно сообщение, обе строки
    assert "первое" in text and "второе" in text


async def test_cache_is_bounded_oldest_evicted():
    client, watcher, _ = make(cache_size=2)
    async with running(watcher):
        for i in (1, 2, 3):
            client.out_q.put_nowait(out_ev(SUPERGROUP, i, f"msg{i}"))
        await _spin()
        client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[1]))
        await _spin()
    assert client.sent == []  # самое старое вытеснено


async def test_title_failure_falls_back_to_dialog_id(caplog):
    client, watcher, _ = make()
    client.title_error = True
    with caplog.at_level(logging.WARNING, logger="tg_messenger.core.watch"):
        async with running(watcher):
            client.out_q.put_nowait(out_ev(SUPERGROUP, 50, "текст"))
            await _spin()
            client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[50]))
            await _spin()
    (peer, text), = client.sent  # уведомление дошло несмотря на сбой title
    assert str(SUPERGROUP) in text
    assert any("title" in r.message for r in caplog.records)


async def test_send_failure_is_logged_and_loop_survives(caplog):
    client, watcher, _ = make()
    client.send_fail_times = 1
    with caplog.at_level(logging.ERROR, logger="tg_messenger.core.watch"):
        async with running(watcher):
            client.out_q.put_nowait(out_ev(SUPERGROUP, 50, "потеряно"))
            client.out_q.put_nowait(out_ev(SUPERGROUP, 51, "дошло"))
            await _spin()
            client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[50]))
            await _spin()
            client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[51]))
            await _spin()
    assert [t for _, t in client.sent if "дошло" in t]  # второе уведомление дошло
    assert any(r.exc_info for r in caplog.records)  # сбой первого — не молча


async def test_media_message_without_text_gets_placeholder():
    client, watcher, _ = make()
    async with running(watcher):
        event = OutgoingEvent(dialog_id=SUPERGROUP,
                              message=msg(SUPERGROUP, 50, text=None))
        client.out_q.put_nowait(event)
        await _spin()
        client.del_q.put_nowait(MessagesDeletedEvent(chat_id=SUPERGROUP, message_ids=[50]))
        await _spin()
    (_, text), = client.sent
    assert MEDIA_PLACEHOLDER in text
