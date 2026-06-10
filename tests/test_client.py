import asyncio
import logging

import pytest
from telethon.sessions import StringSession

from tests.conftest import FakeChannel, FakeChat, FakeDialog, FakeDocument, FakeMessage, FakeUser
from tg_messenger.core.client import StandaloneTelegramClient, _dialog_kind
from tg_messenger.core.models import (
    Dialog,
    IncomingEvent,
    Message,
    MessagesDeletedEvent,
    OutgoingEvent,
    User,
)

VALID_SESSION = StringSession().save()  # valid, empty session string


def _build(fake_client, **kw):
    return StandaloneTelegramClient(
        api_id=1,
        api_hash="h",
        client_factory=lambda session, api_id, api_hash: fake_client,
        **kw,
    )


def _seed_dm(fake_client):
    ann = FakeUser(id=7, first_name="Ann", username="ann")
    bob = FakeUser(id=8, first_name="Bob")
    chan = FakeChannel(id=100, title="News", username="news")
    fake_client.dialogs = [
        FakeDialog(ann, name="Ann", unread_count=2,
                   message=FakeMessage(id=5, sender_id=7, text="hey")),
        FakeDialog(bob, name="Bob"),
        FakeDialog(chan, name="News"),  # not a DM
    ]
    # Telethon iter_messages yields newest-first
    fake_client.messages[7] = [
        FakeMessage(id=2, sender_id=1, text="yo", out=True),
        FakeMessage(id=1, sender_id=7, text="hi", out=False),
    ]


async def test_connect_disconnect(fake_client):
    client = _build(fake_client)
    await client.connect()
    assert fake_client.connected is True
    await client.disconnect()
    assert fake_client.connected is False


