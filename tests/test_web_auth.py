"""Web authorization (#24): TG_WEB_PASS gate, HMAC cookie, SSE protection.

The middleware exists ONLY when ``web_pass`` is set — without it the app behaves
exactly as before (regression covered by tests/test_web.py).
"""

import logging

import httpx
import pytest_asyncio

from tests.test_web import WebStubClient
from tg_messenger.web.app import COOKIE_NAME, build_app


@pytest_asyncio.fixture
async def secured_app(monkeypatch):
    # неверный пароль не должен реально спать — инжектируемый sleep на no-op
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("tg_messenger.web.app._login_delay", no_sleep)
    stub = WebStubClient()
    app = build_app(client=stub, web_pass="s3cret")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, stub


# --- цикл 123: middleware-гейт ----------------------------------------------


async def test_protected_route_without_cookie_json_is_401(secured_app):
    ac, _ = secured_app
    r = await ac.get("/dialogs")
    assert r.status_code == 401


async def test_protected_route_without_cookie_html_redirects(secured_app):
    ac, _ = secured_app
    r = await ac.get("/", headers={"accept": "text/html"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login"


async def test_login_form_is_public(secured_app):
    ac, _ = secured_app
    r = await ac.get("/login")
    assert r.status_code == 200
    assert "password" in r.text.lower()


async def test_sse_without_cookie_is_401(secured_app):
    ac, _ = secured_app
    r = await ac.get("/stream/7", headers={"accept": "text/event-stream"})
    assert r.status_code == 401


# --- цикл 124: логин / логаут -----------------------------------------------


async def test_correct_password_sets_cookie_and_opens_routes(secured_app):
    ac, _ = secured_app
    r = await ac.post("/login", data={"password": "s3cret"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert COOKIE_NAME in r.cookies
    # the cookie now opens a protected route
    r2 = await ac.get("/dialogs")
    assert r2.status_code == 200
    assert "Ann" in r2.text


async def test_wrong_password_401_delays_and_warns(secured_app, monkeypatch, caplog):
    ac, _ = secured_app
    calls = []

    async def spy_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr("tg_messenger.web.app._login_delay", spy_sleep)
    with caplog.at_level(logging.WARNING, logger="tg_messenger.web.app"):
        r = await ac.post("/login", data={"password": "nope"})
    assert r.status_code == 401
    assert calls  # injected sleep was called (no real sleep)
    assert any("login" in rec.message.lower() for rec in caplog.records)


async def test_tampered_cookie_is_rejected(secured_app):
    ac, _ = secured_app
    ac.cookies.set(COOKIE_NAME, "9999999999:deadbeef")
    r = await ac.get("/dialogs")
    assert r.status_code == 401


async def test_expired_cookie_is_rejected(secured_app):
    from tg_messenger.web.app import _sign_cookie

    ac, _ = secured_app
    app = ac._transport.app  # type: ignore[attr-defined]
    # well-formed signature but expiry in the past → rejected
    expired = _sign_cookie(app.state.cookie_key, expiry=1)
    ac.cookies.set(COOKIE_NAME, expired)
    r = await ac.get("/dialogs")
    assert r.status_code == 401


async def test_logout_clears_cookie_and_recloses(secured_app):
    ac, _ = secured_app
    await ac.post("/login", data={"password": "s3cret"})
    assert (await ac.get("/dialogs")).status_code == 200
    r = await ac.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    # the Set-Cookie expires the cookie; httpx drops it from the jar
    assert COOKIE_NAME not in ac.cookies or not ac.cookies.get(COOKIE_NAME)
    r2 = await ac.get("/dialogs")
    assert r2.status_code == 401


# --- цикл 126: cookie key is random per process -----------------------------


async def test_cookie_key_is_random_per_app():
    a = build_app(client=WebStubClient(), web_pass="x")
    b = build_app(client=WebStubClient(), web_pass="x")
    assert a.state.cookie_key != b.state.cookie_key
    assert len(a.state.cookie_key) >= 16


async def test_no_web_pass_means_no_gate():
    # регресс: без web_pass любой маршрут открыт (middleware отсутствует)
    stub = WebStubClient()
    app = build_app(client=stub)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/dialogs")
    assert r.status_code == 200
