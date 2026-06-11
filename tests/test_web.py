import asyncio
from datetime import datetime, timezone

import httpx
import pytest_asyncio
from telethon.sessions import StringSession

from tg_messenger.core.events import EventBus
from tg_messenger.core.models import Dialog, IncomingEvent, Message
from tg_messenger.web.app import build_app


class WebStubClient:
    def __init__(self):
        self.bus = EventBus()
        self.bus_out = EventBus()  # own messages from another device (listen_outgoing)
        self.sent = []
        self.searched = []
        self.read_acks = []

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

    async def group_dialogs(self):
        return [d for d in await self.dialogs(dm_only=False) if d.kind != "dm"]

    async def history(self, peer, limit=50, offset_id=0):
        return [Message(id=1, dialog_id=peer, sender_id=peer, out=False, text="hi",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append((peer, text, reply_to))
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def mark_read(self, peer, max_id=None):
        self.read_acks.append((peer, max_id))

    async def send_media(self, peer, file_path, *, caption=None, voice_note=False,
                         video_note=False, force_document=False):
        self.sent.append((peer, "media", caption))
        self.media_path = str(file_path)
        return Message(id=3, dialog_id=peer, sender_id=1, out=True, text=caption or "<media>",
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def search_messages(self, peer, query, limit=20):
        self.searched.append((peer, query))
        return [Message(id=5, dialog_id=peer, sender_id=peer, out=False, text="found-it",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def listen_all(self):
        async for ev in self.bus.subscribe():
            yield ev

    async def listen_outgoing(self):
        async for ev in self.bus_out.subscribe():
            yield ev


def test_real_web_client_gets_session_encryption_key(monkeypatch, tmp_path):
    from tg_messenger.web import app as web_app

    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", "shared-secret")
    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr("tg_messenger.core.client.StandaloneTelegramClient", FakeStandaloneTelegramClient)

    web_app._make_real_client("default")

    assert captured["session_name"] == "default"
    assert captured["session_dir"] == str(tmp_path)
    assert captured["encryption_key"] == "shared-secret"


def test_real_web_client_gets_send_rate(monkeypatch):
    from tg_messenger.web import app as web_app

    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SEND_RATE", "20")
    monkeypatch.setattr("tg_messenger.core.client.StandaloneTelegramClient", FakeStandaloneTelegramClient)

    web_app._make_real_client("default")

    assert captured["send_rate_per_min"] == 20.0


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


async def test_dialogs_fragment_shows_visible_id(client_app):
    # цикл 63: id диалога виден в тексте строки, не только в hx-get URL
    ac, _ = client_app
    r = await ac.get("/dialogs")
    # "7 — Ann" — видимый id рядом с заголовком
    assert "7 — Ann" in r.text


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


# --- цикл 65: поиск диалогов (?q=) и сообщений (/dialogs/{id}/search) ---


async def test_dialogs_query_filters_dm_tab(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs?q=ann")
    assert r.status_code == 200
    assert "Ann" in r.text


async def test_dialogs_query_no_match_returns_empty(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs?q=zzznope")
    assert r.status_code == 200
    assert "Ann" not in r.text


async def test_dialogs_query_filters_groups_tab(client_app):
    ac, _ = client_app
    r = await ac.get("/dialogs?tab=groups&q=Devs")
    assert r.status_code == 200
    assert "Devs" in r.text
    for non_match in ("News", "HelperBot"):
        assert non_match not in r.text


async def test_dialog_search_calls_search_messages(client_app):
    ac, stub = client_app
    r = await ac.get("/dialogs/7/search?q=hi")
    assert r.status_code == 200
    assert "found-it" in r.text  # текст из заглушки search_messages
    assert stub.searched == [(7, "hi")]


async def test_index_has_search_input(client_app):
    ac, _ = client_app
    r = await ac.get("/")
    assert 'name="q"' in r.text  # поле поиска над списком диалогов


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
    assert stub.sent == [(7, "hello", None)]


async def test_dialogs_show_unread_badge(client_app):
    # цикл 81: непрочитанные показываются бейджем
    ac, _ = client_app
    r = await ac.get("/dialogs")
    assert '<span class="unread">1</span>' in r.text


async def test_opening_messages_marks_read(client_app):
    # цикл 81: открытие диалога помечает его прочитанным (best-effort)
    ac, stub = client_app
    await ac.get("/dialogs/7/messages")
    await asyncio.sleep(0)
    assert stub.read_acks == [(7, 1)]


async def test_messages_mark_read_does_not_block_response():
    stub = WebStubClient()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_mark_read(peer, max_id=None):
        started.set()
        await release.wait()
        stub.read_acks.append((peer, max_id))

    stub.mark_read = slow_mark_read
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await asyncio.wait_for(ac.get("/dialogs/7/messages"), timeout=1)
            assert r.status_code == 200
            assert "hi" in r.text
            await asyncio.wait_for(started.wait(), timeout=1)
            assert stub.read_acks == []
            release.set()
            await asyncio.sleep(0)
    assert stub.read_acks == [(7, 1)]


async def test_messages_empty_history_does_not_mark_read():
    stub = WebStubClient()

    async def empty_history(peer, limit=50, offset_id=0):
        return []

    stub.history = empty_history
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/dialogs/7/messages")
            await asyncio.sleep(0)
    assert r.status_code == 200
    assert stub.read_acks == []


async def test_messages_mark_read_failure_does_not_break(caplog):
    # mark_read best-effort: ошибка логируется, история всё равно отдаётся
    import logging

    stub = WebStubClient()

    async def boom(peer, max_id=None):
        raise RuntimeError("nope")

    stub.mark_read = boom
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            with caplog.at_level(logging.WARNING):
                r = await ac.get("/dialogs/7/messages")
                await asyncio.sleep(0)
    assert r.status_code == 200
    assert "hi" in r.text
    assert any("mark_read" in rec.message or "nope" in str(rec.message) for rec in caplog.records)


async def test_send_reply_to_reaches_client(client_app):
    ac, stub = client_app
    r = await ac.post("/send", data={"dialog_id": "7", "text": "re", "reply_to": "42"})
    assert r.status_code == 200
    assert stub.sent == [(7, "re", 42)]


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
    assert stub.media_path  # a real temp path was passed through
    assert "look" in r.text


async def test_media_upload_over_limit_returns_413(client_app, monkeypatch):
    monkeypatch.setenv("TG_WEB_MAX_UPLOAD_MB", "1")
    ac, stub = client_app
    big = b"x" * (2 * 1024 * 1024)  # 2 MiB > 1 MB limit
    r = await ac.post(
        "/dialogs/7/media",
        files={"file": ("big.bin", big, "application/octet-stream")},
    )
    assert r.status_code == 413
    assert stub.sent == []


async def test_media_upload_streams_in_bounded_chunks(client_app, monkeypatch):
    # лимит должен резать поток ДО того, как файл целиком окажется в памяти:
    # read() зовётся только ограниченными кусками и прекращается на лимите
    from starlette.datastructures import UploadFile as StarletteUploadFile

    monkeypatch.setenv("TG_WEB_MAX_UPLOAD_MB", "1")
    reads: list[int] = []
    orig_read = StarletteUploadFile.read

    async def spy_read(self, size=-1):
        reads.append(size)
        return await orig_read(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", spy_read)
    ac, stub = client_app
    big = b"x" * (3 * 1024 * 1024)  # 3 MiB > 1 MB limit
    r = await ac.post(
        "/dialogs/7/media",
        files={"file": ("big.bin", big, "application/octet-stream")},
    )
    assert r.status_code == 413
    assert stub.sent == []
    assert reads, "the route must read through UploadFile.read"
    # ни одного безразмерного read() (он буферизует весь файл в память)
    assert all(size is not None and 0 < size <= 1024 * 1024 for size in reads)
    # чтение остановилось на лимите, а не дочитало все 3 MiB
    assert len(reads) <= 3


async def test_media_upload_empty_file_returns_400(client_app):
    ac, stub = client_app
    r = await ac.post(
        "/dialogs/7/media",
        files={"file": ("empty.bin", b"", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert stub.sent == []


async def test_profiles_route_lists_saved_profiles(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    # a couple of saved sessions on disk (valid StringSessions)
    store = SessionStore(tmp_path)
    valid = StringSession().save()
    store.save("alice", valid)
    store.save("bob", valid)
    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))

    stub = WebStubClient()
    app = build_app(client=stub, session_name="bob")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/profiles")
    assert r.status_code == 200
    assert "alice" in r.text
    assert "bob" in r.text
    # the active profile is flagged so the UI can highlight it
    assert "active" in r.text.lower()


async def test_unauthorized_session_redirects_to_tg_login():
    # not-logged-in Telegram session: ordinary routes now bounce to the login
    # wizard instead of the old dead-end 401 fragment (#26).
    from telethon.errors.rpcerrorlist import AuthKeyUnregisteredError

    stub = WebStubClient()

    async def boom(dm_only=True):
        raise AuthKeyUnregisteredError(None)

    stub.dialogs = boom
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/dialogs", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/tg-login"


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

    # both pumps fail → the merged stream logs each and closes (StopAsyncIteration)
    stub.listen_all = broken_listen
    stub.listen_outgoing = broken_listen
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
    while stub.bus.subscriber_count == 0:  # deterministic: wait until subscribed
        await asyncio.sleep(0)

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
    while stub.bus.subscriber_count == 0:  # deterministic: wait until subscribed
        await asyncio.sleep(0)

    dm = Message(id=10, dialog_id=7, sender_id=7, out=False, text="не то",
                 date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus.publish(IncomingEvent(dialog_id=7, message=dm))

    grp = Message(id=11, dialog_id=-100200, sender_id=9, out=False, text="в группе",
                  date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus.publish(IncomingEvent(dialog_id=-100200, message=grp))

    frame = await asyncio.wait_for(task, timeout=2)
    import json
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload == {"id": 11, "text": "в группе", "out": False}  # не DM, а групповое
    await gen.aclose()


async def test_stream_yields_outgoing_frame_for_own_message_from_another_device():
    # своё сообщение, отправленное с телефона, приходит через listen_outgoing с out=True
    import asyncio
    import json

    from tg_messenger.core.models import OutgoingEvent
    from tg_messenger.web.app import sse_event_stream

    stub = WebStubClient()
    gen = sse_event_stream(stub, dialog_id=7)
    task = asyncio.create_task(gen.__anext__())
    while stub.bus_out.subscriber_count == 0:  # deterministic: wait until subscribed
        await asyncio.sleep(0)

    msg = Message(id=42, dialog_id=7, sender_id=1, out=True, text="с телефона",
                  date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus_out.publish(OutgoingEvent(dialog_id=7, message=msg))

    frame = await asyncio.wait_for(task, timeout=2)
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload == {"id": 42, "text": "с телефона", "out": True}
    await gen.aclose()


async def test_stream_skips_outgoing_echo_of_messages_we_sent():
    # сообщение, отправленное ЭТИМ сервером (id в sent_ids), не дублируется в SSE
    import asyncio
    from collections import OrderedDict

    from tg_messenger.core.models import OutgoingEvent
    from tg_messenger.web.app import sse_event_stream

    stub = WebStubClient()
    sent_ids = OrderedDict()
    sent_ids[42] = True  # как будто /send уже вернул пузырёк для этого id
    gen = sse_event_stream(stub, dialog_id=7, sent_ids=sent_ids)
    task = asyncio.create_task(gen.__anext__())
    while stub.bus_out.subscriber_count == 0:  # deterministic: wait until subscribed
        await asyncio.sleep(0)

    # эхо нашего собственного сообщения (id=42) — должно быть пропущено
    echo = Message(id=42, dialog_id=7, sender_id=1, out=True, text="эхо",
                   date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus_out.publish(OutgoingEvent(dialog_id=7, message=echo))
    # а вот следующее своё сообщение (id=43) — должно прийти
    fresh = Message(id=43, dialog_id=7, sender_id=1, out=True, text="новое",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stub.bus_out.publish(OutgoingEvent(dialog_id=7, message=fresh))

    import json
    frame = await asyncio.wait_for(task, timeout=2)
    payload = json.loads(frame.removeprefix("data: ").strip())
    # эхо (id=42) пропущено, пришло следующее своё сообщение (id=43)
    assert payload == {"id": 43, "text": "новое", "out": True}
    await gen.aclose()


# --- цикл 97: суфлёр в web (черновик ответа по кнопке Suggest) ---


class StubSuggester:
    def __init__(self, draft="suggested reply"):
        self.draft = draft
        self.calls = []
        self.closed = 0

    async def suggest(self, dialog_id):
        self.calls.append(dialog_id)
        return self.draft

    async def close(self):
        self.closed += 1


SUGGEST_HEADERS = {"X-TG-Messenger-CSRF": "1"}


@pytest_asyncio.fixture
async def suggest_app():
    stub = WebStubClient()
    suggester = StubSuggester()
    app = build_app(client=stub, suggester=suggester)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, suggester


async def test_suggest_endpoint_returns_draft(suggest_app):
    ac, suggester = suggest_app
    r = await ac.post("/dialogs/7/suggest", headers=SUGGEST_HEADERS)
    assert r.status_code == 200
    assert "suggested reply" in r.text
    assert suggester.calls == [7]


async def test_suggest_endpoint_does_not_escape_draft(suggest_app):
    ac, suggester = suggest_app
    suggester.draft = "you & me"
    r = await ac.post("/dialogs/7/suggest", headers=SUGGEST_HEADERS)
    assert r.status_code == 200
    assert r.text == "you & me"


async def test_suggest_endpoint_returns_plain_text_for_markup(suggest_app):
    ac, suggester = suggest_app
    suggester.draft = "<script>alert(1)</script>"
    r = await ac.post("/dialogs/7/suggest", headers=SUGGEST_HEADERS)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "<script>alert(1)</script>"


async def test_suggest_endpoint_rejects_get_before_llm(suggest_app):
    ac, suggester = suggest_app
    r = await ac.get("/dialogs/7/suggest")
    assert r.status_code == 405
    assert suggester.calls == []


async def test_suggest_endpoint_requires_csrf_header(suggest_app):
    ac, suggester = suggest_app
    r = await ac.post("/dialogs/7/suggest")
    assert r.status_code == 403
    assert suggester.calls == []


async def test_suggest_endpoint_rejects_cross_origin(suggest_app):
    ac, suggester = suggest_app
    r = await ac.post(
        "/dialogs/7/suggest",
        headers={**SUGGEST_HEADERS, "Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert suggester.calls == []


async def test_suggest_endpoint_negative_id(suggest_app):
    ac, suggester = suggest_app
    r = await ac.post("/dialogs/-100200/suggest", headers=SUGGEST_HEADERS)
    assert r.status_code == 403
    assert "DM" in r.text
    assert suggester.calls == []


async def test_suggest_endpoint_does_not_send_group_history_to_llm(suggest_app):
    ac, suggester = suggest_app
    r = await ac.post("/dialogs/-100123/suggest", headers=SUGGEST_HEADERS)
    assert r.status_code == 403
    assert suggester.calls == []


async def test_suggest_endpoint_404_when_no_suggester(client_app):
    """build_app без suggester — кнопка/эндпоинт отвечает понятной ошибкой, не 500."""
    ac, _ = client_app
    r = await ac.post("/dialogs/7/suggest", headers=SUGGEST_HEADERS)
    assert r.status_code == 503
    assert "TG_AGENT_MODEL" in r.text or "suggest" in r.text.lower()


async def test_index_has_suggest_button(suggest_app):
    ac, _ = suggest_app
    r = await ac.get("/")
    assert "suggest" in r.text.lower()
    assert "suggest-error" in r.text
    assert "X-TG-Messenger-CSRF" in r.text


# --- циклы 131-132: web-мастер логина Telegram (/tg-login) ---

from tg_messenger.core.auth import CodeDelivery, LoginError  # noqa: E402


class FakeLoginSession:
    """Stand-in for core.LoginSession driving the web/TUI login wizard."""

    def __init__(self, *, needs_2fa=False, wrong_code=False):
        self.state = "phone"
        self.phones = []
        self.codes = []
        self.passwords = []
        self.resends = 0
        self._needs_2fa = needs_2fa
        self._wrong_code = wrong_code

    async def submit_phone(self, phone):
        self.phones.append(phone)
        if getattr(self, "_wrong_phone", False):
            raise LoginError("Invalid phone number.")
        self.state = "code"
        return CodeDelivery(kind="app", next_kind="sms", timeout=60)

    async def resend(self):
        self.resends += 1
        return CodeDelivery(kind="sms")

    async def submit_code(self, code):
        self.codes.append(code)
        if self._wrong_code:
            raise LoginError("Wrong code — try again.")
        if self._needs_2fa:
            self.state = "password"
            return
        self.state = "done"

    async def submit_password(self, password):
        self.passwords.append(password)
        self.state = "done"


def _login_app(session, *, web_pass=None, save_hook=None):
    stub = WebStubClient()
    if save_hook is not None:
        stub.save_session = save_hook
    return build_app(client=stub, login_session=session, web_pass=web_pass)


async def _login_client(app):
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def test_tg_login_get_shows_phone_form():
    app = _login_app(FakeLoginSession())
    async for ac in _login_client(app):
        r = await ac.get("/tg-login")
        assert r.status_code == 200
        assert "phone" in r.text.lower()


async def test_tg_login_phone_returns_code_fragment_with_hint():
    sess = FakeLoginSession()
    app = _login_app(sess)
    async for ac in _login_client(app):
        r = await ac.post("/tg-login/phone", data={"phone": "+10000000000"})
        assert r.status_code == 200
        assert sess.phones == ["+10000000000"]
        assert "Telegram" in r.text  # подсказка: код в приложении Telegram
        assert "code" in r.text.lower()


async def test_tg_login_code_done_redirects_to_chat():
    sess = FakeLoginSession()
    app = _login_app(sess)
    async for ac in _login_client(app):
        await ac.post("/tg-login/phone", data={"phone": "+10000000000"})
        r = await ac.post("/tg-login/code", data={"code": "12345"})
        assert r.status_code in (302, 303)
        assert r.headers["location"] == "/"
        assert sess.codes == ["12345"]


async def test_tg_login_code_2fa_returns_password_fragment():
    sess = FakeLoginSession(needs_2fa=True)
    app = _login_app(sess)
    async for ac in _login_client(app):
        await ac.post("/tg-login/phone", data={"phone": "+10000000000"})
        r = await ac.post("/tg-login/code", data={"code": "12345"})
        assert r.status_code == 200
        assert "password" in r.text.lower()


async def test_tg_login_password_redirects():
    sess = FakeLoginSession(needs_2fa=True)
    app = _login_app(sess)
    async for ac in _login_client(app):
        await ac.post("/tg-login/phone", data={"phone": "+10000000000"})
        await ac.post("/tg-login/code", data={"code": "12345"})
        r = await ac.post("/tg-login/password", data={"password": "hunter2"})
        assert r.status_code in (302, 303)
        assert r.headers["location"] == "/"
        assert sess.passwords == ["hunter2"]


async def test_tg_login_wrong_code_shows_error_not_500():
    sess = FakeLoginSession(wrong_code=True)
    app = _login_app(sess)
    async for ac in _login_client(app):
        await ac.post("/tg-login/phone", data={"phone": "+10000000000"})
        r = await ac.post("/tg-login/code", data={"code": "000"})
        assert r.status_code == 200
        assert "Wrong code" in r.text
        # state stays at code so the user can retry
        assert sess.state == "code"


async def test_tg_login_bad_phone_shows_error_not_500():
    sess = FakeLoginSession()
    sess._wrong_phone = True
    app = _login_app(sess)
    async for ac in _login_client(app):
        r = await ac.post("/tg-login/phone", data={"phone": "not-a-phone"})
        assert r.status_code == 200  # error rendered in the form, not a 500
        assert "Invalid phone" in r.text
        assert sess.state == "phone"  # can retry the phone step


async def test_tg_login_success_saves_session():
    saved = []
    sess = FakeLoginSession()

    def save_hook():
        saved.append(True)

    app = _login_app(sess, save_hook=save_hook)
    async for ac in _login_client(app):
        await ac.post("/tg-login/phone", data={"phone": "+10000000000"})
        await ac.post("/tg-login/code", data={"code": "12345"})
    assert saved == [True]


async def test_unauthorized_routes_redirect_to_tg_login():
    from telethon.errors.rpcerrorlist import AuthKeyUnregisteredError

    stub = WebStubClient()

    async def boom(dm_only=True):
        raise AuthKeyUnregisteredError(None)

    stub.dialogs = boom
    app = build_app(client=stub, login_session=FakeLoginSession())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/dialogs", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/tg-login"


async def test_tg_login_behind_web_pass():
    # /tg-login is reachable only for an authenticated web user (#24): without
    # the cookie the auth gate redirects to /login, not /tg-login.
    app = _login_app(FakeLoginSession(), web_pass="s3cret")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/tg-login", headers={"accept": "text/html"},
                             follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_lifespan_closes_suggester():
    stub = WebStubClient()
    suggester = StubSuggester()
    app = build_app(client=stub, suggester=suggester)
    async with app.router.lifespan_context(app):
        pass
    assert suggester.closed == 1