async def test_dialogs_dm_only(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs(dm_only=True)
    assert all(isinstance(d, Dialog) for d in dialogs)
    ids = {d.id for d in dialogs}
    assert ids == {7, 8}  # channel 100 excluded
    ann = next(d for d in dialogs if d.id == 7)
    assert ann.title == "Ann"
    assert ann.unread == 2
    assert ann.last_text == "hey"
    assert ann.last_message_at is not None


# --- Цикл 32: dialogs(dm_only=False) — все виды, marked id ---


def _seed_all_kinds(fake_client):
    fake_client.dialogs = [
        FakeDialog(FakeUser(id=7, first_name="Ann", username="ann"), name="Ann"),
        FakeDialog(FakeUser(id=9, first_name="Helper", bot=True), name="HelperBot"),
        FakeDialog(FakeChat(id=50, title="Devs"), name="Devs"),
        FakeDialog(FakeChannel(id=123, title="News", broadcast=True), name="News"),
        FakeDialog(FakeChannel(id=200, title="SG", broadcast=False), name="SG"),
    ]


async def test_dialogs_all_returns_every_kind_with_marked_ids(fake_client):
    _seed_all_kinds(fake_client)
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs(dm_only=False)
    # id — marked (отрицательный для групп/каналов): совпадает с event.chat_id
    assert {d.id: d.kind for d in dialogs} == {
        7: "dm", 9: "bot", -50: "group", -100123: "channel", -100200: "group",
    }
    assert {d.id: d.title for d in dialogs}[-50] == "Devs"


async def test_dialogs_dm_only_excludes_bots_and_groups(fake_client):
    _seed_all_kinds(fake_client)
    client = _build(fake_client)
    await client.connect()
    dialogs = await client.dialogs(dm_only=True)
    assert [d.id for d in dialogs] == [7]
    assert dialogs[0].kind == "dm"


# --- Цикл 31: классификатор вида диалога ---


@pytest.mark.parametrize(
    ("entity", "kind"),
    [
        (FakeUser(id=7, first_name="Ann"), "dm"),
        (FakeUser(id=9, first_name="Helper", bot=True), "bot"),
        (FakeChat(id=50, title="Devs"), "group"),  # малая группа: title, без broadcast
        (FakeChannel(id=200, title="SG", broadcast=False), "group"),  # супергруппа
        (FakeChannel(id=123, title="News", broadcast=True), "channel"),  # бродкаст
    ],
)
def test_dialog_kind_classifier(entity, kind):
    assert _dialog_kind(entity) == kind


def test_unknown_entity_is_not_dm():
    # fail-safe прежней _is_dm_entity-семантики: ни title, ни имён → НЕ DM
    assert _dialog_kind(object()) != "dm"


async def test_history_maps_messages(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    msgs = await client.history(7, limit=10)
    assert all(isinstance(m, Message) for m in msgs)
    # chronological order (oldest first), regardless of Telethon's newest-first
    assert [m.text for m in msgs] == ["hi", "yo"]
    assert msgs[1].out is True


def test_media_ref_voice_wins_over_document():
    # Telethon: a voice note is a document with voice=True — .voice must be checked first
    doc = FakeDocument(file_name="note.ogg", size=2048)
    raw = FakeMessage(id=1, sender_id=7, voice=doc,
                      file=FakeDocument(file_name="note.ogg", size=2048, mime_type="audio/ogg"))
    ref = StandaloneTelegramClient._to_media_ref(raw)
    assert ref.kind == "voice"
    assert ref.mime_type == "audio/ogg"


def test_media_ref_photo_carries_mime_type():
    raw = FakeMessage(id=1, sender_id=7, photo=object(),
                      file=FakeDocument(mime_type="image/jpeg"))
    ref = StandaloneTelegramClient._to_media_ref(raw)
    assert ref.kind == "photo"
    assert ref.mime_type == "image/jpeg"


def test_media_ref_without_file_has_no_mime_type():
    raw = FakeMessage(id=1, sender_id=7, photo=object())
    ref = StandaloneTelegramClient._to_media_ref(raw)
    assert ref.kind == "photo"
    assert ref.mime_type is None


async def test_send_text_records_and_returns_message(fake_client):
    client = _build(fake_client)
    await client.connect()
    msg = await client.send_text(7, "hello")
    assert fake_client.sent[-1] == {"peer": 7, "text": "hello"}
    assert isinstance(msg, Message)
    assert msg.out is True


async def test_listen_yields_incoming(fake_client):
    client = _build(fake_client)
    await client.connect()

    # Build a fake NewMessage event that points at dialog 7
    event = type("Evt", (), {})()
    event.chat_id = 7
    event.is_private = True
    event.message = FakeMessage(id=50, sender_id=7, text="ping", out=False)

    received = []

    async def consume():
        async for ev in client.listen():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(event)
    await asyncio.wait_for(task, timeout=1)
    assert isinstance(received[0], IncomingEvent)
    assert received[0].message.text == "ping"
    assert received[0].dialog_id == 7


async def test_listen_skips_non_private_chats(fake_client):
    client = _build(fake_client)
    await client.connect()

    group_event = type("Evt", (), {})()
    group_event.chat_id = -100123
    group_event.is_private = False
    group_event.message = FakeMessage(id=51, sender_id=9, text="group noise", out=False)

    dm_event = type("Evt", (), {})()
    dm_event.chat_id = 7
    dm_event.is_private = True
    dm_event.message = FakeMessage(id=52, sender_id=7, text="dm", out=False)

    received = []

    async def consume():
        async for ev in client.listen():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(group_event)
    await fake_client.push_event(dm_event)
    await asyncio.wait_for(task, timeout=1)
    assert [ev.message.text for ev in received] == ["dm"]


async def test_listen_handler_error_is_logged_not_raised(fake_client, caplog):
    client = _build(fake_client)
    await client.connect()

    broken_event = type("Evt", (), {})()
    broken_event.chat_id = 7
    broken_event.is_private = True
    broken_event.message = object()  # no .date -> mapping blows up

    with caplog.at_level("ERROR", logger="tg_messenger.core.client"):
        await fake_client.push_event(broken_event)  # must not raise

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a broken incoming event must be logged"
    assert errors[0].exc_info is not None  # traceback recorded


# --- Цикл 27: поток своих сообщений (listen_outgoing) + get_me ---


def _evt(chat_id, *, is_private, message):
    event = type("Evt", (), {})()
    event.chat_id = chat_id
    event.is_private = is_private
    event.message = message
    return event


async def test_listen_outgoing_yields_own_group_messages(fake_client):
    # is_private=False НЕ фильтруется: группы — суть фичи watch
    client = _build(fake_client)
    await client.connect()
    event = _evt(-100123, is_private=False,
                 message=FakeMessage(id=60, sender_id=1, text="моё в группе", out=True))

    received = []

    async def consume():
        async for ev in client.listen_outgoing():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(event)
    await asyncio.wait_for(task, timeout=1)
    assert isinstance(received[0], OutgoingEvent)
    assert received[0].dialog_id == -100123
    assert received[0].message.text == "моё в группе"
    assert received[0].message.out is True


async def test_outgoing_and_incoming_streams_do_not_cross(fake_client):
    client = _build(fake_client)
    await client.connect()
    out_event = _evt(7, is_private=True,
                     message=FakeMessage(id=61, sender_id=1, text="своё", out=True))
    in_event = _evt(7, is_private=True,
                    message=FakeMessage(id=62, sender_id=7, text="чужое", out=False))

    incoming, outgoing = [], []

    async def consume_in():
        async for ev in client.listen():
            incoming.append(ev)
            return

    async def consume_out():
        async for ev in client.listen_outgoing():
            outgoing.append(ev)
            return

    tasks = [asyncio.create_task(consume_in()), asyncio.create_task(consume_out())]
    await asyncio.sleep(0)
    await fake_client.push_event(out_event)
    await fake_client.push_event(in_event)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert [ev.message.text for ev in incoming] == ["чужое"]
    assert [ev.message.text for ev in outgoing] == ["своё"]


async def test_outgoing_handler_error_is_logged_not_raised(fake_client, caplog):
    client = _build(fake_client)
    await client.connect()
    broken = _evt(7, is_private=True, message=type("Brk", (), {"out": True})())  # нет .date

    with caplog.at_level("ERROR", logger="tg_messenger.core.client"):
        await fake_client.push_event(broken)  # must not raise

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_get_me_returns_user(fake_client):
    client = _build(fake_client)
    await client.connect()
    me = await client.get_me()
    assert isinstance(me, User)
    assert me.id == 1
    assert me.username == "me"


# --- Цикл 33: listen_all — входящие из всех чатов (вкладка «Группы») ---


async def test_listen_all_yields_group_and_dm(fake_client):
    client = _build(fake_client)
    await client.connect()
    group_event = _evt(-100200, is_private=False,
                       message=FakeMessage(id=80, sender_id=9, text="из группы", out=False))
    dm_event = _evt(7, is_private=True,
                    message=FakeMessage(id=81, sender_id=7, text="из ЛС", out=False))

    received = []

    async def consume():
        async for ev in client.listen_all():
            received.append(ev)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(group_event)
    await fake_client.push_event(dm_event)
    await asyncio.wait_for(task, timeout=1)
    assert [(ev.dialog_id, ev.message.text) for ev in received] == [
        (-100200, "из группы"), (7, "из ЛС"),
    ]


async def test_listen_all_does_not_leak_groups_into_listen(fake_client):
    client = _build(fake_client)
    await client.connect()
    group_event = _evt(-100200, is_private=False,
                       message=FakeMessage(id=82, sender_id=9, text="группа", out=False))
    dm_event = _evt(7, is_private=True,
                    message=FakeMessage(id=83, sender_id=7, text="лс", out=False))

    dm_stream, all_stream = [], []

    async def consume_dm():
        async for ev in client.listen():
            dm_stream.append(ev)
            return

    async def consume_all():
        async for ev in client.listen_all():
            all_stream.append(ev)
            if len(all_stream) == 2:
                return

    tasks = [asyncio.create_task(consume_dm()), asyncio.create_task(consume_all())]
    await asyncio.sleep(0)
    await fake_client.push_event(group_event)
    await fake_client.push_event(dm_event)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert [ev.message.text for ev in dm_stream] == ["лс"]
    assert [ev.message.text for ev in all_stream] == ["группа", "лс"]


async def test_broken_group_event_is_logged_not_raised(fake_client, caplog):
    # групповые события больше не дропаются до try — сбой обязан попасть в лог
    client = _build(fake_client)
    await client.connect()
    broken = _evt(-100200, is_private=False, message=object())  # нет .date

    with caplog.at_level("ERROR", logger="tg_messenger.core.client"):
        await fake_client.push_event(broken)  # must not raise

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


# --- Цикл 28: поток удалений (listen_deleted) + entity_title ---


def _deleted_evt(ids, chat_id=None):
    event = type("Del", (), {})()
    event.deleted_ids = list(ids)
    if chat_id is not None:
        event.chat_id = chat_id
    return event


async def test_listen_deleted_supergroup_carries_chat_id(fake_client):
    client = _build(fake_client)
    await client.connect()

    received = []

    async def consume():
        async for ev in client.listen_deleted():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(_deleted_evt([50, 51], chat_id=-1001234567890))
    await asyncio.wait_for(task, timeout=1)
    assert isinstance(received[0], MessagesDeletedEvent)
    assert received[0].chat_id == -1001234567890
    assert received[0].message_ids == [50, 51]


async def test_listen_deleted_private_has_no_chat_id(fake_client):
    # Telegram не сообщает чат для ЛС/малых групп — chat_id остаётся None
    client = _build(fake_client)
    await client.connect()

    received = []

    async def consume():
        async for ev in client.listen_deleted():
            received.append(ev)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await fake_client.push_event(_deleted_evt([60]))
    await asyncio.wait_for(task, timeout=1)
    assert received[0].chat_id is None
    assert received[0].message_ids == [60]


async def test_deleted_event_does_not_leak_into_message_streams(fake_client):
    client = _build(fake_client)
    await client.connect()

    incoming, deleted = [], []

    async def consume_in():
        async for ev in client.listen():
            incoming.append(ev)
            return

    async def consume_del():
        async for ev in client.listen_deleted():
            deleted.append(ev)
            return

    in_task = asyncio.create_task(consume_in())
    del_task = asyncio.create_task(consume_del())
    await asyncio.sleep(0)
    await fake_client.push_event(_deleted_evt([70]))
    dm = _evt(7, is_private=True, message=FakeMessage(id=71, sender_id=7, text="dm", out=False))
    await fake_client.push_event(dm)
    await asyncio.wait_for(asyncio.gather(in_task, del_task), timeout=1)
    assert [ev.message.text for ev in incoming] == ["dm"]  # deleted-событие не утекло
    assert deleted[0].message_ids == [70]


async def test_entity_title_prefers_group_title(fake_client):
    _seed_dm(fake_client)  # содержит FakeChannel(id=100, title="News")
    client = _build(fake_client)
    await client.connect()
    assert await client.entity_title(100) == "News"


async def test_entity_title_falls_back_to_user_name(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    assert await client.entity_title(7) == "Ann"


async def test_download_message_media_by_id(fake_client, tmp_path):
    fake_client.messages[7] = [FakeMessage(id=42, sender_id=7, text=None, media=object())]
    client = _build(fake_client)
    await client.connect()
    dest = tmp_path / "out.bin"
    result = await client.download_message_media(7, 42, dest)
    assert result == str(dest)
    assert fake_client.downloads[-1]["message_id"] == 42


async def test_external_session_writes_no_files(fake_client, session_dir):
    client = _build(
        fake_client, session_name="acc", external_session=VALID_SESSION, session_dir=session_dir
    )
    await client.connect()
    assert list(session_dir.iterdir()) == []


async def test_typing_proxies_to_telethon_action(fake_client):
    client = _build(fake_client)
    async with client.typing(7):
        assert fake_client.actions_active == [(7, "typing")]
    assert fake_client.actions_active == []  # выключился на выходе
    assert fake_client.actions_log == [(7, "typing")]


async def test_typing_enter_failure_is_swallowed_and_logged(fake_client, caplog):
    client = _build(fake_client)

    def broken_action(entity, action):
        raise RuntimeError("entity not found")

    fake_client.action = broken_action
    body_ran = False
    with caplog.at_level(logging.WARNING, logger="tg_messenger.core.client"):
        async with client.typing(7):
            body_ran = True  # тело выполняется несмотря на сбой индикатора
    assert body_ran
    assert any("typing" in r.message for r in caplog.records)


async def test_typing_exit_failure_is_swallowed_and_logged(fake_client, caplog):
    client = _build(fake_client)

    class _BrokenExit:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            raise RuntimeError("cancel failed")

    fake_client.action = lambda entity, action: _BrokenExit()
    with caplog.at_level(logging.WARNING, logger="tg_messenger.core.client"):
        async with client.typing(7):
            pass
    assert any("typing" in r.message for r in caplog.records)


async def test_typing_propagates_body_exceptions(fake_client):
    client = _build(fake_client)
    try:
        async with client.typing(7):
            raise RuntimeError("body failed")
    except RuntimeError as exc:
        assert str(exc) == "body failed"
    else:
        raise AssertionError("body exception must propagate")
    assert fake_client.actions_active == []  # индикатор всё равно погашен
