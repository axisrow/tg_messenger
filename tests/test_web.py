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
        return [Dialog(id=7, title="Ann", username="ann", unread=1)]

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

    async def listen(self):
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
