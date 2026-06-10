"""FastAPI + HTMX + SSE web interface over the shared core.

``build_app(client=...)`` injects a client for tests; otherwise a real
StandaloneTelegramClient is created from env and connected on startup.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from telethon.errors import UnauthorizedError

from tg_messenger.core.auth import LOGIN_HINT
from tg_messenger.core.flood import HandledFloodWaitError

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
        f'<li hx-get="/dialogs/{d.id}/messages" hx-target="#messages">'
        f"{d.id} — {escape(d.title)}{uname}{unread}</li>"
    )


def _message_div(m) -> str:
    cls = "out" if m.out else "in"
    body = escape(m.text) if m.text else "&lt;media&gt;"
    return f'<div class="msg {cls}" data-id="{m.id}">{body}</div>'


async def sse_event_stream(client, dialog_id: int):
    """Yield SSE frames for incoming messages of one dialog."""
    try:
        async for ev in client.listen():
            if ev.dialog_id != dialog_id:
                continue
            payload = json.dumps({"id": ev.message.id, "text": ev.message.text})
            yield f"data: {payload}\n\n"
    except Exception:
        # close the stream; the browser's EventSource will reconnect
        logger.exception("SSE stream for dialog %s failed", dialog_id)
        return


def build_app(*, client=None, session_name: str = "default") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = client or _make_real_client(session_name)
        await app.state.client.connect()
        yield
        await app.state.client.disconnect()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(UnauthorizedError)
    async def unauthorized(request: Request, exc: UnauthorizedError):
        return HTMLResponse(f'<div class="error">{LOGIN_HINT}</div>', status_code=401)

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

    @app.get("/dialogs", response_class=HTMLResponse)
    async def dialogs(request: Request):
        items = await request.app.state.client.dialogs(dm_only=True)
        return HTMLResponse("".join(_dialog_li(d) for d in items))

    @app.get("/dialogs/{dialog_id}/messages", response_class=HTMLResponse)
    async def messages(request: Request, dialog_id: int):
        items = await request.app.state.client.history(dialog_id, limit=50)
        return HTMLResponse("".join(_message_div(m) for m in items))

    @app.post("/send", response_class=HTMLResponse)
    async def send(request: Request, dialog_id: str = Form(""), text: str = Form("")):
        if not dialog_id.strip().lstrip("-").isdigit():
            return HTMLResponse('<div class="error">Select a dialog first.</div>', status_code=400)
        if not text.strip():
            return HTMLResponse('<div class="error">Cannot send an empty message.</div>', status_code=400)
        msg = await request.app.state.client.send_text(int(dialog_id), text)
        return HTMLResponse(_message_div(msg))

    @app.post("/dialogs/{dialog_id}/media", response_class=HTMLResponse)
    async def upload_media(
        request: Request,
        dialog_id: int,
        file: UploadFile = File(...),
        caption: str | None = Form(None),
    ):
        suffix = Path(file.filename or "").suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        try:
            msg = await request.app.state.client.send_media(dialog_id, tmp_path, caption=caption)
        finally:
            os.unlink(tmp_path)
        return HTMLResponse(_message_div(msg))

    @app.get("/stream/{dialog_id}")
    async def stream(request: Request, dialog_id: int):
        return StreamingResponse(
            sse_event_stream(request.app.state.client, dialog_id),
            media_type="text/event-stream",
        )

    return app
