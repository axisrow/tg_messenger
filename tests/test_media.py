from telethon.sessions import StringSession

from tests.conftest import FakeMessage
from tg_messenger.core.client import StandaloneTelegramClient
from tg_messenger.core.models import MediaRef, Message

VALID_SESSION = StringSession().save()


def _build(fake_client):
    return StandaloneTelegramClient(
        api_id=1, api_hash="h", external_session=VALID_SESSION,
        client_factory=lambda session, api_id, api_hash: fake_client,
    )


async def test_send_media_calls_send_file(fake_client, tmp_path):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    client = _build(fake_client)
    await client.connect()
    msg = await client.send_media(7, f, caption="look")
    assert fake_client.sent[-1]["file"] == str(f)
    assert fake_client.sent[-1]["caption"] == "look"
    assert isinstance(msg, Message)


async def test_download_media_writes_to_dest(fake_client, tmp_path):
    client = _build(fake_client)
    await client.connect()
    dest = tmp_path / "out.bin"
    msg = FakeMessage(id=50, sender_id=7, media=object())
    result = await client.download_media(msg, dest)
    assert result == str(dest)
    assert fake_client.downloads[-1]["dest"] == str(dest)


async def test_history_marks_media_messages(fake_client):
    fake_client.messages[7] = [FakeMessage(id=1, sender_id=7, text=None, media=object())]
    client = _build(fake_client)
    await client.connect()
    msgs = await client.history(7)
    assert isinstance(msgs[0].media, MediaRef)
    assert msgs[0].media.downloadable is True
