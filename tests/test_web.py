from datetime import datetime, timezone

import httpx
import pytest_asyncio

from tg_messenger.core.events import EventBus
from tg_messenger.core.models import Dialog, IncomingEvent, Message
from tg_messenger.web.app import build_app


class WebStubClient:
    def __init__(self):
        self.bus = EventBus()
        self.sent = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def dialogs(self, dm_only=True):
        # повторяет контракт core: dm_only=False — все диалоги с kind и marked id
        dms = [Dialog(id=7, title="Ann", username="ann", unread=1)]
        if dm_only:
            return dms
        return dms + [
            Dialog(id=-100200, title="Devs", kind="group"),
            Dialog(id=-100123, title="News", kind="channel"),
            Dialog(id=9, title="HelperBot", kind="bot"),
        ]

    async def history(self, peer, limit=50, offset_id=0):
        return [Message(id=1, dialog_id=peer, sender_id=peer, out=False, text="hi",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def send_text(self, peer, text):
        self.sent.append((peer, text))
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def send_media(self, peer, file_path, caption=None):
        self.sent.append((peer, "media", caption))
        return Message(id=3, dialog_id=peer, sender_id=1, out=True, text=caption or "<media>",
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def listen_all(self):
        async for ev in self.bus.subscribe():
            yield ev


@pytest_asyncio.fixture
async def client_app():
    stub = WebStubClient()
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, stub


async def test_index_serves_html(client_app):
    ac, _ = client_app
    r = await ac.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_dialogs_fragment(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs")
    assert r.status_code == 200
    assert "Ann" in r.text
    assert "7" in r.text


async def test_dialogs_default_tab_is_dm(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs")
    assert r.status_code == 200
    assert "Ann" in r.text
    for non_dm in ("Devs", "News", "HelperBot"):
        assert non_dm not in r.text


async def test_dialogs_groups_tab(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs?tab=groups")
    assert r.status_code == 200
    assert "Ann" not in r.text
    for non_dm in ("Devs", "News", "HelperBot"):
        assert non_dm in r.text
    assert 'hx-get="/dialogs/-100200/messages"' in r.text  # marked id кликабелен
    assert 'data-kind="channel"' in r.text


async def test_dialogs_unknown_tab_falls_back_to_dm(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs?tab=zzz")
    assert r.status_code == 200
    assert "Ann" in r.text
    assert "Devs" not in r.text


async def test_index_has_tab_buttons(client_app):
    ac, _ = client_app
    r = await ac.get("/")
    assert "/dialogs?tab=dm" in r.text
    assert "/dialogs?tab=groups" in r.text


async def test_messages_fragment_accepts_negative_id(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs/-100200/messages")
    assert r.status_code == 200


async def test_messages_fragment(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs/7/messages")
    assert r.status_code == 200
    assert "hi" in r.text


async def test_send_returns_fragment(client_app):
    ac, stub = client_app
    r = await ac.post("/send", data={"dialog_id": "7", "text": "hello"})
    assert r.status_code == 200
    assert "hello" in r.text
    assert stub.sent == [(7, "hello")]


async def test_send_empty_text_returns_400(client_app):
    ac, stub = client_app
    r = await ac.post("/send", data={"dialog_id": "7", "text": "   "})
    assert r.status_code == 400
    assert stub.sent == []


async def test_send_without_dialog_returns_400(client_app):
    ac, stub = client_app
    r = await ac.post("/send", data={"dialog_id": "", "text": "hi"})
    assert r.status_code == 400
    assert stub.sent == []


async def test_media_upload_calls_send_media(client_app):
    ac, stub = client_app
    r = await ac.post(
        "/dialogs/7/media",
        files={"file": ("pic.jpg", b"binarydata", "image/jpeg")},
        data={"caption": "look"},
    )
    assert r.status_code == 200
    assert stub.sent == [(7, "media", "look")]
    assert "look" in r.text


async def test_unauthorized_session_gives_401_with_hint():
    from telethon.errors.rpcerrorlist import AuthKeyUnregisteredError

    stub = WebStubClient()

    async def boom(dm_only=True):
        raise AuthKeyUnregisteredError(None)

    stub.dialogs = boom
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/dialogs")
    assert r.status_code == 401
    assert "tg-messenger login" in r.text


async def test_unhandled_error_returns_500_fragment_and_is_logged(caplog):
    stub = WebStubClient()

    async def boom(dm_only=True):
        raise RuntimeError("kaboom")

    stub.dialogs = boom
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            with caplog.at_level("ERROR", logger="tg_messenger.web.app"):
                r = await ac.get("/dialogs")
    assert r.status_code == 500
    assert "error" in r.text  # HTMX-friendly fragment, not a blank 500
    errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_flood_wait_returns_503_with_hint():
    from tg_messenger.core.flood import HandledFloodWaitError

    stub = WebStubClient()

    async def boom(dm_only=True):
        raise HandledFloodWaitError("dialogs", 100)

    stub.dialogs = boom
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/dialogs")
    assert r.status_code == 503
    assert "flood wait" in r.text.lower()


async def test_sse_stream_failure_is_logged_and_closes(caplog):
    import pytest

    from tg_messenger.web.app import sse_event_stream

    stub = WebStubClient()

    async def broken_listen():
        raise RuntimeError("stream blew up")
        yield  # pragma: no cover

    stub.listen_all = broken_listen
    gen = sse_event_stream(stub, dialog_id=7)
    with caplog.at_level("ERROR", logger="tg_messenger.web.app"):
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
    errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_stream_yields_sse_frame():
    # Drive the SSE generator directly: subscribing then publishing must
    # produce one data frame for the matching dialog (and skip others).
    import asyncio

    from tg_messenger.web.app import sse_event_stream

    stub = WebStubClient()
    gen = sse_event_stream(stub, dialog_id=7)
    task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)  # let it subscribe

    # a message for a different dialog must be ignored
    other = Message(id=8, dialog_id=1, sender_id=1, out=False, text="nope",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus.publish(IncomingEvent(dialog_id=1, message=other))

    msg = Message(id=9, dialog_id=7, sender_id=7, out=False, text="ping",
                  date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus.publish(IncomingEvent(dialog_id=7, message=msg))

    frame = await asyncio.wait_for(task, timeout=2)
    assert "ping" in frame
    assert frame.startswith("data: ")
    await gen.aclose()


async def test_stream_yields_group_frame():
    # SSE для группового диалога (marked id) живёт на listen_all
    import asyncio

    from tg_messenger.web.app import sse_event_stream

    stub = WebStubClient()
    gen = sse_event_stream(stub, dialog_id=-100200)
    task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)  # let it subscribe

    dm = Message(id=10, dialog_id=7, sender_id=7, out=False, text="не то",
                 date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus.publish(IncomingEvent(dialog_id=7, message=dm))

    grp = Message(id=11, dialog_id=-100200, sender_id=9, out=False, text="в группе",
                  date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus.publish(IncomingEvent(dialog_id=-100200, message=grp))

    frame = await asyncio.wait_for(task, timeout=2)
    import json
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload == {"id": 11, "text": "в группе"}  # не DM-событие, а групповое
    await gen.aclose()
