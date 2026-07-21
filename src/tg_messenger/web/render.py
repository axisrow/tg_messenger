"""Server-rendered HTML fragments for the web UI (dialog rows, message bubbles, error +
login-wizard fragments).

Pure string builders over core models — every user-controlled field is ``escape``d. No app
state, no network. Re-exported from ``tg_messenger.web.app`` so the routes (closures inside
``build_app``) keep resolving these names as module globals.
"""

from __future__ import annotations

from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse

from tg_messenger.core.auth import delivery_hint
from tg_messenger.core.models import format_author


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _dialog_li(d) -> str:
    uname = f" @{escape(d.username)}" if d.username else ""
    unread = f' <span class="unread">{d.unread}</span>' if d.unread else ""
    # data-can-send drives the composer enable/disable on the client (zero round-trip):
    # chat.html reads li.dataset.canSend when a dialog opens
    can_send = "1" if getattr(d, "can_send", True) else "0"
    # data-dialog / data-title are the exact, unparsed id+title for the cross-dialog reaction
    # toast (#97): the client matches on data-dialog and reads data-title instead of slicing the
    # rendered <li> text, so a title containing " @" or trailing digits can no longer be
    # over-trimmed (Codex review of #103).
    # #187: keyboard/screen-reader access — a bare <li> is mouse-only. role="button"
    # + tabindex="0" make the row focusable and announced as an activatable control;
    # chat.html wires Enter/Space to the same open-dialog path as a click.
    return (
        f'<li role="button" tabindex="0" '
        f'hx-get="/dialogs/{d.id}/messages" hx-target="#messages" '
        f'data-dialog="{d.id}" data-kind="{d.kind}" data-can-send="{can_send}" '
        f'data-title="{escape(d.title)}">'
        f"{d.id} — {escape(d.title)}{uname}{unread}</li>"
    )


def _profile_li(name: str, *, active: bool) -> str:
    cls = ' class="active"' if active else ""
    marker = " (active)" if active else ""
    return f'<li{cls} data-profile="{escape(name)}">{escape(name)}{marker}</li>'


def _message_div(m, *, show_author: bool = False) -> str:
    cls = "out" if m.out else "in"
    body = escape(m.text) if m.text else "&lt;media&gt;"
    body = f"[{m.id}] {body}"
    # #187: direction is signaled by background+alignment only — an sr-only "You:" label lets a
    # screen reader distinguish an outgoing bubble from an incoming one (color-only WCAG gap).
    direction = '<span class="sr-only">You: </span>' if m.out else ""
    # #108: author line above the text, only in groups/supergroups for incoming messages
    # (the route decides via dialog kind + out). Escaped — author fields are user-controlled.
    author = f'<div class="author">{escape(format_author(m))}</div>' if show_author else ""
    translation = ""
    if getattr(m, "translated_text", None):
        translation = f'<div class="translation">↳ {escape(m.translated_text)}</div>'
    # #48: a reply control referencing this message id; chat.html wires it to the composer.
    # #187: the glyph carries no text — aria-label makes it announced by a screen reader.
    reply_btn = (
        f'<button type="button" class="reply-btn" data-reply="{m.id}" '
        f'title="Reply" aria-label="Reply">↩</button>'
    )
    # #86: a per-message reaction trigger; chat.html toggles a small preset palette under it.
    react_btn = (
        f'<button type="button" class="react-btn" data-react="{m.id}" '
        f'title="React" aria-label="React">🙂</button>'
    )
    # #95: stamp the bubble's own dialog id so a per-message action (react/reply) targets
    # the source dialog, not the global #dialog_id — which updates synchronously on a switch
    # while these bubbles are still briefly live in the DOM (HTMX swaps #messages async).
    return (
        f'<div class="msg {cls}" data-id="{m.id}" data-dialog="{m.dialog_id}">'
        f"{author}{direction}{body}{reply_btn}{react_btn}{translation}</div>"
    )


def _error_response(text: str, status_code: int) -> HTMLResponse:
    """The one escaped error-fragment shape every route returns."""
    # #187: role="alert" — errors are announced immediately by a screen reader (not queued
    # like the polite live regions the success/hint fragments use).
    return HTMLResponse(
        f'<div class="error" role="alert">{escape(text)}</div>', status_code=status_code
    )


def _tg_login_phone_fragment(*, error: str) -> str:
    """HTMX fragment: re-render the phone step with an error (e.g. invalid number)."""
    # #187: one canonical heading — the template's first render and this error re-render
    # must not disagree (Войти↔Вход read as two different screens on a phone typo).
    return (
        '<div id="card">'
        "<h1>Войти в Telegram</h1>"
        f'<div class="error" role="alert">{escape(error)}</div>'
        '<form hx-post="/tg-login/phone" hx-target="#card" hx-swap="outerHTML">'
        '<label for="phone" lang="en">Phone</label>'
        '<input id="phone" type="tel" name="phone" autofocus>'
        '<button type="submit">Отправить код</button>'
        "</form>"
        "</div>"
    )


def _tg_login_code_fragment(delivery=None, *, error: str | None = None) -> str:
    """HTMX fragment: the code-entry step of the /tg-login wizard."""
    hint = escape(delivery_hint(delivery)) if delivery is not None else ""
    # #187: errors use role="alert" so a screen reader announces them immediately.
    err = f'<div class="error" role="alert">{escape(error)}</div>' if error else ""
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
        '<label for="code" lang="en">Code</label>'
        '<input id="code" type="text" name="code" autofocus inputmode="numeric" '
        'autocomplete="one-time-code">'
        '<button type="submit">Войти</button>'
        "</form>"
        f"{resend_form}"
        "</div>"
    )


def _tg_login_password_fragment(*, error: str | None = None) -> str:
    """HTMX fragment: the 2FA-password step of the /tg-login wizard."""
    err = f'<div class="error" role="alert">{escape(error)}</div>' if error else ""
    return (
        '<div id="card">'
        "<h1>Пароль 2FA</h1>"
        f"{err}"
        '<form hx-post="/tg-login/password" hx-target="#card" hx-swap="outerHTML">'
        '<label for="password" lang="en">2FA password</label>'
        '<input id="password" type="password" name="password" autofocus '
        'autocomplete="current-password">'
        '<button type="submit">Войти</button>'
        "</form>"
        "</div>"
    )
