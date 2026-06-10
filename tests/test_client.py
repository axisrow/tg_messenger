import asyncio

from telethon.sessions import StringSession

from tests.conftest import FakeChannel, FakeDialog, FakeMessage, FakeUser
from tg_messenger.core.client import StandaloneTelegramClient
from tg_messenger.core.models import Dialog, IncomingEvent, Message

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


async def test_history_maps_messages(fake_client):
    _seed_dm(fake_client)
    client = _build(fake_client)
    await client.connect()
    msgs = await client.history(7, limit=10)
    assert all(isinstance(m, Message) for m in msgs)
    # chronological order (oldest first), regardless of Telethon's newest-first
    assert [m.text for m in msgs] == ["hi", "yo"]
    assert msgs[1].out is True


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
