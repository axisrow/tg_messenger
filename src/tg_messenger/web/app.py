"""FastAPI + HTMX + SSE web interface over the shared core.

``build_app(client=...)`` injects a client for tests; otherwise a real
StandaloneTelegramClient is created from env and connected on startup.
``suggester=...`` optionally enables the human-in-the-loop reply draft endpoint.
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
from collections import OrderedDict
from contextlib import asynccontextmanager
from hashlib import sha256
from html import escape
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from telethon.errors import UnauthorizedError

from tg_messenger.core.auth import (
    LoginError,
    LoginSession,
    delivery_hint,
    session_store_from_env,
)
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.search import filter_dialogs

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SUGGEST_CSRF_HEADER = "x-tg-messenger-csrf"
OUTBOUND_NONCE_TTL_SECONDS = 5 * 60
OUTBOUND_NONCE_MAX = 200
OUTBOUND_TIMEOUT_SECONDS = 20

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
    from tg_messenger.core.client import client_from_env

    return client_from_env(session_name=session_name)


def _dialog_li(d) -> str:
    uname = f" @{escape(d.username)}" if d.username else ""
    unread = f' <span class="unread">{d.unread}</span>' if d.unread else ""
    return (
        f'<li hx-get="/dialogs/{d.id}/messages" hx-target="#messages" data-kind="{d.kind}">'
        f"{d.id} — {escape(d.title)}{uname}{unread}</li>"
    )


def _session_store():
    """SessionStore over the configured session dir (env override for tests/ops)."""
    return session_store_from_env()


DEFAULT_MAX_UPLOAD_MB = 50
# upload streaming chunk: bounds the per-read memory, not the file size
_UPLOAD_CHUNK_BYTES = 1024 * 1024


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
    body = f"[{m.id}] {body}"
    translation = ""
    if getattr(m, "translated_text", None):
        translation = f'<div class="translation">↳ {escape(m.translated_text)}</div>'
    # #48: a reply control referencing this message id; chat.html wires it to the composer
    reply_btn = (
        f'<button type="button" class="reply-btn" data-reply="{m.id}" '
        f'title="Reply">↩</button>'
    )
    return (
        f'<div class="msg {cls}" data-id="{m.id}">{body}{reply_btn}{translation}</div>'
    )


def _reaction_emoticon(emoticon: str | None) -> str:
    return emoticon if emoticon is not None else "<custom>"


def _reaction_div(message_id: int, emoticon: str | None) -> str:
    body = f"reacted [{message_id}]: {escape(_reaction_emoticon(emoticon))}"
    return f'<div class="msg reaction">{body}</div>'


def _error_response(text: str, status_code: int) -> HTMLResponse:
    """The one escaped error-fragment shape every route returns."""
    return HTMLResponse(f'<div class="error">{escape(text)}</div>', status_code=status_code)


def _same_origin_error(request: Request) -> HTMLResponse | None:
    if request.headers.get(SUGGEST_CSRF_HEADER) != "1":
        return _error_response("Suggest requires a same-origin request.", 403)
    origin = request.headers.get("origin")
    if origin is None:
        return None
    host = request.headers.get("host") or request.url.netloc
    expected = f"{request.url.scheme}://{host}"
    if origin != expected:
        return _error_response("Suggest requires a same-origin request.", 403)
    return None


def _same_origin_json_error(request: Request) -> JSONResponse | None:
    if request.headers.get(SUGGEST_CSRF_HEADER) != "1":
        return JSONResponse(
            {
                "applies": False,
                "status": "error",
                "error": "Outbound requires a same-origin request.",
            },
            status_code=403,
        )
    origin = request.headers.get("origin")
    if origin is None:
        return None
    host = request.headers.get("host") or request.url.netloc
    expected = f"{request.url.scheme}://{host}"
    if origin != expected:
        return JSONResponse(
            {
                "applies": False,
                "status": "error",
                "error": "Outbound requires a same-origin request.",
            },
            status_code=403,
        )
    return None


def _sent_bucket(sent_ids_by_client: OrderedDict, client_id: str) -> OrderedDict:
    """Return the bounded sent-message set for one browser client."""
    client_id = _bounded_client_id(client_id)
    bucket = sent_ids_by_client.get(client_id)
    if bucket is None:
        bucket = OrderedDict()
        sent_ids_by_client[client_id] = bucket
    sent_ids_by_client.move_to_end(client_id)
    while len(sent_ids_by_client) > 100:
        sent_ids_by_client.popitem(last=False)
    return bucket


def _bounded_client_id(client_id: str) -> str:
    return client_id[:80]


def _prune_outbound_nonces(nonces: OrderedDict, *, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        nonce for nonce, payload in nonces.items()
        if payload["expires_at"] <= now
    ]
    for nonce in expired:
        nonces.pop(nonce, None)
    while len(nonces) > OUTBOUND_NONCE_MAX:
        nonces.popitem(last=False)


def _remember_outbound_nonce(
    nonces: OrderedDict,
    *,
    dialog_id: int,
    web_client_id: str,
    source_text: str,
    variants: list[str],
) -> str:
    now = time.monotonic()
    _prune_outbound_nonces(nonces, now=now)
    nonce = secrets.token_urlsafe(24)
    nonces[nonce] = {
        "dialog_id": dialog_id,
        "web_client_id": _bounded_client_id(web_client_id),
        "source_text": source_text,
        "variants": tuple(variants),
        "expires_at": now + OUTBOUND_NONCE_TTL_SECONDS,
    }
    nonces.move_to_end(nonce)
    _prune_outbound_nonces(nonces, now=now)
    return nonce


def _consume_outbound_nonce(
    nonces: OrderedDict,
    *,
    nonce: str,
    dialog_id: int,
    web_client_id: str,
    text: str,
) -> tuple[bool, str | None]:
    if not nonce.strip():
        return True, None
    _prune_outbound_nonces(nonces)
    payload = nonces.pop(nonce, None)
    if payload is None:
        logger.warning("rejecting missing or expired outbound nonce for dialog %s", dialog_id)
        return False, None
    if payload["dialog_id"] != dialog_id:
        logger.warning("rejecting outbound nonce for wrong dialog %s", dialog_id)
        return False, None
    if payload["web_client_id"] != _bounded_client_id(web_client_id):
        logger.warning("rejecting outbound nonce for wrong web client")
        return False, None
    if text not in payload["variants"]:
        logger.warning("rejecting outbound nonce for non-variant send in dialog %s", dialog_id)
        return False, None
    return True, payload["source_text"]


def _remember_sent(sent_ids: OrderedDict, dialog_id: int, message_id: int) -> None:
    """Record a sent message key so its outgoing echo isn't re-streamed."""
    key = (dialog_id, message_id)
    sent_ids[key] = True
    sent_ids.move_to_end(key)
    while len(sent_ids) > 200:  # bounded, like the core caches
        sent_ids.popitem(last=False)


