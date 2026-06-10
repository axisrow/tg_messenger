"""FastAPI + HTMX + SSE web interface over the shared core.

``build_app(client=...)`` injects a client for tests; otherwise a real
StandaloneTelegramClient is created from env and connected on startup.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from hashlib import sha256
from html import escape
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from telethon.errors import UnauthorizedError

from tg_messenger.core.auth import (
    DEFAULT_SESSION_DIR,
    LoginError,
    LoginSession,
    SessionStore,
    delivery_hint,
)
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.search import filter_dialogs

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# --- web authorization (#24) -------------------------------------------------
COOKIE_NAME = "tg_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days
# paths reachable without a valid cookie (the login wizard itself)
_PUBLIC_PATHS = frozenset({"/login", "/logout"})
# wrong-password penalty; injected so tests monkeypatch it to a no-op (no real sleep)
_WRONG_PASSWORD_DELAY = 1.0


async def _login_delay(seconds: float) -> None:
    """Sleep after a failed login. Tests monkeypatch this to a no-op."""
    await asyncio.sleep(seconds)


def _sign_cookie(key: bytes, *, expiry: int | None = None) -> str:
    """Build a signed cookie value ``"{expiry}:{hmac_hex}"``.

    The HMAC-SHA256 (per-process random ``key``) is taken over the expiry, so a
    client cannot forge or extend a cookie without the key.
    """
    if expiry is None:
        expiry = int(time.time()) + COOKIE_MAX_AGE
    mac = hmac.new(key, str(expiry).encode("ascii"), sha256).hexdigest()
    return f"{expiry}:{mac}"


def _valid_cookie(key: bytes, value: str | None) -> bool:
    """Constant-time validate a cookie: good signature AND not expired."""
    if not value or ":" not in value:
        return False
    expiry_str, _, mac = value.partition(":")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    expected = hmac.new(key, str(expiry).encode("ascii"), sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return False
    return expiry > int(time.time())


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _make_real_client(session_name: str):
    from tg_messenger.core.client import StandaloneTelegramClient

    return StandaloneTelegramClient(
        api_id=int(os.environ.get("TG_API_ID", "0")),
        api_hash=os.environ.get("TG_API_HASH", ""),
        session_name=session_name,
    )


def _dialog_li(d) -> str:
    uname = f" @{escape(d.username)}" if d.username else ""
    unread = f' <span class="unread">{d.unread}</span>' if d.unread else ""
    return (
        f'<li hx-get="/dialogs/{d.id}/messages" hx-target="#messages" data-kind="{d.kind}">'
        f"{d.id} — {escape(d.title)}{uname}{unread}</li>"
    )


def _session_store() -> SessionStore:
    """SessionStore over the configured session dir (env override for tests/ops)."""
    return SessionStore(os.environ.get("TG_SESSION_DIR") or DEFAULT_SESSION_DIR)


DEFAULT_MAX_UPLOAD_MB = 50


def _max_upload_mb() -> int:
    """Upload size cap in MB (env ``TG_WEB_MAX_UPLOAD_MB``, default 50).

    A non-numeric/non-positive override falls back to the default, logged — never
    crashes the request.
    """
    raw = os.environ.get("TG_WEB_MAX_UPLOAD_MB")
    if raw is None:
        return DEFAULT_MAX_UPLOAD_MB
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid TG_WEB_MAX_UPLOAD_MB=%r — using default %d",
                       raw, DEFAULT_MAX_UPLOAD_MB)
        return DEFAULT_MAX_UPLOAD_MB
    if value <= 0:
        logger.warning("non-positive TG_WEB_MAX_UPLOAD_MB=%r — using default %d",
                       raw, DEFAULT_MAX_UPLOAD_MB)
        return DEFAULT_MAX_UPLOAD_MB
    return value


def _profile_li(name: str, *, active: bool) -> str:
    cls = ' class="active"' if active else ""
    marker = " (active)" if active else ""
    return f'<li{cls} data-profile="{escape(name)}">{escape(name)}{marker}</li>'


def _message_div(m) -> str:
    cls = "out" if m.out else "in"
    body = escape(m.text) if m.text else "&lt;media&gt;"
    return f'<div class="msg {cls}" data-id="{m.id}">{body}</div>'


async def sse_event_stream(client, dialog_id: int):
    """Yield SSE frames for incoming messages of one dialog (any kind — groups too)."""
    try:
        async for ev in client.listen_all():
            if ev.dialog_id != dialog_id:
                continue
            payload = json.dumps({"id": ev.message.id, "text": ev.message.text})
            yield f"data: {payload}\n\n"
    except Exception:
        # close the stream; the browser's EventSource will reconnect
        logger.exception("SSE stream for dialog %s failed", dialog_id)
        return


def _tg_login_phone_fragment(*, error: str) -> str:
    """HTMX fragment: re-render the phone step with an error (e.g. invalid number)."""
    return (
        '<div id="card">'
        "<h1>Вход в Telegram</h1>"
        f'<div class="error">{escape(error)}</div>'
        '<form hx-post="/tg-login/phone" hx-target="#card" hx-swap="outerHTML">'
        '<label for="phone">Phone</label>'
        '<input id="phone" type="tel" name="phone" autofocus>'
        '<button type="submit">Отправить код</button>'
        "</form>"
        "</div>"
    )


def _tg_login_code_fragment(delivery=None, *, error: str | None = None) -> str:
    """HTMX fragment: the code-entry step of the /tg-login wizard."""
    hint = escape(delivery_hint(delivery)) if delivery is not None else ""
    err = f'<div class="error">{escape(error)}</div>' if error else ""
    return (
        '<div id="card">'
        "<h1>Введите код</h1>"
        f'<p class="hint">{hint}</p>'
        f"{err}"
        '<form hx-post="/tg-login/code" hx-target="#card" hx-swap="outerHTML">'
        '<label for="code">Code</label>'
        '<input id="code" type="text" name="code" autofocus inputmode="numeric" '
        'autocomplete="one-time-code">'
        '<button type="submit">Войти</button>'
        "</form>"
        '<form hx-post="/tg-login/resend" hx-target="#card" hx-swap="outerHTML">'
        '<button type="submit">Отправить код повторно</button>'
        "</form>"
        "</div>"
    )


def _tg_login_password_fragment(*, error: str | None = None) -> str:
    """HTMX fragment: the 2FA-password step of the /tg-login wizard."""
    err = f'<div class="error">{escape(error)}</div>' if error else ""
    return (
        '<div id="card">'
        "<h1>Пароль 2FA</h1>"
        f"{err}"
        '<form hx-post="/tg-login/password" hx-target="#card" hx-swap="outerHTML">'
        '<label for="password">2FA password</label>'
        '<input id="password" type="password" name="password" autofocus '
        'autocomplete="current-password">'
        '<button type="submit">Войти</button>'
        "</form>"
        "</div>"
    )


def build_app(
    *, client=None, session_name: str = "default", suggester=None, web_pass: str | None = None,
    login_session=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = client or _make_real_client(session_name)
        # reply suggester (#17) — optional; None disables the /suggest endpoint
        app.state.suggester = suggester
        await app.state.client.connect()
        # one login wizard state machine per process, bound to the lifespan client
        # (phone_code_hash binds to that single connection — see core.LoginSession).
        # A test seam may inject a fake; otherwise build over the inner Telethon client.
        if login_session is not None:
            app.state.login_session = login_session
        else:
            inner = getattr(app.state.client, "_client", app.state.client)
            app.state.login_session = LoginSession(inner)
        yield
        await app.state.client.disconnect()

    app = FastAPI(lifespan=lifespan)
    # per-process random cookie-signing key — restart invalidates every session.
    app.state.cookie_key = secrets.token_bytes(32)
    app.state.web_pass = web_pass

    if web_pass:
        @app.middleware("http")
        async def auth_gate(request: Request, call_next):
            if request.url.path in _PUBLIC_PATHS:
                return await call_next(request)
            if _valid_cookie(app.state.cookie_key, request.cookies.get(COOKIE_NAME)):
                return await call_next(request)
            # browsers (HTML) go to the login form; API/SSE callers get a hard 401
            if _wants_html(request):
                return RedirectResponse("/login", status_code=302)
            return HTMLResponse(
                '<div class="error">Authentication required.</div>', status_code=401
            )

        @app.get("/login", response_class=HTMLResponse)
        async def login_form(request: Request):
            return TEMPLATES.TemplateResponse(request, "login.html", {})

        @app.post("/login")
        async def login_submit(request: Request, password: str = Form("")):
            if hmac.compare_digest(password, web_pass):
                resp = RedirectResponse("/", status_code=303)
                resp.set_cookie(
                    COOKIE_NAME,
                    _sign_cookie(app.state.cookie_key),
                    max_age=COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                )
                return resp
            # no silent failure: log the attempt with the client IP, then a delay
            client_ip = request.client.host if request.client else "?"
            logger.warning("failed web login attempt from %s", client_ip)
            await _login_delay(_WRONG_PASSWORD_DELAY)
            return HTMLResponse(
                '<div class="error">Wrong password.</div>', status_code=401
            )

        @app.get("/logout")
        async def logout(request: Request):
            resp = RedirectResponse("/login", status_code=303)
            resp.delete_cookie(COOKIE_NAME)
            return resp

    @app.exception_handler(UnauthorizedError)
    async def unauthorized(request: Request, exc: UnauthorizedError):
        # The Telegram session isn't logged in — send the user to the login wizard
        # instead of the old dead-end 401 fragment (#26). HTMX swap targets follow
        # the redirect to the wizard's phone form.
        logger.info("unauthorized Telegram session — redirecting to /tg-login")
        return RedirectResponse("/tg-login", status_code=302)

    @app.get("/tg-login", response_class=HTMLResponse)
    async def tg_login_form(request: Request):
        return TEMPLATES.TemplateResponse(request, "tg_login.html", {})

    @app.post("/tg-login/phone", response_class=HTMLResponse)
    async def tg_login_phone(request: Request, phone: str = Form("")):
        session = request.app.state.login_session
        # the phone number itself never reaches a log line
        try:
            delivery = await session.submit_phone(phone.strip())
        except LoginError as exc:
            # invalid/refused phone: re-render the phone step with the message (not a 500)
            return HTMLResponse(_tg_login_phone_fragment(error=str(exc)))
        return HTMLResponse(_tg_login_code_fragment(delivery))

    @app.post("/tg-login/resend", response_class=HTMLResponse)
    async def tg_login_resend(request: Request):
        session = request.app.state.login_session
        try:
            delivery = await session.resend()
        except LoginError as exc:
            return HTMLResponse(_tg_login_code_fragment(error=str(exc)))
        return HTMLResponse(_tg_login_code_fragment(delivery))

    @app.post("/tg-login/code", response_class=HTMLResponse)
    async def tg_login_code(request: Request, code: str = Form("")):
        session = request.app.state.login_session
        try:
            await session.submit_code(code.strip())
        except LoginError as exc:
            # wrong/expired code: re-render the code step with the message (not a 500)
            return HTMLResponse(_tg_login_code_fragment(error=str(exc)))
        if session.state == "password":
            return HTMLResponse(_tg_login_password_fragment())
        _save_after_login(request)
        return RedirectResponse("/", status_code=302)

    @app.post("/tg-login/password", response_class=HTMLResponse)
    async def tg_login_password(request: Request, password: str = Form("")):
        session = request.app.state.login_session
        try:
            await session.submit_password(password)
        except LoginError as exc:
            return HTMLResponse(_tg_login_password_fragment(error=str(exc)))
        _save_after_login(request)
        return RedirectResponse("/", status_code=302)

    def _save_after_login(request: Request) -> None:
        # persist the freshly-authorized session; best-effort, logged, never 500s the redirect
        save = getattr(request.app.state.client, "save_session", None)
        if save is None:
            return
        try:
            save()
        except Exception:
            logger.exception("save_session after web login failed")

    @app.exception_handler(HandledFloodWaitError)
    async def flood_wait(request: Request, exc: HandledFloodWaitError):
        logger.warning("%s: flood wait %ss", exc.operation, exc.wait_seconds)
        return HTMLResponse(f'<div class="error">{exc.user_message}</div>', status_code=503)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.error(
            "unhandled error: %s %s", request.method, request.url.path, exc_info=exc
        )
        return HTMLResponse(
            '<div class="error">Internal error — see log for details.</div>',
            status_code=500,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "chat.html", {})

    @app.get("/profiles", response_class=HTMLResponse)
    async def profiles(request: Request):
        # read-only: the saved sessions on disk, with the served one flagged active.
        # One process = one profile, so the active profile is fixed at build_app time.
        names = _session_store().list_profiles()
        return HTMLResponse(
            "".join(_profile_li(n, active=(n == session_name)) for n in names)
        )

    @app.get("/dialogs", response_class=HTMLResponse)
    async def dialogs(request: Request, tab: str = "dm", q: str = ""):
        # unknown tab falls back to dm — HTMX-friendly, never a 400
        client = request.app.state.client
        items = await (client.group_dialogs() if tab == "groups" else client.dialogs())
        # q фильтрует уже загруженный список локально (поверх #8-кэша, без сети)
        items = filter_dialogs(items, q)
        return HTMLResponse("".join(_dialog_li(d) for d in items))

    @app.get("/dialogs/{dialog_id}/search", response_class=HTMLResponse)
    async def search_dialog(request: Request, dialog_id: int, q: str = ""):
        # серверный поиск сообщений внутри диалога (Telegram search=)
        items = await request.app.state.client.search_messages(dialog_id, q)
        return HTMLResponse("".join(_message_div(m) for m in items))

    @app.get("/dialogs/{dialog_id}/messages", response_class=HTMLResponse)
    async def messages(request: Request, dialog_id: int):
        client = request.app.state.client
        items = await client.history(dialog_id, limit=50)
        # opening a dialog clears its unread counter — best-effort, never block the read
        try:
            await client.mark_read(dialog_id)
        except Exception:
            logger.warning("mark_read failed for dialog %s — continuing", dialog_id, exc_info=True)
        return HTMLResponse("".join(_message_div(m) for m in items))

    @app.post("/send", response_class=HTMLResponse)
    async def send(request: Request, dialog_id: str = Form(""), text: str = Form(""),
                   reply_to: str = Form("")):
        if not dialog_id.strip().lstrip("-").isdigit():
            return HTMLResponse('<div class="error">Select a dialog first.</div>', status_code=400)
        if not text.strip():
            return HTMLResponse('<div class="error">Cannot send an empty message.</div>', status_code=400)
        reply_to_id = int(reply_to) if reply_to.strip().lstrip("-").isdigit() else None
        msg = await request.app.state.client.send_text(int(dialog_id), text, reply_to=reply_to_id)
        return HTMLResponse(_message_div(msg))

    @app.post("/dialogs/{dialog_id}/media", response_class=HTMLResponse)
    async def upload_media(
        request: Request,
        dialog_id: int,
        file: UploadFile = File(...),
        caption: str | None = Form(None),
    ):
        data = await file.read()
        if not data:
            logger.warning("media upload for dialog %s rejected: empty file", dialog_id)
            return HTMLResponse('<div class="error">Empty file.</div>', status_code=400)
        max_mb = _max_upload_mb()
        if len(data) > max_mb * 1024 * 1024:
            logger.warning("media upload for dialog %s rejected: %d bytes > %d MB",
                           dialog_id, len(data), max_mb)
            return HTMLResponse(
                f'<div class="error">File too large (limit {max_mb} MB).</div>',
                status_code=413,
            )
        suffix = Path(file.filename or "").suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            msg = await request.app.state.client.send_media(dialog_id, tmp_path, caption=caption)
        finally:
            os.unlink(tmp_path)
        return HTMLResponse(_message_div(msg))

    @app.get("/dialogs/{dialog_id}/suggest", response_class=HTMLResponse)
    async def suggest(request: Request, dialog_id: int):
        # draft a reply for a human to review; the JS in chat.html drops it into
        # the composer input. 503 (not 500) when the feature is unconfigured.
        suggester = request.app.state.suggester
        if suggester is None:
            return HTMLResponse(
                '<div class="error">Suggest is not configured — needs the [agent]'
                " extra and TG_AGENT_MODEL.</div>",
                status_code=503,
            )
        draft = await suggester.suggest(dialog_id)
        return HTMLResponse(escape(draft))

    @app.get("/stream/{dialog_id}")
    async def stream(request: Request, dialog_id: int):
        return StreamingResponse(
            sse_event_stream(request.app.state.client, dialog_id),
            media_type="text/event-stream",
        )

    return app
