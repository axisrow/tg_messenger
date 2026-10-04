"""JSON API for the /tg pane's warm layer (a `serve` daemon on localhost).

The pane spawns `serve` and reads history/dialog lists over HTTP so one chat
action costs a single RPC on the daemon's live Telethon connection instead of
a cold CLI child (~6 s: python boot + full MTProto handshake on a lossy route).
"""

import httpx
import pytest_asyncio

from tests.test_web import WebStubClient
from tg_messenger.web.app import build_app


class RecordingStub(WebStubClient):
    def __init__(self):
        super().__init__()
        self.history_kwargs: list[dict] = []

    async def history(self, peer, limit=50, offset_id=0, fresh=False):
        self.history_kwargs.append({"peer": peer, "limit": limit, "fresh": fresh})
        return await super().history(peer, limit, offset_id)


@pytest_asyncio.fixture
async def api_app():
    stub = RecordingStub()
    app = build_app(client=stub)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"X-TG-Messenger-CSRF": "1"},
        ) as ac:
            yield ac, stub


async def test_api_health_reports_profile(api_app):
    ac, _ = api_app
    r = await ac.get("/api/health")
    assert r.status_code == 200
    # the pane adopts a daemon only when the profile name matches — a wrong
    # match would cross accounts
    assert r.json() == {"profile": "default"}


async def test_api_dialogs_returns_json(api_app):
    ac, _ = api_app
    r = await ac.get("/api/dialogs")
    assert r.status_code == 200
    rows = r.json()
    assert [d["id"] for d in rows] == [7]
    assert rows[0]["title"] == "Ann"
    assert rows[0]["unread"] == 1


async def test_api_dialogs_groups_tab(api_app):
    ac, _ = api_app
    r = await ac.get("/api/dialogs?tab=groups")
    assert r.status_code == 200
    assert [d["id"] for d in r.json()] == [-100200, -100123, 9]


async def test_api_messages_fresh_flag_reaches_client(api_app):
    ac, stub = api_app
    r = await ac.get("/api/dialogs/7/messages?fresh=1")
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["id"] == 1
    assert rows[0]["text"] == "hi"
    assert stub.history_kwargs[-1]["fresh"] is True


async def test_api_messages_without_fresh_keeps_cache(api_app):
    ac, stub = api_app
    r = await ac.get("/api/dialogs/7/messages")
    assert r.status_code == 200
    assert stub.history_kwargs[-1]["fresh"] is False


async def test_send_with_json_accept_returns_id(api_app):
    # the pane's warm send path takes the new id from JSON — no HTML scraping
    ac, _ = api_app
    r = await ac.post(
        "/send",
        data={"dialog_id": "7", "text": "привет"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"id": 2}


async def test_send_without_json_accept_keeps_html(api_app):
    ac, _ = api_app
    r = await ac.post("/send", data={"dialog_id": "7", "text": "привет"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