def _remember_sent_reaction(
    sent_reactions: OrderedDict,
    dialog_id: int,
    message_id: int,
    emoticon: str | None,
) -> None:
    """Record a sent reaction key so its live echo isn't re-streamed."""
    key = (dialog_id, message_id, emoticon)
    sent_reactions[key] = True
    sent_reactions.move_to_end(key)
    while len(sent_reactions) > 200:  # bounded, like the core caches
        sent_reactions.popitem(last=False)


async def sse_event_stream(
    client,
    dialog_id: int,
    sent_ids: OrderedDict | None = None,
    sent_reactions: OrderedDict | None = None,
    translator=None,
):
    """Yield SSE frames for one dialog: incoming messages AND our own (out) messages.

    Merges listen_all() (incoming, groups too) and listen_outgoing() (our messages
    from any device) into one stream via a shared queue. Outgoing echoes of messages
    this server already sent (their ids in ``sent_ids``) are skipped — the POST that
    sent them already returned the bubble. Reaction echoes work the same way through
    ``sent_reactions``.
    """
    sent_ids = sent_ids if sent_ids is not None else OrderedDict()
    sent_reactions = sent_reactions if sent_reactions is not None else OrderedDict()
    # Merge incoming, outgoing and reaction streams by racing their __anext__
    # coroutines. The iterators are created here, before the first await, so an
    # EventBus subscription is live by the time anything is published.
    iterators = {
        "incoming": client.listen_all().__aiter__(),
        "outgoing": client.listen_outgoing().__aiter__(),
        "reaction": client.listen_reactions().__aiter__(),
    }
    # one pending __anext__ task per still-open stream
    pending = {
        asyncio.create_task(it.__anext__()): kind
        for kind, it in iterators.items()
    }
    try:
        while pending:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                kind = pending.pop(task)
                try:
                    ev = task.result()
                except StopAsyncIteration:
                    continue  # that stream ended; the other may still run
                except Exception:
                    logger.exception("SSE %s stream for dialog %s failed", kind, dialog_id)
                    return  # close the SSE stream; browser EventSource will reconnect
                # queue the next pull from this stream right away
                pending[asyncio.create_task(iterators[kind].__anext__())] = kind
                if ev.dialog_id != dialog_id:
                    continue
                if kind == "reaction":
                    key = (ev.dialog_id, ev.message_id, ev.emoticon)
                    if key in sent_reactions:
                        sent_reactions.pop(key, None)
                        continue
                    payload = {
                        "type": "reaction",
                        "message_id": ev.message_id,
                        "emoticon": ev.emoticon,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    continue
                out = kind == "outgoing"
                if out and (ev.dialog_id, ev.message.id) in sent_ids:
                    continue  # our own optimistic bubble already shows it
                payload = {"id": ev.message.id, "text": ev.message.text, "out": out}
                yield f"data: {json.dumps(payload)}\n\n"
                if translator is not None:
                    try:
                        translated = await translator.translate_message(ev.message)
                    except Exception:
                        logger.exception("SSE translation failed for dialog %s", dialog_id)
                        continue
                    if translated.translated_text:
                        payload = {
                            "type": "translation",
                            "message_id": translated.id,
                            "text": translated.translated_text,
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
    finally:
        for task in pending:
            task.cancel()


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
    # #49: if Telegram told us when a resend is allowed, disable the button and count
    # down — spamming resend is a flood risk on the number. No timeout → active as before.
    timeout = getattr(delivery, "timeout", None) if delivery is not None else None
    if timeout and timeout > 0:
        resend_form = (
            '<form hx-post="/tg-login/resend" hx-target="#card" hx-swap="outerHTML">'
            f'<button type="submit" id="resend-btn" data-timeout="{int(timeout)}" disabled>'
            f"Отправить код повторно ({int(timeout)})</button>"
            "</form>"
            "<script>(function(){"
            "var b=document.getElementById('resend-btn');"
            "if(!b)return;var n=parseInt(b.dataset.timeout,10)||0;"
            "var t=setInterval(function(){n-=1;"
            "if(n<=0){clearInterval(t);b.disabled=false;"
            "b.textContent='Отправить код повторно';}"
            "else{b.textContent='Отправить код повторно ('+n+')';}},1000);"
            "})();</script>"
        )
    else:
        resend_form = (
            '<form hx-post="/tg-login/resend" hx-target="#card" hx-swap="outerHTML">'
            '<button type="submit">Отправить код повторно</button>'
            "</form>"
        )
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
        f"{resend_form}"
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


async def _mark_read_best_effort(client, dialog_id: int, max_id: int) -> None:
    try:
        await client.mark_read(dialog_id, max_id=max_id)
    except Exception:
        logger.warning("mark_read failed for dialog %s — continuing", dialog_id, exc_info=True)


def _schedule_mark_read(app: FastAPI, client, dialog_id: int, max_id: int) -> None:
    task = asyncio.create_task(_mark_read_best_effort(client, dialog_id, max_id))
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


def build_app(
    *, client=None, session_name: str = "default", suggester=None, web_pass: str | None = None,
    login_session=None, store=None, translator=None, outbound=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.background_tasks = set()
        # Per-browser ids of messages sent via this server's /send & /media.
        # They echo back on listen_outgoing(); only the same browser client skips
        # them because its POST already returned the optimistic bubble.
        app.state.sent_ids_by_client = OrderedDict()
        app.state.sent_reactions_by_client = OrderedDict()
        app.state.outbound_nonces = OrderedDict()
        app.state.client = client or _make_real_client(session_name)
        # reply suggester (#17) — optional; None disables the /suggest endpoint
        app.state.suggester = suggester
        app.state.store = store
        app.state.translator = translator
        app.state.outbound = outbound
        await app.state.client.connect()
        app.state.store_task = None
        if app.state.store is not None:
            await app.state.store.connect()
            app.state.store_task = asyncio.create_task(app.state.store.run())
        # one login wizard state machine per process, bound to the lifespan client
        # (phone_code_hash binds to that single connection — see core.LoginSession).
        # A test seam may inject a fake; otherwise build over the inner Telethon client.
        if login_session is not None:
            app.state.login_session = login_session
        else:
            inner = getattr(app.state.client, "_client", app.state.client)
            app.state.login_session = LoginSession(inner)
        try:
            yield
        finally:
            tasks = set(app.state.background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if app.state.store_task is not None:
                app.state.store_task.cancel()
                results = await asyncio.gather(app.state.store_task, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning("message store task failed", exc_info=result)
            await app.state.client.disconnect()
            if app.state.store is not None:
                await app.state.store.close()
            close_suggester = getattr(app.state.suggester, "close", None)
            if close_suggester is not None:
                try:
                    await close_suggester()
                except Exception:
                    logger.warning("suggester close failed", exc_info=True)

    app = FastAPI(lifespan=lifespan)
    # per-process random cookie-signing key — restart invalidates every session.
    app.state.cookie_key = secrets.token_bytes(32)

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
            return _error_response("Authentication required.", 401)

        @app.get("/login", response_class=HTMLResponse)
        async def login_form(request: Request):
            return TEMPLATES.TemplateResponse(request, "login.html", {})

        @app.post("/login")
        async def login_submit(request: Request, password: str = Form("")):
            # compare bytes: compare_digest raises TypeError on non-ASCII str
            if hmac.compare_digest(password.encode("utf-8"), web_pass.encode("utf-8")):
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
            return _error_response("Wrong password.", 401)

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
        return _error_response(exc.user_message, 503)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.error(
            "unhandled error: %s %s", request.method, request.url.path, exc_info=exc
        )
        return _error_response("Internal error — see log for details.", 500)

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
        store = request.app.state.store
        translator = request.app.state.translator
        items = await (store.history(dialog_id, limit=50) if store is not None else client.history(dialog_id, limit=50))
        if translator is not None:
            items = await translator.translate_history(dialog_id, items)
        # opening a dialog clears its unread counter, but it must never block rendering history
        if items:
            _schedule_mark_read(request.app, client, dialog_id, max(m.id for m in items))
        return HTMLResponse("".join(_message_div(m) for m in items))

    @app.post("/send", response_class=HTMLResponse)
    async def send(request: Request, dialog_id: str = Form(""), text: str = Form(""),
                   reply_to: str = Form(""), web_client_id: str = Form(""),
                   outbound_nonce: str = Form("")):
        if not dialog_id.strip().lstrip("-").isdigit():
            return _error_response("Select a dialog first.", 400)
        if not text.strip():
            return _error_response("Cannot send an empty message.", 400)
        dialog_id_int = int(dialog_id)
        reply_to_id = int(reply_to) if reply_to.strip().lstrip("-").isdigit() else None
        nonce_ok, source_text = _consume_outbound_nonce(
            request.app.state.outbound_nonces,
            nonce=outbound_nonce,
            dialog_id=dialog_id_int,
            web_client_id=web_client_id,
            text=text,
        )
        if not nonce_ok:
            return _error_response("Outbound selection expired. Pick a variant again.", 409)
        msg = await request.app.state.client.send_text(dialog_id_int, text, reply_to=reply_to_id)
        if source_text and request.app.state.store is not None:
            from tg_messenger.agent.translate import get_user_lang

            try:
                source_lang = await get_user_lang(request.app.state.store.storage)
                if source_lang:
                    await request.app.state.store.record_outgoing(
                        dialog_id_int,
                        msg,
                        source_text=source_text,
                        source_lang=source_lang,
                    )
                    msg = msg.model_copy(update={"translated_text": source_text})
            except Exception:
                logger.exception("failed to record outbound source text after send")
        sent_ids = _sent_bucket(request.app.state.sent_ids_by_client, web_client_id)
        _remember_sent(sent_ids, dialog_id_int, msg.id)  # suppress only this client's SSE echo
        return HTMLResponse(_message_div(msg))

    @app.post("/dialogs/{dialog_id}/reaction", response_class=HTMLResponse)
    async def reaction(
        request: Request,
        dialog_id: int,
        message_id: str = Form(""),
        emoticon: str = Form(""),
        web_client_id: str = Form(""),
    ):
        if not message_id.strip().isdigit():
            return _error_response("Message id must be a positive integer.", 400)
        emoticon = emoticon.strip()
        if not emoticon:
            return _error_response("Reaction cannot be empty.", 400)
        msg_id = int(message_id)
        await request.app.state.client.send_reaction(dialog_id, msg_id, emoticon)
        sent_reactions = _sent_bucket(request.app.state.sent_reactions_by_client, web_client_id)
        _remember_sent_reaction(sent_reactions, dialog_id, msg_id, emoticon)
        return HTMLResponse(_reaction_div(msg_id, emoticon))

    @app.post("/dialogs/{dialog_id}/media", response_class=HTMLResponse)
    async def upload_media(
        request: Request,
        dialog_id: int,
        file: UploadFile = File(...),
        caption: str | None = Form(None),
        web_client_id: str = Form(""),
    ):
        max_mb = _max_upload_mb()
        max_bytes = max_mb * 1024 * 1024
        suffix = Path(file.filename or "").suffix
        # stream to the temp file in bounded chunks — the whole upload never
        # sits in memory; reading stops as soon as the limit is crossed
        size = 0
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    break
                tmp.write(chunk)
        try:
            if size > max_bytes:
                logger.warning("media upload for dialog %s rejected: over %d MB",
                               dialog_id, max_mb)
                return _error_response(f"File too large (limit {max_mb} MB).", 413)
            if size == 0:
                logger.warning("media upload for dialog %s rejected: empty file", dialog_id)
                return _error_response("Empty file.", 400)
            msg = await request.app.state.client.send_media(dialog_id, tmp_path, caption=caption)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.warning("failed to remove temporary upload %s", tmp_path, exc_info=True)
        sent_ids = _sent_bucket(request.app.state.sent_ids_by_client, web_client_id)
        _remember_sent(sent_ids, dialog_id, msg.id)  # suppress only this client's SSE echo
        return HTMLResponse(_message_div(msg))

    @app.post("/dialogs/{dialog_id}/suggest", response_class=PlainTextResponse)
    async def suggest(request: Request, dialog_id: int):
        # draft a reply for a human to review; the JS in chat.html drops it into
        # the composer input. 503 (not 500) when the feature is unconfigured.
        same_origin_error = _same_origin_error(request)
        if same_origin_error is not None:
            return same_origin_error
        suggester = request.app.state.suggester
        if suggester is None:
            return _error_response(
                "Suggest is not configured — needs the [agent] extra and TG_AGENT_MODEL.", 503
            )
        dm_ids = {d.id for d in await request.app.state.client.dialogs()}
        if dialog_id not in dm_ids:
            return _error_response("Suggest is available for DM dialogs only.", 403)
        draft = await suggester.suggest(dialog_id)
        return PlainTextResponse(draft)

    @app.post("/dialogs/{dialog_id}/outbound")
    async def outbound_variants(
        request: Request,
        dialog_id: int,
        text: str = Form(""),
        web_client_id: str = Form(""),
    ):
        same_origin_error = _same_origin_json_error(request)
        if same_origin_error is not None:
            return same_origin_error
        outbound = request.app.state.outbound
        if outbound is None:
            return JSONResponse({"applies": False, "status": "disabled"})
        try:
            dialog = await _outbound_dialog(request.app.state.client, dialog_id)
        except DialogLookupError:
            return JSONResponse(
                {
                    "applies": False,
                    "status": "error",
                    "error": "Dialogs are temporarily unavailable.",
                },
                status_code=503,
            )
        if dialog is None:
            return JSONResponse(
                {
                    "applies": False,
                    "status": "error",
                    "error": "Dialog is not available.",
                },
                status_code=403,
            )
        try:
            target_lang, variants = await asyncio.wait_for(
                _build_outbound_variants(
                    outbound,
                    dialog_id,
                    text,
                    telegram_lang_code=getattr(dialog, "telegram_lang_code", None),
                ),
                timeout=OUTBOUND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("outbound variants timed out for dialog %s", dialog_id)
            return JSONResponse(
                {
                    "applies": False,
                    "status": "error",
                    "error": "Translation timed out. Use Send original to send without translation.",
                }
            )
        except Exception:
            logger.exception("outbound variants failed for dialog %s", dialog_id)
            return JSONResponse(
                {
                    "applies": False,
                    "status": "error",
                    "error": "Translation failed. Use Send original to send without translation.",
                }
            )
        if target_lang is None:
            return JSONResponse({"applies": False, "status": "not_applicable"})
        nonce = _remember_outbound_nonce(
            request.app.state.outbound_nonces,
            dialog_id=dialog_id,
            web_client_id=web_client_id,
            source_text=text,
            variants=variants,
        )
        return JSONResponse(
            {
                "applies": True,
                "status": "ready",
                "target_lang": target_lang,
                "variants": variants,
                "nonce": nonce,
            }
        )

    @app.post("/dialogs/{dialog_id}/lang", response_class=HTMLResponse)
    async def outbound_lang(
        request: Request,
        dialog_id: int,
        code: str = Form(""),
        enabled: str = Form("on"),
    ):
        same_origin_error = _same_origin_error(request)
        if same_origin_error is not None:
            return same_origin_error
        outbound = request.app.state.outbound
        if outbound is None:
            return _error_response("Outbound translation is not configured.", 503)
        try:
            dialog = await _outbound_dialog(request.app.state.client, dialog_id)
        except DialogLookupError:
            return _error_response("Dialogs are temporarily unavailable.", 503)
        if dialog is None:
            return _error_response("Dialog is not available.", 403)
        from tg_messenger.agent.outbound import (
            get_dialog_lang,
            is_outbound_enabled,
            set_dialog_lang,
            set_outbound_enabled,
        )

        previous_lang = None
        previous_enabled = True
        previous_loaded = False
        try:
            previous_lang = await get_dialog_lang(outbound.storage, dialog_id)
            previous_enabled = await is_outbound_enabled(outbound.storage, dialog_id)
            previous_loaded = True
            await set_dialog_lang(outbound.storage, dialog_id, code.strip() or None, source="manual")
            await set_outbound_enabled(outbound.storage, dialog_id, enabled != "off")
        except ValueError as exc:
            logger.warning("invalid outbound language code for dialog %s: %s", dialog_id, exc)
            return _error_response(str(exc), 400)
        except Exception:
            logger.exception("failed to update outbound settings for dialog %s", dialog_id)
            if previous_loaded:
                await _restore_outbound_settings(
                    outbound.storage,
                    dialog_id,
                    previous_lang=previous_lang,
                    previous_enabled=previous_enabled,
                )
            return _error_response("Outbound settings could not be saved.", 503)
        return HTMLResponse('<div id="outbound-lang-status">saved</div>')

    @app.get("/settings/lang", response_class=HTMLResponse)
    async def lang_settings(request: Request):
        translator = request.app.state.translator
        if translator is None:
            return _error_response("Translation is not configured.", 503)
        lang = await translator.target_lang()
        return HTMLResponse(
            '<form hx-post="/settings/lang" hx-swap="outerHTML" '
            'hx-headers=\'{"x-tg-messenger-csrf": "1"}\'>'
            f'<input name="code" value="{escape(lang or "")}" placeholder="Language">'
            '<button type="submit">Save</button>'
            "</form>"
        )

    @app.post("/settings/lang", response_class=HTMLResponse)
    async def lang_settings_update(request: Request, code: str = Form("")):
        same_origin_error = _same_origin_error(request)
        if same_origin_error is not None:
            return same_origin_error
        translator = request.app.state.translator
        if translator is None:
            return _error_response("Translation is not configured.", 503)
        value = code.strip().lower() or None
        try:
            await translator.set_target_lang(value)
        except ValueError as exc:
            logger.warning("invalid user language code: %s", exc)
            return _error_response(str(exc), 400)
        return HTMLResponse(f'<div id="lang-status">Language: {escape(value or "unset")}</div>')

    @app.get("/stream/{dialog_id}")
    async def stream(request: Request, dialog_id: int, client_id: str = ""):
        return StreamingResponse(
            sse_event_stream(
                request.app.state.client,
                dialog_id,
                sent_ids=_sent_bucket(request.app.state.sent_ids_by_client, client_id),
                sent_reactions=_sent_bucket(request.app.state.sent_reactions_by_client, client_id),
                translator=request.app.state.translator,
            ),
            media_type="text/event-stream",
        )

    return app


class DialogLookupError(RuntimeError):
    pass


async def _outbound_dialog(client, dialog_id: int):
    try:
        dialogs = await client.dialogs(dm_only=False)
    except Exception as exc:
        logger.warning("failed to verify outbound dialog access", exc_info=True)
        raise DialogLookupError from exc
    for dialog in dialogs:
        if dialog.id == dialog_id:
            return dialog
    return None


async def _restore_outbound_settings(
    storage,
    dialog_id: int,
    *,
    previous_lang,
    previous_enabled: bool,
) -> None:
    from tg_messenger.agent.outbound import set_dialog_lang, set_outbound_enabled

    try:
        if previous_lang is None:
            await set_dialog_lang(storage, dialog_id, None)
        else:
            await set_dialog_lang(
                storage,
                dialog_id,
                previous_lang.lang,
                source=previous_lang.source,
                detected_at=previous_lang.detected_at,
            )
        await set_outbound_enabled(storage, dialog_id, previous_enabled)
    except Exception:
        logger.exception("failed to restore outbound settings for dialog %s", dialog_id)


async def _build_outbound_variants(
    outbound,
    dialog_id: int,
    text: str,
    *,
    telegram_lang_code: str | None = None,
) -> tuple[str | None, list[str]]:
    prepare = getattr(outbound, "prepare_variants", None)
    if prepare is not None:
        return await prepare(dialog_id, text, telegram_lang_code=telegram_lang_code)
    target_lang = await outbound.applies(
        dialog_id,
        text,
        telegram_lang_code=telegram_lang_code,
    )
    if target_lang is None:
        return None, []
    variants = await outbound.variants(dialog_id, text, target_lang)
    return target_lang, variants
