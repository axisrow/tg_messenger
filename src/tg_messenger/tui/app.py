"""Textual TUI: dialog list (left, Telegram-style tabs) + message view + composer.

Reuses the bubble/list pattern. A background worker drains ``client.listen_all()``
and appends incoming bubbles for the selected dialog (any kind, groups included).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    RadioButton,
    RadioSet,
    Static,
    Tab,
    Tabs,
)

from tg_messenger.agent.outbound_coordinator import OutboundError, OutboundSendCoordinator
from tg_messenger.core.auth import (
    LoginError,
    LoginSession,
    delivery_hint,
    session_store_from_env,
)
from tg_messenger.core.client import READ_ONLY_MESSAGE, SendForbiddenError
from tg_messenger.core.languages import parse_lang_codes, validate_supported_lang_code
from tg_messenger.core.models import format_author
from tg_messenger.core.names import is_safe_profile_name, sanitize_profile_name
from tg_messenger.core.search import can_send_in

logger = logging.getLogger(__name__)

# shown before a draft in the suggestion strip; Tab accepts it into the composer
SUGGEST_PREFIX = "💡 Tab: "
ORIGINAL_SENTINEL = "__tg_messenger_original__"
# #93: the per-message reaction presets — the same four as the web palette (chat.html).
REACTION_PRESETS = ["👍", "❤️", "🔥", "😂"]
# #73: the outbound prepare timeout now lives in OutboundSendCoordinator.
# #124: the key-help overlay text (HelpScreen, opened with ? / F1). Russian, to match the UI.
HELP_TEXT = """Навигация (стрелки):
  ↑ / ↓     цепочка фокуса: Поиск → Вкладки → Диалоги → Сообщения → Поле ввода
  ← / →     войти в диалог (→ из списка) · выйти (← на пустом поле); на вкладках — смена вкладки
  Пробел    к концу/началу списка (диалоги и сообщения)
  Enter     открыть диалог · отправить сообщение

Действия:
  Tab       принять подсказку ответа (иначе — вперёд по фокусу)
  Shift+Tab назад по фокусу
  r / x     реакция на выбранном сообщении
  Ctrl+S    настройки: аккаунты + перевод входящих (режим и языки)
  /lang     язык перевода ИСХОДЯЩИХ в текущем диалоге (команда в поле ввода)
  ? / F1    эта справка
  Esc       очистить поиск · закрыть окно
  Ctrl+C    выход"""


def _terminal_safe_display_text(value: str) -> str:
    """Return a terminal-safe display copy of Telegram-sourced text.

    macOS Terminal and Rich/Textual disagree on a few zero-width Thai marks and emoji glyph
    widths. Keep the model text untouched, but render a conservative one-cell display form in
    the TUI so borders and line clearing stay aligned.
    """
    safe: list[str] = []
    for ch in unicodedata.normalize("NFC", value):
        codepoint = ord(ch)
        if ch in "\ufe0e\ufe0f\u200d" or unicodedata.category(ch) == "Mn":
            continue
        if (
            0x1F000 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF
            or 0x2B00 <= codepoint <= 0x2BFF
        ):
            safe.append("*")
        else:
            safe.append(ch)
    return "".join(safe)


@dataclass
class ComposeState:
    draft: str = ""
    source_text: str | None = None
    original_confirm_text: str | None = None
    ignore_next_empty_change: bool = False
    # #73: the coordinator token for the picked variant; consumed on the send that follows
    outbound_token: str | None = None


def parse_media_command(text: str) -> tuple[str, str | None] | None:
    """Parse a composer ``@PATH [caption]`` media command.

    Pure (no filesystem). Returns ``(path, caption)`` or ``None`` when ``text`` is
    not a media command (doesn't start with ``@`` or has no path after it).
    The path may be quoted (``@"with spaces.png" cap``); the rest is the caption.
    """
    if not text.startswith("@"):
        return None
    rest = text[1:]
    if not rest.strip():
        return None
    try:
        # split off only the first token (the path), keep the remainder verbatim
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    path = tokens[0]
    if not path:
        return None
    # caption = everything after the first token, re-derived from the raw text so
    # internal quoting/spacing of the caption is preserved literally
    remainder = _strip_first_token(rest)
    caption = remainder.strip() or None
    return path, caption


def parse_lang_command(text: str) -> tuple[str, str | None] | None:
    parts = text.split(maxsplit=1)
    if not parts or parts[0] != "/lang":
        return None
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError("usage: /lang CODE|auto|on|off")
    value = parts[1].strip().lower()
    if value in {"auto", "on", "off"}:
        return value, None
    return "set", value


def _strip_first_token(s: str) -> str:
    """Return ``s`` with its first shlex token (quoted or not) removed."""
    lexer = shlex.shlex(s, posix=True)
    lexer.whitespace_split = True
    try:
        lexer.get_token()  # consume the first token
    except ValueError:
        return ""
    # lexer.instream holds the unconsumed tail
    return lexer.instream.read()


def _make_real_client(session_name: str):
    from tg_messenger.core.client import client_from_env

    return client_from_env(session_name=session_name)


def _message_body(message) -> str:
    """The bubble body for a message: the ``[id] text`` line (or ``<media>``)."""
    return f"[{message.id}] {message.text or '<media>'}"


def _message_author_line(message, *, show_author: bool) -> str | None:
    """The group author line (#108) when the caller asks for it, else ``None``.

    #118: author is now passed to MessageBubble as a SEPARATE field rather than glued onto the
    body and re-parsed — so untrusted message text (a body that happens to contain ``\\n[``)
    can never be misread as an author line. Content (``format_author``) is unchanged.
    """
    return format_author(message) if show_author else None


def _split_id_prefix(body: str) -> tuple[str, str]:
    """Split a body into its ``[id] `` prefix and the rest, for dimming the id (#113).

    Returns ``("[id] ", rest)`` when the body opens with a ``[...] `` head, else ``("", body)``
    so non-prefixed bodies (e.g. media echoes) pass through unstyled.
    """
    if body.startswith("["):
        close = body.find("] ")
        if close != -1:
            return body[: close + 2], body[close + 2 :]
    return "", body


class ProfileItem(ListItem):
    """One selectable account profile on the startup screen."""

    def __init__(self, profile: str):
        super().__init__(Static(profile, markup=False))
        self.profile = profile


class ProfileScreen(ModalScreen[str]):
    """Startup account picker — dismisses with the chosen profile name.

    Only shown when >1 profile exists and none was preselected; selecting a row
    returns its name to the caller (which then builds the client for it).
    """

    # #116: center the modal card (the box geometry is shaped by App.CSS #profile-box).
    DEFAULT_CSS = "ProfileScreen { align: center middle; }"

    def __init__(self, profiles: list[str]):
        super().__init__()
        self._profiles = profiles

    def compose(self) -> ComposeResult:
        with Vertical(id="profile-box"):
            yield Label("Select account profile:")
            yield ListView(*(ProfileItem(p) for p in self._profiles), id="profiles")

    def on_mount(self) -> None:
        lv = self.query_one("#profiles", ListView)
        lv.focus()
        if len(self._profiles) > 0:
            lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, ProfileItem):
            self.dismiss(item.profile)


class LoginScreen(ModalScreen[bool]):
    """Telegram login wizard: phone → code → (2FA password) → done.

    Drives a core ``LoginSession`` (the state machine that keeps phone_code_hash
    bound to the one connected client). Network steps run through ``run_worker``
    — never awaited in a handler — so the message pump never stalls. Dismisses
    with ``True`` once the session reaches ``done``; the app then continues its
    normal startup (loads dialogs). Phone numbers and codes are never logged.
    """

    # #116: center the modal card (the box geometry is shaped by App.CSS #login-box).
    DEFAULT_CSS = "LoginScreen { align: center middle; }"

    BINDINGS = [
        # Ctrl+C must quit cleanly even mid-login (priority: focus sits in Input)
        Binding("ctrl+c", "app.quit", "Quit", priority=True, show=False),
    ]

    def __init__(self, login_session):
        super().__init__()
        self._session = login_session

    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Label("Войти в Telegram", id="login-title")
            yield Label("Номер телефона (международный формат):", id="login-prompt")
            yield Input(id="login-input", placeholder="+10000000000")

    def on_mount(self) -> None:
        self.query_one("#login-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # never await network in a handler — hand each step to a worker
        value = event.value.strip()
        if not value:
            return
        self.query_one("#login-input", Input).value = ""
        state = self._session.state
        if state == "phone":
            self.run_worker(self._do_phone(value), exclusive=True)
        elif state == "code":
            self.run_worker(self._do_code(value), exclusive=True)
        elif state == "password":
            self.run_worker(self._do_password(value), exclusive=True)

    async def _do_phone(self, phone: str) -> None:
        try:
            delivery = await self._session.submit_phone(phone)
        except Exception as exc:
            logger.exception("login: submit_phone failed")  # phone stays out of the log
            self.notify(f"Не удалось отправить код: {exc}", severity="error")
            return
        self.query_one("#login-prompt", Label).update(delivery_hint(delivery))
        self.query_one("#login-input", Input).placeholder = "Код"

    async def _do_code(self, code: str) -> None:
        try:
            await self._session.submit_code(code)
        except LoginError as exc:
            self.notify(str(exc), severity="error")  # state preserved — retry
            return
        except Exception as exc:
            logger.exception("login: submit_code failed")
            self.notify(f"Ошибка входа: {exc}", severity="error")
            return
        if self._session.state == "password":
            self.query_one("#login-prompt", Label).update("Пароль 2FA:")
            self.query_one("#login-input", Input).placeholder = "Пароль 2FA"
            return
        self.dismiss(True)

    async def _do_password(self, password: str) -> None:
        try:
            await self._session.submit_password(password)
        except LoginError as exc:
            self.notify(str(exc), severity="error")
            return
        except Exception as exc:
            logger.exception("login: submit_password failed")
            self.notify(f"Ошибка входа: {exc}", severity="error")
            return
        self.dismiss(True)


class SidebarTabs(Tabs):
    """Dialog tabs that hand focus down to the sibling dialog list.

    Textual's Tabs only binds left/right; Enter or Down here focuses the
    #dialogs list so the user can start scrolling dialogs at once, instead of
    having to Tab past the strip. Not focus_next: Tabs holds focusable Tab
    children, so the chain would step inside itself.
    """

    BINDINGS = [
        Binding("down,enter", "focus_dialogs", "Dialogs", show=False),
        # #124-r2: Up returns to the search box — the symmetric counterpart to Down/Enter, so the
        # whole sidebar (search ↔ tabs ↔ dialogs) is reachable with arrows alone.
        Binding("up", "focus_search", "Search", show=False),
    ]

    def action_focus_dialogs(self) -> None:
        self.screen.query_one("#dialogs", ListView).focus()

    def action_focus_search(self) -> None:
        self.screen.query_one("#search", Input).focus()


class DialogListView(ListView):
    """Dialog list that returns focus to the sibling tabs when Up is pressed at the top.

    Up from the first item (or an empty selection) focuses the previous widget
    in the chain — the tab strip — the symmetric counterpart to SidebarTabs'
    Down/Enter. Anywhere else, Up scrolls the list as usual (ListView's cursor_up).
    On_focus lands the cursor on the first dialog so arrows scroll immediately,
    however focus arrived (mouse, Tab, or the keyboard handoff).
    """

    BINDINGS = [
        Binding("up", "cursor_up_or_tabs", "Up", show=False),
        # #124: Down at the LAST dialog hands off into the chat pane so the vertical chain
        # (dialogs → messages → composer) is seamless with arrows alone — was a dead clamp.
        Binding("down", "cursor_down_or_messages", "Down", show=False),
        # #124-r2: Right opens the highlighted dialog and drops the cursor into the composer
        # (horizontal "enter the chat"); Space jumps to the last/first item (toggle).
        Binding("right", "open_dialog", "Open", show=False),
        Binding("space", "jump_edge", "Top/bottom", show=False),
    ]

    def on_focus(self) -> None:
        if self.index is None and len(self) > 0:
            self.index = 0

    def action_cursor_up_or_tabs(self) -> None:
        if self.index in (None, 0):
            self.screen.focus_previous()  # the tabs are the prior focusable
        else:
            self.action_cursor_up()

    def action_cursor_down_or_messages(self) -> None:
        # At the last item (or an empty/None selection on a populated list), Down leaves the
        # sidebar for the chat pane: the first message bubble if any, else the composer. Anywhere
        # else, Down scrolls the list as usual (ListView's cursor_down). Mirror of cursor_up_or_tabs.
        if len(self) == 0 or (self.index is not None and self.index >= len(self) - 1):
            # #124: cursor-only movement highlights without opening (only Selected sets _current),
            # so the highlighted dialog can differ from the open chat. Commit the highlighted dialog
            # BEFORE entering the chat pane, else a reply would silently go to the previously-open
            # dialog — a wrong-recipient send. Mirrors action_open_dialog's synchronous open.
            item = self.highlighted_child
            switching = isinstance(item, DialogItem) and item.dialog_id != self.app._current
            if switching:
                # #124: open SYNCHRONOUSLY (not action_select_cursor, which only posts Selected and
                # leaves _current stale until the pump drains it). _current is committed here, before
                # the composer is focused below, so a same-tick Enter/queued paste can never send to
                # the previously-open dialog (Codex wrong-recipient finding).
                self.app._open_dialog(item.dialog_id)
            # #124: same read-only guard as action_open_dialog (the Right path). A read-only chat
            # disables the composer asynchronously; _focus_first_bubble_or_composer would land on it
            # (no bubbles yet — history loads in a worker) and focus would be lost into nothing.
            # Keep focus on the dialog list there so arrow navigation stays alive.
            if isinstance(item, DialogItem) and not self.app._dialog_can_send(item.dialog_id):
                return
            if switching:
                # #124: _open_dialog above committed _current synchronously, but the history RENDER
                # still happens in a worker (_show_history removes and re-mounts bubbles). The bubbles
                # mounted RIGHT NOW still belong to the previously-open chat, so focusing one would
                # land on a STALE bubble during the load window. Because MessageBubble.action_react
                # acts on the bubble's OWN dialog/message id, a fast r/x there would react on the
                # previous conversation — a wrong-conversation action. Land on the composer instead:
                # the Right path's safe target, which _show_history never removes. Once the new
                # history renders, the user can walk up into the (now correct) bubbles.
                self.screen.query_one("#composer", Input).focus()
            else:
                # Same open dialog (no switch): the mounted bubbles are already the current chat's,
                # so entering the first bubble is safe.
                _focus_first_bubble_or_composer(self.screen)
        else:
            self.action_cursor_down()

    def action_open_dialog(self) -> None:
        # Right = open the highlighted dialog, then focus the composer so the user can reply at once.
        # No-op on an empty list / no selection.
        if self.index is None or len(self) == 0:
            return
        item = self.highlighted_child
        # #124: open SYNCHRONOUSLY (not action_select_cursor, which only posts ListView.Selected and
        # leaves _current stale until the pump drains it). _current is committed here, BEFORE the
        # composer is focused, so a same-tick Enter / queued paste can never send to the previously-
        # open dialog — the wrong-recipient send window (Codex finding). _open_dialog still kicks
        # the history render in a worker, independent of focus.
        if isinstance(item, DialogItem):
            self.app._open_dialog(item.dialog_id)
        # #124: a read-only chat disables the composer (via _apply_composer_writable, inside
        # _open_dialog). Focusing a soon-to-be-disabled composer would leave focus on nothing
        # (Textual releases focus from a disabled widget). Keep focus on the list there so arrow
        # navigation stays alive; only dive into the composer when the chat is writable.
        if isinstance(item, DialogItem) and not self.app._dialog_can_send(item.dialog_id):
            return
        self.screen.query_one("#composer", Input).focus()

    def action_jump_edge(self) -> None:
        # Space = toggle between the last and the first item (the same edge-jump used in the
        # message pane). On the last item → first; anywhere else → last. Empty list → no-op.
        if len(self) == 0:
            return
        self.index = 0 if (self.index is not None and self.index >= len(self) - 1) else len(self) - 1


class SearchInput(Input):
    """The sidebar search box. Down hands focus to the tab strip (the next link in the
    vertical chain); Up is the top of the chain (no-op). Left/Right/Home/End stay with Input
    for cursor movement, so typing a query is unaffected (#124)."""

    BINDINGS = [
        Binding("down", "focus_tabs", "Tabs", show=False),
    ]

    def action_focus_tabs(self) -> None:
        self.screen.query_one("#tabs", Tabs).focus()


class ComposerInput(Input):
    """The message composer. Up leaves the composer for the chat history — the last message
    bubble if any, else the dialog list. Down is the bottom of the chain (no-op). Left/Right/
    Home/End stay with Input for cursor movement, so typing/editing is unaffected (#124)."""

    BINDINGS = [
        Binding("up", "focus_messages", "Messages", show=False),
        # #124-r2: Left on an EMPTY composer leaves the chat (back to the dialog list); with text,
        # Left is the normal cursor move (Input's action_cursor_left) so editing isn't broken.
        Binding("left", "cursor_left_or_dialogs", "Dialogs", show=False),
    ]

    def action_focus_messages(self) -> None:
        # #124 (Codex cycle-2): only the current dialog's bubbles are navigable — Up from the
        # composer must never land on a stale previous-dialog bubble that lingers during the async
        # history render (a fast r/x there would react on the previous conversation).
        bubbles = _navigable_bubbles(self.screen)
        if bubbles:
            bubbles[-1].focus()
        else:
            self.screen.query_one("#dialogs", ListView).focus()

    def action_cursor_left_or_dialogs(self) -> None:
        if self.value == "":
            self.screen.query_one("#dialogs", ListView).focus()  # empty field → exit the chat
        else:
            self.action_cursor_left()  # normal in-field cursor move


def _navigable_bubbles(screen) -> list["MessageBubble"]:
    # #124 (Codex cycle-2): a dialog SWITCH commits _current synchronously but the history RENDER
    # is async (_show_history removes the old rows in a worker), so bubbles of the PREVIOUS dialog
    # linger in the DOM during the load window. Reacting on one (r/x) acts on the bubble's OWN
    # dialog_id → a wrong-conversation outbound action. Navigation must therefore only ever land on
    # bubbles belonging to the now-current dialog; stale ones are unreachable until they're removed.
    current = getattr(screen.app, "_current", None)
    return [b for b in screen.query(MessageBubble) if b.dialog_id == current]


def _focus_first_bubble_or_composer(screen) -> None:
    """Enter the chat pane from the dialog list: the first current-dialog bubble, else the composer."""
    bubbles = _navigable_bubbles(screen)
    if bubbles:
        bubbles[0].focus()
    else:
        screen.query_one("#composer", Input).focus()


class DialogItem(ListItem):
    def __init__(self, dialog_id: int, title: str, unread: int = 0, kind: str = "dm"):
        # #113: title-first for readability — the human-readable title leads, the unread badge
        # is prominent, and the raw id is subdued (dim) and trailing. Built as a Rich Text so the
        # untrusted title still renders literally ([brackets] never markup-parsed) while only the
        # id segment carries a style — markup=False stays required.
        t = Text()
        t.append(_terminal_safe_display_text(title))
        if unread:
            t.append(f"  ({unread})")
        t.append(f"  #{dialog_id}", style="dim")
        super().__init__(Static(t, markup=False))
        self.dialog_id = dialog_id
        self.kind = kind


class MessageBubble(Static):
    # #93: focusable so arrow keys select a message and "r" reacts on it — replacing the
    # /react composer command (the TUI has no mouse-click-on-message like the web).
    can_focus = True
    BINDINGS = [
        Binding("up", "focus_prev_bubble", "Prev message", show=False),
        Binding("down", "focus_next_bubble", "Next message", show=False),
        # #124-r2: "x" is a synonym for "r" (both open the reaction picker); Space jumps to the
        # last/first message (toggle) — the same edge-jump as the dialog list. Space is NOT a
        # reaction key (Static binds nothing to it by default, so it's free for navigation).
        Binding("r,x", "react", "React", show=False),
        Binding("space", "jump_edge", "Top/bottom", show=False),
    ]

    def __init__(self, body: str, out: bool, message_id: int | None = None,
                 dialog_id: int | None = None, *, author: str | None = None):
        # #118: author is a SEPARATE field set only by the caller when it knows the message gets
        # an author line (group, incoming) — NOT re-parsed from the body. So untrusted body text
        # that happens to contain "\n[" is never misread as author/[id] metadata. The rendered
        # CONTENT (author line + "\n" + body) is byte-identical to before (#113 parity).
        self._author = author
        self._body = body
        # the message this bubble can be reacted to; None for non-target bubbles.
        self.message_id = message_id
        # #102: the SOURCE dialog of this bubble (web #96 parity, data-dialog) — a reaction
        # targets it, not the globally-current dialog, so action_react is self-contained.
        self.dialog_id = dialog_id
        # #106: translation + reactions are SEPARATE state composed by _build(), so neither
        # clobbers the other regardless of arrival order (both rewrite the whole widget text).
        self._translation: str | None = None
        self._reactions: list[str] = []  # ordered by arrival; the "👍 ❤️ 🔥" line
        super().__init__(self._build(), classes="out" if out else "in", markup=False)

    def _build(self) -> Text:
        # #113: build the bubble as a Rich Text — author line dim, the "[id] " prefix dim, the
        # message body literal (untrusted, never markup-parsed). Translation/reactions append as
        # extra lines exactly as before. NOT named _render — Widget._render is reserved.
        t = Text()
        if self._author is not None:
            t.append(_terminal_safe_display_text(self._author), style="dim")
            t.append("\n")
        prefix, rest = _split_id_prefix(self._body)
        if prefix:
            t.append(prefix, style="dim")
        t.append(_terminal_safe_display_text(rest))
        if self._translation is not None:
            t.append("\n")
            t.append(f"↳ {_terminal_safe_display_text(self._translation)}")
        if self._reactions:
            t.append("\n")
            t.append(_terminal_safe_display_text(" ".join(self._reactions)))
        return t

    def _recompose_text(self) -> None:
        self.update(self._build())

    def show_translation(self, text: str) -> None:
        self._translation = text
        self._recompose_text()

    def add_reaction(self, emoticon: str | None) -> None:
        # #106: accumulate reactions on one line under the message; custom/premium → "<custom>".
        # Dedup so the same emoji isn't shown twice (a re-add or our-own + live echo).
        label = emoticon if emoticon is not None else "<custom>"
        if label in self._reactions:
            return
        self._reactions.append(label)
        self._recompose_text()

    def action_focus_prev_bubble(self) -> None:
        self._focus_sibling(-1)

    def action_focus_next_bubble(self) -> None:
        self._focus_sibling(+1)

    def _focus_sibling(self, delta: int) -> None:
        # #118: bubbles now sit inside a per-message BubbleRow wrapper, so they are no longer
        # direct siblings of each other. Walk all bubbles in DOM (= visual) order via the screen
        # query and step to the neighbour. #124: at the ends, hand focus OFF rather than clamp so
        # the vertical chain is seamless — Up at the first bubble returns to the dialog list, Down
        # at the last bubble drops into the composer.
        if self.screen is None:
            return
        # #124 (Codex cycle-2): step only among the CURRENT dialog's bubbles — never across a stale
        # previous-dialog bubble lingering during the async history render.
        bubbles = _navigable_bubbles(self.screen)
        try:
            idx = bubbles.index(self)
        except ValueError:
            # self is a stale bubble (different dialog) or unmounted — leave the chat pane rather
            # than stepping onto another stale bubble. Up → dialogs, Down → composer.
            (self.screen.query_one("#dialogs", ListView) if delta < 0
             else self.screen.query_one("#composer", Input)).focus()
            return
        target = idx + delta
        if 0 <= target < len(bubbles):
            bubbles[target].focus()
        elif target < 0:
            self.screen.query_one("#dialogs", ListView).focus()  # up past the top → dialog list
        else:
            self.screen.query_one("#composer", Input).focus()  # down past the bottom → composer

    def action_jump_edge(self) -> None:
        # #124-r2: Space = toggle between the last and the first message (the same edge-jump as the
        # dialog list). On the last bubble → first; anywhere else → last. No-op if alone/unmounted.
        if self.screen is None:
            return
        # #124 (Codex cycle-2): jump only within the CURRENT dialog's bubbles, never onto a stale one.
        bubbles = _navigable_bubbles(self.screen)
        if not bubbles or self not in bubbles:
            return
        (bubbles[0] if self is bubbles[-1] else bubbles[-1]).focus()

    def action_react(self) -> None:
        # #102: react on the bubble's OWN source dialog, not the globally-current one —
        # parity with web #96 and self-contained (no app-global read).
        if self.message_id is None or self.dialog_id is None:
            return  # reaction-echo bubble, or no source dialog — nothing to react to
        self.app.run_worker(
            self.app._react_from_bubble(self.dialog_id, self.message_id)
        )


class BubbleRow(Horizontal):
    """A full-width row that aligns its single MessageBubble left (incoming) or right (outgoing).

    #118: the in/out offset is a proportional alignment of the bubble inside this row, not a fixed
    side margin — so the bubble stays inside the message pane at any terminal width. The CSS class
    (``in``/``out``) mirrors the bubble's so the row aligns to the correct side.
    """

    def __init__(self, bubble: "MessageBubble"):
        super().__init__(bubble, classes="out" if bubble.has_class("out") else "in")


def _wrap_bubble(bubble: "MessageBubble") -> BubbleRow:
    """Wrap a bubble in its alignment row (the unit mounted into ``#messages``)."""
    return BubbleRow(bubble)


class VariantItem(ListItem):
    def __init__(self, label: str, value: str):
        super().__init__(Static(label, markup=False))
        self.value = value


class VariantPickScreen(ModalScreen[str | None]):
    # #116: center the modal card (the box geometry is shaped by App.CSS #variant-box).
    DEFAULT_CSS = "VariantPickScreen { align: center middle; }"

    # #124: Escape must CONSUME the event (a Binding, not a key_escape method) so it cancels only
    # this modal — a method handler lets Escape bubble to the app and silently clears the search.
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, variants: list[str], draft: str):
        super().__init__()
        self._variants = variants
        self._draft = draft

    def compose(self) -> ComposeResult:
        with Vertical(id="variant-box"):
            yield Label("Pick translation:")
            rows = [VariantItem(text, text) for text in self._variants]
            rows.append(VariantItem(f"Original: {self._draft}", ORIGINAL_SENTINEL))
            yield ListView(*rows, id="variants")

    def on_mount(self) -> None:
        lv = self.query_one("#variants", ListView)
        lv.focus()
        if len(lv) > 0:
            lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, VariantItem):
            self.dismiss(item.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EmojiPickerScreen(ModalScreen[str | None]):
    """Pick one of the 4 reaction presets for the focused message (#93).

    Mirrors VariantPickScreen and the web palette (REACTION_PRESETS). Returns the chosen
    emoticon, or None if dismissed with Escape.
    """

    # #116: center the modal card (the box geometry is shaped by App.CSS #emoji-box).
    DEFAULT_CSS = "EmojiPickerScreen { align: center middle; }"

    # #124: Escape as a Binding (consumes the event) — see VariantPickScreen: a key_escape method
    # would let Escape bubble to the app and clear the search filter while only closing this picker.
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="emoji-box"):
            yield Label("React:")
            yield ListView(*(VariantItem(e, e) for e in REACTION_PRESETS), id="emojis")

    def on_mount(self) -> None:
        lv = self.query_one("#emojis", ListView)
        lv.focus()
        if len(lv) > 0:
            lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, VariantItem):
            self.dismiss(item.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """The key-help overlay (#124): a centered card listing navigation + hotkeys.

    Opened/closed by ? or F1 (toggle, via the app's action_toggle_help) and dismissed by
    Escape too. f1/escape are non-printable so they fire from the modal's own BINDINGS even
    though no Input is focused here; ? works because the modal holds no text field.
    """

    # #116-parity: center the modal card (the box geometry is shaped by App.CSS #help-box).
    DEFAULT_CSS = "HelpScreen { align: center middle; }"

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit", priority=True, show=False),
        Binding("escape", "dismiss", "Close", show=False),
        Binding("f1", "dismiss", "Close", show=False),
        Binding("question_mark", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label("Горячие клавиши", id="help-title")
            yield Static(HELP_TEXT, id="help-body", markup=False)

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A small yes/no confirmation card (#121): dismisses True (y / Enter) or False (n / Esc).

    Reused for destructive account actions so a single keypress can't delete a saved session —
    parity with the CLI ``profiles remove`` confirmation.
    """

    DEFAULT_CSS = (
        "ConfirmScreen { align: center middle; } "
        "#confirm-box { width: 60%; max-width: 64; height: auto; "
        "padding: 1 2; border: round $warning; background: $surface; }"
    )

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit", priority=True, show=False),
        Binding("y", "confirm", "Yes", show=False),
        Binding("enter", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("escape", "cancel", "No", show=False),
    ]

    def __init__(self, prompt: str):
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._prompt, id="confirm-prompt")
            yield Label("y — да · n / Esc — нет", id="confirm-help")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class AccountItem(ListItem):
    """One saved account profile in the settings screen; the active one is marked."""

    def __init__(self, profile: str, active: bool):
        mark = "  (текущий)" if active else ""
        super().__init__(Static(f"{profile}{mark}", markup=False))
        self.profile = profile


class AccountsScreen(ModalScreen[object]):
    """Account settings (#115): list saved profiles (active marked), add a new one, remove one.

    Dismisses with a rebuilt Translator when the user picks a new translation model (so the main
    app can swap it in), or with None otherwise.

    Adding runs the SAME LoginScreen/LoginSession wizard against a freshly-built client for the
    typed profile name, then persists via the client's save_session() (→ SessionStore). Switching
    the active profile in-session is deferred (it would need a full deps rebuild + reconnect).
    Reuses LoginSession + SessionStore — login is NOT reimplemented here.
    """

    # #116-parity: a centered, bordered card (the box geometry mirrors the other modals).
    DEFAULT_CSS = (
        "AccountsScreen { align: center middle; } "
        "#accounts-box { width: 60%; max-width: 64; height: auto; max-height: 80%; "
        "padding: 1 2; border: round $primary; background: $surface; } "
        "#translate-section { height: auto; margin-top: 1; border-top: solid $primary; padding-top: 1; } "
        "#translate-section RadioSet { height: auto; } "
        # the field captions live in border_title, which inherits the (grey, blurred) border colour
        # and is nearly unreadable by default — give the translation inputs a visible border + a
        # bold accent caption so the labels stay legible whether the field is focused or not.
        "#target-lang, #known-langs, #unknown-langs, #translate-model, #translate-max "
        "{ border: round $primary; border-title-color: $accent; border-title-style: bold; }"
    )

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit", priority=True, show=False),
        Binding("escape", "close", "Close", show=False),
        Binding("a", "add_account", "Add account", show=False),
        Binding("d", "remove_account", "Remove", show=False),
    ]

    # Inbound-translation modes, in display order. The id is the stored TranslateMode literal.
    _TRANSLATE_MODE_LABELS = (
        ("off", "Выкл — не переводить"),
        ("all_unknown", "Всё незнакомое (кроме моих языков)"),
        ("skip_known", "Кроме знакомых (список ниже)"),
        ("only_unknown", "Только указанные (список ниже)"),
    )

    def __init__(self, *, profiles, active, store, account_client_factory=None,
                 login_session=None, translator=None):
        super().__init__()
        # #121: profiles from list_profiles() are already canonical (sanitized stems); the active
        # name comes from the raw session_name, so canonicalize it ONCE here. Marker + delete guard
        # then compare canonical-to-canonical, so an active raw name that sanitizes differently
        # (e.g. "work/personal" → "work_personal") is still recognised and protected.
        self._profiles = list(profiles)
        self._active = sanitize_profile_name(active)
        self._store = store
        # test seams: build the new-profile client / skip the network login flow
        self._account_client_factory = account_client_factory or _make_real_client
        self._login_session = login_session
        # inbound-translation settings live on the injected Translator (it owns the Storage). None
        # means the [agent] extra / translate model isn't configured — the section is then hidden.
        self._translator = translator
        # the mode last loaded-from / saved-to storage. RadioSet.Changed is delivered async (after
        # the worker that set it returns), so a sync "loading" flag can't gate it; instead we save
        # only when the pressed mode actually DIFFERS from this, which the programmatic load never does.
        self._applied_mode: str | None = None
        # the model last loaded-from / saved-to storage; a save only re-probes/rebuilds the
        # Translator when this actually changes (an empty string means "fall back to env").
        self._applied_model: str = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="accounts-box"):
            yield Label("Аккаунты", id="accounts-title")
            yield ListView(
                *(AccountItem(p, p == self._active) for p in self._profiles),
                id="accounts",
            )
            yield Label("a — добавить · d — удалить · Esc — закрыть", id="accounts-help")
            yield Input(placeholder="Имя нового профиля", id="new-profile")
            # inbound-translation settings — only when a Translator is wired ([agent] extra +
            # translate model configured). Values are loaded in on_mount (async, can't read in compose).
            if self._translator is not None:
                with Vertical(id="translate-section"):
                    yield Label("Перевод входящих", id="translate-title")
                    with RadioSet(id="translate-mode"):
                        for mode_id, label in self._TRANSLATE_MODE_LABELS:
                            yield RadioButton(label, id=f"mode-{mode_id}")
                    # THREE explicit fields, each with a PERSISTENT border_title caption (visible on
                    # the frame even with a value typed — a placeholder vanishes on input). The two
                    # language lists are always shown so nothing silently changes meaning by mode.
                    target = Input(placeholder="напр. ru", id="target-lang")
                    target.border_title = "Мой язык (на что переводить)"
                    yield target
                    known = Input(placeholder="напр. ru, en", id="known-langs")
                    known.border_title = "Не переводить"
                    yield known
                    unknown = Input(placeholder="напр. en, ja", id="unknown-langs")
                    unknown.border_title = "Переводить (пусто = всё переводить)"
                    yield unknown
                    model_field = Input(placeholder="напр. openai:glm-5.1", id="translate-model")
                    model_field.border_title = "Модель для перевода"
                    yield model_field
                    max_field = Input(placeholder="напр. 100", id="translate-max")
                    max_field.border_title = "Сколько переводить за раз (Ctrl+T)"
                    yield max_field
                    yield Label("Enter в поле — сохранить", id="translate-help")

    def on_mount(self) -> None:
        if self._translator is not None:
            self.run_worker(self._load_translate_settings(), exclusive=False)

    async def _load_translate_settings(self) -> None:
        if self._translator is None:
            return
        try:
            settings = await self._translator.get_settings()
        except Exception:
            logger.exception("settings: failed to load translation settings")
            return
        # select the stored mode in the RadioSet; record it so the resulting (async) RadioSet.Changed
        # is recognised as the load echo, not a user change, and doesn't auto-save.
        mode = settings.get("mode") or "off"
        self._applied_mode = mode
        try:
            self.query_one(f"#mode-{mode}", RadioButton).value = True
        except Exception:
            logger.warning("settings: unknown stored translate mode %r", mode)
        # three independent fields → three independent settings (no per-mode branching)
        self.query_one("#target-lang", Input).value = settings.get("target") or ""
        self.query_one("#known-langs", Input).value = ", ".join(settings.get("known") or [])
        self.query_one("#unknown-langs", Input).value = ", ".join(settings.get("unknown") or [])
        self.query_one("#translate-model", Input).value = settings.get("model") or ""
        # remember the loaded model so a save only re-probes/rebuilds when it actually changed
        self._applied_model = settings.get("model") or ""
        max_msgs = settings.get("max_messages")
        self.query_one("#translate-max", Input).value = str(max_msgs) if max_msgs else ""

    def _selected_mode(self) -> str:
        """The currently-pressed translate-mode RadioButton id → its TranslateMode literal."""
        pressed = self.query_one("#translate-mode", RadioSet).pressed_button
        if pressed is not None and pressed.id and pressed.id.startswith("mode-"):
            return pressed.id[len("mode-"):]
        return "off"

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "translate-mode":
            return
        mode = self._selected_mode()
        # ignore the echo of the programmatic selection done while loading — only a genuine user
        # mode change (differs from what's stored/applied) should persist.
        if mode == self._applied_mode:
            return
        self.run_worker(self._save_translate_settings(), exclusive=True)

    async def _save_translate_settings(self) -> None:
        if self._translator is None:
            return
        mode = self._selected_mode()
        target = self.query_one("#target-lang", Input).value.strip()
        max_raw = self.query_one("#translate-max", Input).value.strip()
        try:
            # three independent fields → persist all three every save (no per-mode branching),
            # so editing one field never clobbers another
            target_code = validate_supported_lang_code(target) if target else None
            known = parse_lang_codes(self.query_one("#known-langs", Input).value)
            unknown = parse_lang_codes(self.query_one("#unknown-langs", Input).value)
            max_messages = self._parse_max_messages(max_raw)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        model = self.query_one("#translate-model", Input).value.strip()
        try:
            await self._translator.set_settings(
                mode=mode, target=target_code, known=known, unknown=unknown,
                model=model, max_messages=max_messages,
            )
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        except Exception:
            logger.exception("settings: failed to save translation settings")
            self.notify("Не удалось сохранить настройки перевода", severity="error")
            return
        self._applied_mode = mode
        # A changed model means a different LLM: probe its structured method and rebuild the
        # Translator, then hand the new instance back to the app via dismiss() (see action below).
        if model != self._applied_model:
            await self._apply_model_change(model)
            return
        self.notify("Настройки перевода сохранены")

    @staticmethod
    def _parse_max_messages(raw: str) -> int | None:
        """Parse the per-pass cap field: blank → None (use env/default); else a positive int."""
        if not raw:
            return None
        try:
            n = int(raw)
        except ValueError as exc:
            raise ValueError("Сколько переводить: введите число") from exc
        if n < 1:
            raise ValueError("Сколько переводить: число должно быть ≥ 1")
        return n

    async def _apply_model_change(self, model: str) -> None:
        """Probe the freshly chosen model and rebuild the Translator, propagating it to the app.

        A blank model falls back to env; an unbuildable model (bad name / missing key) is reported
        and NOT applied (the old translator stays). On success we dismiss with the new Translator so
        the main app swaps it in for live/history/whole-chat translation.
        """
        self.notify("Проверяю модель…")
        try:
            from tg_messenger.agent.factory import build_translator_with_probe
            from tg_messenger.agent.translate import translate_model_from_env
        except ImportError:
            logger.exception("settings: agent extra unavailable for model change")
            self.notify("Переводчик недоступен (нет extra [agent])", severity="error")
            return
        target_model = model or translate_model_from_env()
        if not target_model:
            self.notify("Не задана модель перевода", severity="error")
            return
        # the SQLite Storage the Translator caches into lives on the current translator, NOT on
        # self._store (which is the SessionStore). Reuse it so settings/cache stay in one DB.
        storage = self._translator.storage
        try:
            new_translator = await build_translator_with_probe(storage, target_model)
        except Exception:
            logger.exception("settings: failed to build translator for model %r", target_model)
            self.notify("Не удалось применить модель — проверьте имя/ключ", severity="error")
            return
        self._translator = new_translator
        self._applied_model = model
        self.notify("Модель перевода применена")
        # hand the rebuilt translator back to the main app (it propagates on screen dismiss)
        self.dismiss(new_translator)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in a translation field saves; the profile-name field keeps its add-account flow.
        if event.input.id in ("target-lang", "known-langs", "unknown-langs",
                               "translate-model", "translate-max"):
            self.run_worker(self._save_translate_settings(), exclusive=True)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_add_account(self) -> None:
        name = self.query_one("#new-profile", Input).value.strip()
        if not name:
            self.query_one("#new-profile", Input).focus()
            return
        # #121: reject a name that isn't already its own filename-safe form (it would silently
        # collapse onto a DIFFERENT session file and overwrite another account), or whose canonical
        # form already exists. Checked BEFORE building a client — no network on a bad name.
        if not is_safe_profile_name(name):
            self.notify(
                f"Недопустимое имя профиля: {name} (только латиница, цифры, _.-)",
                severity="error",
            )
            self.query_one("#new-profile", Input).focus()
            return
        if name in self._store.list_profiles():
            self.notify(f"Профиль уже существует: {name}", severity="error")
            self.query_one("#new-profile", Input).focus()
            return
        self.run_worker(self._add_account(name), exclusive=True)

    async def _add_account(self, name: str) -> None:
        # build + connect a client for the NEW profile, then run the existing login wizard.
        # name is already validated as safe + unique by action_add_account.
        client = None
        try:
            if self._login_session is not None:  # test seam: skip the real client/network
                session = self._login_session
                client = self._account_client_factory(name)
            else:
                client = self._account_client_factory(name)
                await client.connect()
                session = LoginSession(getattr(client, "_client", client))
            ok = await self.app.push_screen_wait(LoginScreen(session))
            if not ok:
                return
            save_session = getattr(client, "save_session", None)
            if save_session is not None:
                save_session()  # → SessionStore.save(name, ...)
        except Exception:
            logger.exception("settings: add account failed")  # name only; no secrets logged
            self.notify(f"Не удалось добавить профиль: {name}", severity="error")
            return
        finally:
            if client is not None:
                disconnect = getattr(client, "disconnect", None)
                if disconnect is not None:
                    try:
                        await disconnect()
                    except Exception:
                        logger.warning("settings: client disconnect failed", exc_info=True)
        await self._refresh(self._store.list_profiles() or [*self._profiles, name])
        self.query_one("#new-profile", Input).value = ""
        self.notify(f"Профиль добавлен: {name}")

    def action_remove_account(self) -> None:
        lv = self.query_one("#accounts", ListView)
        item = lv.highlighted_child
        # #121: both sides are canonical (item.profile from list_profiles, self._active sanitized
        # in __init__), so the active profile is recognised even when its raw name differs.
        if not isinstance(item, AccountItem) or item.profile == self._active:
            return  # never remove the active profile
        # #121: destructive — confirm before deleting a saved session (parity with CLI).
        self.run_worker(self._confirm_remove(item.profile), exclusive=True)

    async def _confirm_remove(self, profile: str) -> None:
        ok = await self.app.push_screen_wait(
            ConfirmScreen(f"Удалить профиль «{profile}»?")
        )
        if not ok:
            return
        try:
            self._store.delete(profile)
        except Exception:
            logger.exception("settings: remove account failed")
            self.notify(f"Не удалось удалить профиль: {profile}", severity="error")
            return
        await self._refresh(self._store.list_profiles())
        self.notify(f"Профиль удалён: {profile}")

    async def _refresh(self, profiles) -> None:
        self._profiles = list(profiles)
        lv = self.query_one("#accounts", ListView)
        await lv.clear()
        for p in self._profiles:
            await lv.append(AccountItem(p, p == self._active))


class MessengerTUI(App):
    # priority: quitting must work even while focus sits in the composer Input.
    # Tab accepts a pending reply suggestion (priority so the Input doesn't eat it
    # for focus traversal); the binding is a no-op when there's nothing to accept.
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        # #114: Tab accepts a pending suggestion, else falls through to forward focus cycling
        # (search → tabs → dialogs → message bubbles → composer, the DOM order). Shift+Tab cycles
        # backward — Textual's Screen already binds it to focus_previous; declaring it on the app
        # makes the symmetric scheme explicit. Arrows navigate within a panel and hand off at the
        # edges (up-at-top-of-dialogs → tabs, down/enter on tabs → dialogs, up/down between bubbles).
        Binding("tab", "accept_suggestion", "Accept suggestion", priority=True, show=False),
        Binding("shift+tab", "focus_previous", "Back", show=False),
        # #115: open account settings (add/list/remove a profile). priority so it works from
        # inside the composer Input.
        Binding("ctrl+s", "open_settings", "Настройки", priority=True),
        # translate the whole open chat (up to the configured cap) on demand — opening a dialog
        # stays fast (cache only); this runs the long LLM pass with a spinner.
        Binding("ctrl+t", "translate_all", "Перевести чат", priority=True, show=True),
        # #124: the key-help overlay (toggle). F1 is non-printable so a priority binding fires
        # even from inside an Input; "?" is printable and so is filtered out while an Input is
        # focused — it works everywhere ELSE (tabs/dialogs/a bubble). Documented: ? outside text
        # inputs, F1 anywhere.
        # The Footer shows ONE help hint. It must be F1, not "?": the Footer only renders bindings
        # active in the CURRENT focus context, and "?" (printable, non-priority) is filtered out
        # while the search Input has focus on startup — so a "?"-hint would be invisible exactly
        # when the user first looks (the original complaint). F1 is priority, stays active inside an
        # Input, and so is always shown. "?" keeps working everywhere outside a text input but is
        # hidden to avoid a duplicate "Справка" entry.
        Binding("f1", "toggle_help", "Справка", priority=True, show=True),
        Binding("question_mark", "toggle_help", "Справка", show=False),
        # #124: Escape clears a non-empty search filter (a small, predictable global). NOT priority,
        # so any open modal's own Escape still wins (the modal binding chain truncates above the app).
        Binding("escape", "clear_search", "Clear search", show=False),
    ]

    CSS = """
    /* #110: max-width lets the fixed 32-wide sidebar yield space on narrow terminals so the
       chat pane (and composer) don't collapse off-screen; on terminals >=64 cols 50% >= 32,
       so the default 32 width is unchanged. */
    #sidebar { width: 32; max-width: 50%; border-right: solid $primary; }
    #chat { width: 1fr; min-width: 16; }
    /* #124: focus indicator — the pane holding focus gets an accent border so "where am I" is
       always visible. :focus-within styles a container while any descendant (search/tabs/list/
       bubble/composer) is focused. Keeps the sidebar's existing right border; the chat's left
       accent is purely additive (it had none) and visually balances the sidebar. The exact
       focused bubble still gets its own #messages MessageBubble:focus accent below. */
    #sidebar:focus-within { border-right: solid $accent; }
    #chat:focus-within { border-left: solid $accent; }
    #messages {
        overflow-x: hidden;
        overflow-y: auto;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    /* the whole-chat translate (Ctrl+T) status line — centered, accent, bold */
    #messages .translate-status {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
        color: $accent;
        text-style: bold;
    }
    /* #113/#118: each message is a framed, shrink-wrapped card. The in/out asymmetry is now a
       PROPORTIONAL alignment of the bubble inside a full-width BubbleRow (align-horizontal),
       NOT a fixed side margin — a fixed 20-col margin pushed the bubble off the right edge once
       #chat shrank to its 16-col min-width (Codex #118). The bubble caps at max-width 80% so it
       always stays inside the pane at any width; the row carries the vertical separation. */
    #messages BubbleRow { width: 1fr; height: auto; }
    #messages BubbleRow.out { align-horizontal: right; }
    #messages BubbleRow.in { align-horizontal: left; }
    #messages MessageBubble {
        width: auto; max-width: 80%; height: auto;
        margin: 1 1; padding: 0 4 0 1;
        border: round $panel;
        text-wrap: wrap; text-overflow: fold;
    }
    /* incoming vs outgoing: distinct border (the side offset is the BubbleRow alignment above). */
    #messages MessageBubble.out { border: round $accent; }
    #messages MessageBubble.in { border: round $panel; }
    /* #124-r3: the focused message is INVERTED (bg/fg swapped) — the same scheme Textual uses for
       list/table selection, so it's clearly visible (the old $boost wash was nearly invisible).
       Placed AFTER .out/.in on purpose: :focus and .out have equal specificity (1,1,1), so source
       ORDER decides the tie — :focus must come last to win on outgoing bubbles too. */
    #messages MessageBubble:focus {
        background: $block-cursor-background;
        color: $block-cursor-foreground;
        text-style: $block-cursor-text-style;
        border: round $accent;
    }
    #suggestion { color: $text-muted; height: auto; }
    #composer { dock: bottom; }
    /* #116: shared modal card — a centered, bordered, width-capped box (was full-width, top-left,
       unframed). Centering (align: center middle) lives on each ModalScreen's DEFAULT_CSS so it is
       not affected by App.CSS-vs-screen scoping; these rules only shape the box. */
    #profile-box, #login-box, #variant-box, #emoji-box, #help-box {
        width: 60%;
        max-width: 64;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #login-title { text-style: bold; }
    #help-title { text-style: bold; }
    """

    def __init__(self, *, client=None, session_name: str = "default",
                 profiles: list[str] | None = None, client_factory=None, deps_factory=None,
                 suggester=None, login_session=None, store=None, translator=None,
                 outbound=None, session_store=None, account_client_factory=None):
        super().__init__()
        self._client = client
        self._session_name = session_name
        self._profiles = profiles or []
        # #115: account settings seams — the SessionStore for listing/saving/removing profiles
        # (lazy: resolved from env when None) and the factory that builds a client for a NEW
        # profile during the add-account wizard (defaults to the real env client).
        self._session_store = session_store
        self._account_client_factory = account_client_factory or _make_real_client
        # login wizard state machine (test seam); built from the client otherwise
        self._login_session = login_session
        # how a client is built once a profile is chosen (injectable for tests)
        self._client_factory = client_factory or _make_real_client
        # #52: builds the WHOLE dependency set (client + suggester/store/translator/
        # outbound) for a profile chosen via ProfileScreen — used by the `tui` entrypoint
        # so the in-app picker, not a CLI menu, resolves the profile. None = library path.
        self._deps_factory = deps_factory
        self._current: int | None = None
        # #108 (Codex review): the open dialog's kind, captured at selection time as a stable value
        # for the author-line decision (read by _kind_for_rendering). Since #110 _all_dialogs is the
        # full snapshot (a dialog no longer drops out on a tab switch), but keeping the captured kind
        # is still the cheapest correct source and harmless.
        self._current_kind: str | None = None
        self._tab = "all"
        self._started = False  # gates tab events until _startup finished
        # #110: the FULL source snapshot (every non-archived dialog, or the archived set on the
        # archive tab) — NOT a tab subset. _render_dialogs projects it via _filter_by_tab + search.
        self._all_dialogs: list = []
        # #110 (Codex 4th pass): which tab's SOURCE the current snapshot was loaded under. _startup
        # reconciles it against _tab after _started flips, so a tab switch during ANY pre-startup
        # await (connect / login / store.connect) can't leave the wrong dialog population rendered.
        self._loaded_tab: str | None = None
        self._suggester = suggester
        self._store = store
        self._translator = translator
        self._outbound = outbound
        # #73: one UI-agnostic coordinator owns the outbound flow (prepare/timeout/token/
        # send/source-recording). Rebuilt after a ProfileScreen pick swaps the deps.
        self._coordinator = self._build_coordinator()
        self._compose_states: dict[int, ComposeState] = {}
        self._bubble_index: dict[int, MessageBubble] = {}
        self._pending_suggestion: str | None = None
        # (dialog_id, message_id) keys we sent from this composer — the same messages echo back on
        # listen_outgoing(); skip them so our optimistic bubble isn't duplicated.
        # Bounded (OrderedDict-as-set, watch.py pattern): a long session can't grow it.
        self._sent_ids: OrderedDict[tuple[int, int], bool] = OrderedDict()
        # (dialog_id, message_id, emoticon) keys we reacted with from this composer.
        # Telegram echoes the update via listen_reactions(); skip the local echo only.
        self._sent_reactions: OrderedDict[tuple[int, int, str | None], bool] = OrderedDict()
        # #106: reactions for the current dialog that arrived while its history was still
        # loading (the bubble didn't exist yet) — replayed once _show_history mounts bubbles,
        # so a reaction in that window isn't lost. Keyed by dialog_id; bounded per dialog.
        self._pending_reactions: dict[int, list[tuple[int, str | None]]] = {}

    def _build_coordinator(self) -> OutboundSendCoordinator:
        return OutboundSendCoordinator(outbound=self._outbound, store=self._store)

    def _scroll_messages_to_end(self, pane=None) -> None:
        pane = pane or self.query_one("#messages", Vertical)
        remaining_attempts = 4

        def scroll_once() -> None:
            nonlocal remaining_attempts
            pane.scroll_end(animate=False, force=True)
            if pane.max_scroll_y:
                pane.scroll_to(y=pane.max_scroll_y, animate=False, force=True)
            remaining_attempts -= 1
            if remaining_attempts > 0 and (
                pane.max_scroll_y == 0 or pane.scroll_y < pane.max_scroll_y
            ):
                self.call_later(scroll_once)

        scroll_once()
        self.call_later(scroll_once)

    def _compose_state_for(self, dialog_id: int) -> ComposeState:
        return self._compose_states.setdefault(int(dialog_id), ComposeState())

    def _save_current_compose_state(self) -> None:
        if self._current is None:
            return
        state = self._compose_state_for(self._current)
        state.draft = self.query_one("#composer", Input).value

    def _restore_compose_state(self, dialog_id: int) -> None:
        state = self._compose_state_for(dialog_id)
        self.query_one("#composer", Input).value = state.draft

    def _clear_pending_outbound(self, dialog_id: int) -> None:
        state = self._compose_state_for(dialog_id)
        state.source_text = None
        state.outbound_token = None
        state.original_confirm_text = None

    def _optimistic_clear(self, dialog_id: int, event_input: Input) -> None:
        """Clear the draft, pending outbound and composer before a send (#89).

        The single optimistic-clear used by every send branch in on_input_submitted
        (except the outbound-flow branch, which keeps the draft). Its failure-path
        counterpart is _restore_draft, called by the send workers if the network rejects.
        """
        self._compose_state_for(dialog_id).draft = ""
        self._clear_pending_outbound(dialog_id)
        event_input.value = ""

    def _restore_draft(self, dialog_id: int, text: str | None) -> None:
        """Put an optimistically-cleared draft back after a failed send (#89).

        Mirror of _optimistic_clear: restore ``text`` as the dialog's draft and, only if
        this dialog is current AND the composer is empty, back into the composer (the
        non-clobber guard — never overwrite a draft typed while the send was in flight).
        ``text is None`` is a no-op, so media/reaction workers can call it unconditionally
        with their optional source_text.
        """
        if text is None:
            return
        self._compose_state_for(dialog_id).draft = text
        if dialog_id == self._current:
            composer = self.query_one("#composer", Input)
            if not composer.value:  # don't clobber a draft typed meanwhile
                composer.value = text

    def _clear_compose_state_after_send(self, dialog_id: int, sent_text: str) -> None:
        state = self._compose_state_for(dialog_id)
        if state.draft == sent_text:
            state.draft = ""
        state.source_text = None
        state.outbound_token = None
        state.original_confirm_text = None
        if dialog_id == self._current:
            composer = self.query_one("#composer", Input)
            if composer.value == sent_text:
                composer.value = ""

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar"):
                yield SearchInput(placeholder="Поиск…", id="search")
                yield SidebarTabs(
                    Tab("Все", id="all"),
                    Tab("Контакты", id="contacts"),
                    Tab("Не контакты", id="non_contacts"),
                    Tab("Группы/супер", id="groups"),
                    Tab("Каналы", id="channels"),
                    Tab("Боты", id="bots"),
                    Tab("Непрочитанные", id="unread"),
                    Tab("Архив", id="archive"),
                    id="tabs",
                )
                yield DialogListView(id="dialogs")
            with Vertical(id="chat"):
                yield Vertical(id="messages")
                yield Static("", id="suggestion", markup=False)
                yield ComposerInput(placeholder="Message…", id="composer")
        # #124-followup: the Footer surfaces the show=True bindings (Справка, Настройки, Выход) —
        # without it the key hints exist but are invisible (the reported "не вижу настроек/?").
        yield Footer()

    async def on_mount(self) -> None:
        # Textual's real App.run() installs asyncio.eager_task_factory (py3.12+),
        # which runs task bodies at create_task time. Telethon's MTProtoSender
        # starts its send loop via create_task and sets _user_connected only
        # afterwards — eagerly-started, the loop sees False and dies, so no
        # request is ever sent (blank TUI, server drops the idle connection).
        # Telethon needs lazy task scheduling; reset before the client exists.
        asyncio.get_running_loop().set_task_factory(None)
        # network work runs in a worker: awaiting it here would stall the app's
        # message pump — blank screen, dead keys, no way to quit (seen live)
        self.query_one("#dialogs", ListView).loading = True
        self.run_worker(self._startup(), exclusive=False)

    async def _startup(self) -> None:
        # The client (and, via deps_factory, the whole dependency set) is built inside the
        # try so a factory error — e.g. a bad TG_API_ID in .env making client_from_env raise
        # — exits cleanly via the handler below instead of crashing run_worker with a raw
        # traceback that tears down the alternate screen.
        try:
            if self._client is None:
                if len(self._profiles) > 1:
                    # >1 account and none preselected → ask which one, then build it
                    chosen = await self.push_screen_wait(ProfileScreen(self._profiles))
                    if self._deps_factory is not None:
                        # #52: the `tui` entrypoint builds the WHOLE dependency set for the
                        # picked profile (client + suggester/store/translator/outbound) and
                        # re-inits its per-profile log file — not just the client.
                        deps = self._deps_factory(chosen)
                        self._session_name = deps.session_name
                        self._client = deps.client
                        self._suggester = deps.suggester
                        self._store = deps.store
                        self._translator = deps.translator
                        self._outbound = deps.outbound
                        self._coordinator = self._build_coordinator()  # #73: deps swapped
                    else:
                        self._session_name = chosen
                        self._client = self._client_factory(chosen)
                elif len(self._profiles) == 1:
                    self._session_name = self._profiles[0]
                    self._client = self._client_factory(self._profiles[0])
                else:
                    self._client = self._client_factory(self._session_name)
            await self._client.connect()
            if not await self._client.is_authorized():
                # show the login wizard instead of exiting; on success continue startup
                session = self._login_session
                if session is None:
                    inner = getattr(self._client, "_client", self._client)
                    session = LoginSession(inner)
                ok = await self.push_screen_wait(LoginScreen(session))
                if not ok:
                    self.exit(return_code=1)
                    return
                save_session = getattr(self._client, "save_session", None)
                if save_session is not None:
                    try:
                        save_session()
                    except Exception:
                        logger.warning("TUI login succeeded but session save failed", exc_info=True)
            await self._load_dialogs()
            if self._store is not None:
                # #112: store.connect() is a pre-startup await DURING which a tab switch can land
                # (on_tabs_tab_activated only sets _tab, gated). The initial snapshot rendered above
                # is for _loaded_tab; if the user switches to a different SOURCE (archive <-> the
                # rest) while connect is pending, that stale population would sit under the loading
                # spinner. Keep #dialogs empty + loading across the connect await so NO stale source
                # is ever shown in this window; the shared reconcile below repopulates under the
                # active tab once the gate opens. (clear() makes no network call, so the same-source
                # no-refetch contract holds.)
                lv = self.query_one("#dialogs", ListView)
                await lv.clear()
                lv.loading = True
                await self._store.connect()
                self.run_worker(self._run_store(), group="message-store", exclusive=False)
        except Exception as exc:
            logger.exception("TUI startup failed")
            self.exit(return_code=1, message=f"Startup failed: {exc}")
            return
        # #118 (Codex high): open the _started gate BEFORE the reconcile render — the ONE shared
        # tail for both the store and no-store paths. Previously the store path awaited a render
        # while the gate was still closed, so a switch to Archive landing in that render window
        # scheduled no reload and the non-archived snapshot stayed under Archive. With the gate
        # open first, _reconcile_after_startup reads the active tab synchronously (no await before
        # that read) and any switch arriving later flows through on_tabs_tab_activated as a reload.
        self._started = True
        await self._reconcile_after_startup()
        self.run_worker(self._drain_incoming(), exclusive=False)
        self.run_worker(self._drain_outgoing(), exclusive=False)
        self.run_worker(self._drain_reactions(), exclusive=False)

    async def _reconcile_after_startup(self) -> None:
        """Reconcile the dialog list to the tab active right now, then clear the spinner (#118).

        Called once, AFTER _started=True, so the active-source decision is read under the open
        gate with no preceding await — a tab switch arriving after this read reaches
        on_tabs_tab_activated (which is no longer gated) and schedules its own reload, instead of
        being silently dropped into a render window. A cross-source switch re-fetches via a worker
        (never an inline awaited render that could paint the stale source); a same-source switch
        (e.g. all→groups) just re-projects the already-fetched snapshot — no refetch.
        """
        lv = self.query_one("#dialogs", ListView)
        if self._tab != self._loaded_tab and (self._tab == "archive") != (
            self._loaded_tab == "archive"
        ):
            # cross-source: the worker clears loading after fetching under the active source
            self.run_worker(self._reload_dialogs(), group="dialogs", exclusive=True)
            return
        # Re-render unconditionally: the store path cleared #dialogs across store.connect(), so the
        # list must be repopulated even when the active tab is unchanged (same source, possibly a
        # different projection than the initial load). No refetch — _render_dialogs is network-free.
        await self._render_dialogs()
        lv.loading = False

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
        close_suggester = getattr(self._suggester, "close", None)
        if close_suggester is not None:
            try:
                await close_suggester()
            except Exception:
                logger.warning("suggester close failed", exc_info=True)
        if self._store is not None:
            await self._store.close()

    async def _run_store(self) -> None:
        try:
            await self._store.run()
        except Exception:
            logger.exception("message store task failed")

    def _filter_by_tab(self, dialogs: list) -> list:
        """Project the full dialog snapshot down to the active tab (#110 bug #4).

        Pure (no network). The ONLY place the tab subset is computed — applied in _render_dialogs
        on every redraw over the full _all_dialogs snapshot, so a live touch that flips a dialog
        read<->unread surfaces/drops it on the 'unread' tab without a reload. 'all' and 'archive'
        pass through unchanged (archive's snapshot already is the archived set).
        """
        if self._tab == "contacts":
            return [d for d in dialogs if d.kind == "dm" and d.is_contact is True]
        if self._tab == "non_contacts":
            return [d for d in dialogs if d.kind == "dm" and d.is_contact is False]
        if self._tab == "unread":
            return [d for d in dialogs if d.unread > 0]
        if self._tab in {"groups", "channels", "bots"}:
            kind = {"groups": "group", "channels": "channel", "bots": "bot"}[self._tab]
            return [d for d in dialogs if d.kind == kind]
        return dialogs

    async def _load_dialogs(self) -> None:
        # #110 (Codex re-review): _all_dialogs holds the FULL source snapshot (every non-archived
        # dialog, or the archived set on the archive tab) — NOT the tab projection. _render_dialogs
        # projects it through _filter_by_tab, so a live touch that flips a dialog read<->unread is
        # reflected on the unread tab without a reload (the dialog stays in the snapshot either way).
        # archive vs the rest use DIFFERENT endpoints, so the snapshot SOURCE depends on the tab.
        requested_tab = self._tab  # capture the scope this load fetches under
        if requested_tab == "archive":
            archived_dialogs = getattr(self._client, "archived_dialogs", None)
            items = [] if archived_dialogs is None else await archived_dialogs()
        else:
            items = [dialog for dialog in await self._client.dialogs(dm_only=False) if not dialog.archived]
        self._all_dialogs = list(items)  # full source snapshot; _render_dialogs applies the tab filter
        self._loaded_tab = requested_tab  # remember the source this snapshot came from
        await self._render_dialogs()

    async def _render_dialogs(self) -> None:
        """Redraw the dialog list: project the full snapshot to the tab, then the search filter.

        Local and network-free: ``_filter_by_tab`` + ``filter_dialogs`` run over the already-fetched
        ``self._all_dialogs`` snapshot, never re-querying the client.
        """
        from tg_messenger.core.search import filter_dialogs

        lv = self.query_one("#dialogs", ListView)
        query = self.query_one("#search", Input).value
        selected_id = None
        if isinstance(lv.highlighted_child, DialogItem):
            selected_id = lv.highlighted_child.dialog_id
        # #110: project the full snapshot to the active tab here, so a live touch flipping a dialog
        # unread<->read drops/surfaces it on the "unread" tab without a reload.
        filtered = list(filter_dialogs(self._filter_by_tab(self._all_dialogs), query))
        await lv.clear()
        for d in filtered:
            await lv.append(DialogItem(d.id, d.title, d.unread, d.kind))
        if selected_id is not None:
            for idx, dialog in enumerate(filtered):
                if dialog.id == selected_id:
                    lv.index = idx
                    break

    async def _touch_dialog_for_message(self, dialog_id: int, message, *, incoming: bool) -> None:
        """Apply one live message to the current sidebar snapshot without reloading."""
        for idx, dialog in enumerate(self._all_dialogs):
            if dialog.id != dialog_id:
                continue
            unread = dialog.unread
            if incoming:
                unread = 0 if dialog_id == self._current else dialog.unread + 1
            updated = dialog.model_copy(
                update={
                    "unread": unread,
                    "last_text": message.text,
                    "last_message_at": message.date,
                }
            )
            self._all_dialogs = [updated, *self._all_dialogs[:idx], *self._all_dialogs[idx + 1:]]
            await self._render_dialogs()
            return

    async def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        # Tabs fires this once at mount, before the client exists — _started gates it.
        # (NOT named _ready: Textual's App already has a _ready coroutine.)
        # Network goes through a worker: awaiting here would stall the message pump.
        self._tab = event.tab.id or "all"
        # #110 (Codex re-review): clear a stale search filter BEFORE the _started guard — it needs
        # no network, and a tab switch during a slow connect would otherwise render the picked tab
        # with the old query still applied (the same empty-tab failure on the pre-startup path).
        # #search exists from compose(); resetting an already-empty value is a no-op.
        self.query_one("#search", Input).value = ""
        if not self._started:
            return
        self.run_worker(self._reload_dialogs(), group="dialogs", exclusive=True)

    async def _reload_dialogs(self) -> None:
        lv = self.query_one("#dialogs", ListView)
        await lv.clear()
        lv.loading = True
        try:
            await self._load_dialogs()
        except Exception as exc:
            logger.exception("dialog list reload failed (tab %s)", self._tab)
            self.notify(f"Dialogs failed: {exc}", severity="error")
        finally:
            lv.loading = False

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, DialogItem):
            self._open_dialog(item.dialog_id)

    def _open_dialog(self, dialog_id: int) -> None:
        # #124: the SYNCHRONOUS open — sets _current and all per-dialog state, then kicks the
        # history worker. Called both from on_list_view_selected (mouse/Enter selection, via the
        # posted ListView.Selected) AND directly from the arrow handoff paths (Down/Right). The
        # keyboard paths MUST commit synchronously: action_select_cursor only POSTS Selected, so
        # if the composer is focused before the pump drains it, a fast Enter/queued paste would
        # snapshot the stale _current and send to the previously-open dialog — a wrong-recipient
        # send (Codex finding). Setting _current here, before focus moves, closes that window.
        self._save_current_compose_state()
        self._current = dialog_id
        # #108: capture the kind now, while this dialog's <li> is in _all_dialogs — a later
        # tab switch can drop it from the list, but the open chat keeps showing author lines.
        self._current_kind = self._dialog_kind(dialog_id)
        self._restore_compose_state(dialog_id)
        self._apply_composer_writable(dialog_id)
        self._clear_suggestion()
        self._bubble_index.clear()
        self._pending_reactions.clear()  # #106: drop any buffered reactions from the prior dialog
        # exclusive group: selecting another dialog cancels a still-loading history
        self.run_worker(self._show_history(dialog_id), group="history", exclusive=True)

    async def _mark_read(self, dialog_id: int, max_id: int) -> None:
        try:
            await self._client.mark_read(dialog_id, max_id=max_id)
        except Exception:
            logger.warning("mark_read failed (dialog %s) — continuing", dialog_id, exc_info=True)

    def _message_bubble_for(self, message, dialog_id: int) -> MessageBubble:
        """Build and index the one TUI bubble representation for a core message."""
        show_author = (not message.out) and self._kind_for_rendering(dialog_id) == "group"
        bubble = MessageBubble(
            _message_body(message),
            message.out,
            message_id=message.id,
            dialog_id=dialog_id,
            author=_message_author_line(message, show_author=show_author),
        )
        if message.translated_text:
            bubble.show_translation(message.translated_text)
        self._bubble_index[message.id] = bubble
        return bubble

    async def _show_history(self, dialog_id: int) -> None:
        pane = self.query_one("#messages", Vertical)
        await pane.remove_children()
        pane.loading = True
        try:
            if self._store is not None:
                messages = await self._store.history(dialog_id, limit=50)
            else:
                messages = await self._client.history(dialog_id, limit=50)
        except Exception as exc:
            pane.loading = False
            logger.exception("history load failed (dialog %s)", dialog_id)
            self.notify(f"History failed: {exc}", severity="error")
            return
        pane.loading = False
        bubbles = []
        self._bubble_index.clear()
        for m in messages:
            bubbles.append(self._message_bubble_for(m, dialog_id))
        await pane.mount(*(_wrap_bubble(b) for b in bubbles))  # #118: align via wrapper row
        # #106: apply any reactions that arrived while this history was loading.
        self._drain_pending_reactions(dialog_id)
        self._scroll_messages_to_end(pane)
        if self._translator is not None:
            self.run_worker(
                self._translate_history_bubbles(dialog_id, messages),
                group="translate-history",
                exclusive=True,
            )
        if messages:
            # Acknowledge exactly the loaded snapshot; messages arriving later stay unread.
            self.run_worker(
                self._mark_read(dialog_id, max(m.id for m in messages)),
                group="mark_read",
                exclusive=False,
            )

    async def _translate_history_bubbles(self, dialog_id: int, messages) -> None:
        try:
            translated = await self._translator.translate_history(dialog_id, messages)
        except Exception:
            logger.exception("history translation failed (dialog %s)", dialog_id)
            return
        if dialog_id != self._current:
            return
        for message in translated:
            if not message.translated_text:
                continue
            bubble = self._bubble_index.get(message.id)
            if bubble is not None:
                bubble.show_translation(message.translated_text)

    def action_translate_all(self) -> None:
        """Ctrl+T — translate the whole open chat (up to the configured cap), with a spinner."""
        if self._translator is None:
            self.notify("Переводчик не настроен", severity="warning")
            return
        if self._current is None:
            self.notify("Нет открытого диалога", severity="warning")
            return
        self.run_worker(
            self._translate_whole_dialog(self._current),
            group="translate-all",
            exclusive=True,
        )

    async def _translate_whole_dialog(self, dialog_id: int) -> None:
        """Reload up to N recent messages and translate them in one pass, showing a spinner.

        Long by design (a reasoning model over a whole chat) — the #messages pane shows its loading
        spinner for the duration. A failed/empty pass is surfaced via notify (the Ctrl+T path is
        explicit, unlike the silent background auto-translate on open).
        """
        pane = self.query_one("#messages", Vertical)
        # the built-in loading overlay shows only dots; mount a labelled status line instead so it's
        # clear WHAT is happening for the whole (possibly minute-long) pass.
        await pane.remove_children()
        self._bubble_index.clear()
        await pane.mount(Static("⏳ Идёт перевод…", id="translate-status", classes="translate-status"))
        ok = False
        try:
            cap = await self._translator.max_messages()
            if self._store is not None:
                messages = await self._store.history(dialog_id, limit=cap)
            else:
                messages = await self._client.history(dialog_id, limit=cap)
            translated = await self._translator.translate_history(dialog_id, messages)
            if dialog_id == self._current:
                # drop the status line, then mount the (now translated) snapshot
                await pane.remove_children()
                self._bubble_index.clear()
                bubbles = [self._message_bubble_for(m, dialog_id) for m in messages]
                await pane.mount(*(_wrap_bubble(b) for b in bubbles))
                self._scroll_messages_to_end(pane)
            ok = any(m.translated_text for m in translated)
        except Exception:
            logger.exception("whole-chat translation failed (dialog %s)", dialog_id)
        if dialog_id == self._current:
            # if the status line is still up (error path), clear it so the pane isn't stuck
            for stale in pane.query("#translate-status"):
                await stale.remove()
        if not ok and dialog_id == self._current:
            self.notify("Перевод не удался — см. лог", severity="warning")

    async def _translate_bubble(self, dialog_id: int, message, bubble: MessageBubble) -> None:
        try:
            translated = await self._translator.translate_message(message)
        except Exception:
            logger.exception("live translation failed (dialog %s)", dialog_id)
            return
        if dialog_id == self._current and translated.translated_text:
            bubble.show_translation(translated.translated_text)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "composer":
            # Textual captures Changed.value when posting; by handler time a
            # newer programmatic composer value may already be present.
            if event.value != event.input.value:
                return
            # the user is typing their own reply — a stale suggestion must go
            if self._pending_suggestion is not None and event.value != self._pending_suggestion:
                self._clear_suggestion()
            if self._current is not None:
                state = self._compose_state_for(self._current)
                if not event.value and state.ignore_next_empty_change:
                    state.ignore_next_empty_change = False
                    return
                previous_draft = state.draft
                state.draft = event.value
                if state.source_text is not None and event.value != previous_draft:
                    state.source_text = None
                    state.outbound_token = None  # edited draft invalidates the variant (#73)
                if state.original_confirm_text is not None and event.value != state.original_confirm_text:
                    state.original_confirm_text = None
            if not event.value:
                if self._current is not None:
                    self._clear_pending_outbound(self._current)
            return
        # only the search box filters; the composer's own changes are ignored.
        # Filtering is local (over self._all_dialogs) — no network, safe to await here.
        if event.input.id != "search":
            return
        await self._render_dialogs()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # the search box submits nothing — only the composer sends messages
        if event.input.id == "search":
            return
        if self._current is None or not event.value.strip():
            return
        text = event.value
        dialog_id = self._current
        state = self._compose_state_for(dialog_id)
        state.draft = text
        # #93: reactions are placed via the per-message "r" hotkey (EmojiPickerScreen), not a
        # composer command — the composer is for text/media/lang/outbound only.
        if not self._dialog_can_send(dialog_id):
            # belt-and-suspenders with the disabled composer (cache may be momentarily stale)
            self.notify(READ_ONLY_MESSAGE, severity="warning")
            return
        try:
            lang_command = parse_lang_command(text)
        except ValueError as exc:
            logger.warning("bad lang command in dialog %s: %s", dialog_id, exc)
            self.notify(str(exc), severity="error")
            return
        if lang_command is not None:
            self._optimistic_clear(dialog_id, event.input)
            self.run_worker(
                self._apply_lang_command(dialog_id, lang_command),
                group="outbound-lang",
                exclusive=True,
            )
            return
        parsed = parse_media_command(text)
        if parsed is not None:
            path, caption = parsed
            if not os.path.isfile(path):
                # validate BEFORE the worker/network; surface, don't send
                logger.warning("media path not found: %s (dialog %s)", path, dialog_id)
                self.notify(f"File not found: {path}", severity="error")
                return
            self._optimistic_clear(dialog_id, event.input)
            self.run_worker(
                self._send_media(dialog_id, path, caption, source_text=text),
                exclusive=False,
            )
            return
        if state.source_text is not None:
            source = state.source_text
            token = state.outbound_token
            self._optimistic_clear(dialog_id, event.input)
            self.run_worker(
                self._send_text(dialog_id, text, source_text=source, token=token),
                exclusive=False,
            )
            return
        if state.original_confirm_text == text:
            self._optimistic_clear(dialog_id, event.input)
            self.run_worker(self._send_text(dialog_id, text), exclusive=False)
            return
        if self._outbound is not None:
            state.ignore_next_empty_change = True
            event.input.value = ""
            state.draft = text
            self.run_worker(
                self._outbound_flow(dialog_id, text),
                group="outbound",
                exclusive=True,
            )
            return
        self._optimistic_clear(dialog_id, event.input)
        self.run_worker(self._send_text(dialog_id, text), exclusive=False)

    def _remember_sent(self, dialog_id: int, message_id: int) -> None:
        """Record a message we sent so its listen_outgoing() echo isn't drawn twice."""
        key = (dialog_id, message_id)
        self._sent_ids[key] = True
        self._sent_ids.move_to_end(key)
        while len(self._sent_ids) > 200:  # bounded, like watch.py's caches
            self._sent_ids.popitem(last=False)

    def _remember_sent_reaction(self, dialog_id: int, message_id: int, emoticon: str | None) -> None:
        """Record a reaction we sent so its listen_reactions() echo isn't drawn twice."""
        key = (dialog_id, message_id, emoticon)
        self._sent_reactions[key] = True
        self._sent_reactions.move_to_end(key)
        while len(self._sent_reactions) > 200:  # bounded, like watch.py's caches
            self._sent_reactions.popitem(last=False)

    def _apply_reaction(self, dialog_id: int, message_id: int, emoticon: str | None) -> None:
        # #106: attach the reaction UNDER its target message (the translation pattern), not as
        # a separate bubble.
        bubble = self._bubble_index.get(message_id)
        # Defense-in-depth (Codex review): _bubble_index is keyed by bare message_id, which is
        # not unique across dialogs. on_list_view_selected clears the index synchronously and the
        # history worker is exclusive, so a stale bubble from a prior dialog should never remain —
        # but verify the bubble's own SOURCE dialog matches before attaching, so a colliding id
        # can never render under the wrong chat's message even if that invariant is ever broken.
        if bubble is not None:
            if bubble.dialog_id == dialog_id:
                bubble.add_reaction(emoticon)
            # else: a same-id bubble from a different dialog (the invariant says this can't
            # happen — see above) — drop rather than mis-attach or buffer indefinitely.
            return
        # Target not in the index. Two cases: (a) for the OPEN dialog whose history is still
        # loading — the bubble will exist in a moment, so buffer and replay after _show_history
        # mounts (Codex review: don't drop a live reaction in that window); (b) any other case
        # (a different dialog, or a message scrolled out of the loaded snapshot) — silently
        # ignore, mirroring how translations no-op.
        if dialog_id != self._current:
            return
        pending = self._pending_reactions.setdefault(dialog_id, [])
        pending.append((message_id, emoticon))
        while len(pending) > 100:  # bounded, like the other in-memory caches
            pending.pop(0)

    def _drain_pending_reactions(self, dialog_id: int) -> None:
        # #106: replay reactions buffered while this dialog's history was loading. Apply onto
        # the freshly-mounted bubbles (a missing target now means it really isn't in the
        # snapshot → dropped, not re-buffered, so this can't loop).
        for message_id, emoticon in self._pending_reactions.pop(dialog_id, []):
            bubble = self._bubble_index.get(message_id)
            if bubble is not None:
                bubble.add_reaction(emoticon)

    async def _apply_lang_command(self, dialog_id: int, command: tuple[str, str | None]) -> None:
        if self._outbound is None:
            self.notify("Outbound translation is not configured.", severity="warning")
            return
        from tg_messenger.agent.outbound import set_dialog_lang, set_outbound_enabled

        action, value = command
        try:
            if action == "auto":
                await set_dialog_lang(self._outbound.storage, dialog_id, None)
            elif action == "on":
                await set_outbound_enabled(self._outbound.storage, dialog_id, True)
            elif action == "off":
                await set_outbound_enabled(self._outbound.storage, dialog_id, False)
            elif action == "set" and value is not None:
                await set_dialog_lang(self._outbound.storage, dialog_id, value, source="manual")
        except ValueError as exc:
            logger.warning("invalid dialog language code: %s", exc)
            self.notify(str(exc), severity="error")
            return
        except Exception as exc:
            logger.exception("dialog language command failed")
            self.notify(f"Language setting failed: {exc}", severity="error")
            return
        self.notify("Language setting saved.")

    async def _outbound_flow(self, dialog_id: int, text: str) -> None:
        # #73: the coordinator owns prepare (applies+variants), the timeout AND the token —
        # the TUI no longer calls applies()/variants() directly, and there is one timeout
        # budget inside prepare() instead of a separate asyncio.wait_for here.
        composer = self.query_one("#composer", Input)
        state = self._compose_state_for(dialog_id)
        state.draft = text
        telegram_lang_code = self._dialog_telegram_lang_hint(dialog_id)
        result = await self._coordinator.prepare(
            dialog_id, text, telegram_lang_code=telegram_lang_code, owner_id=str(dialog_id)
        )
        if result.status == "not_applicable":
            state.draft = ""
            self._clear_pending_outbound(dialog_id)
            if dialog_id == self._current and composer.value == text:
                composer.value = ""
            await self._send_text(dialog_id, text)
            return
        if result.status != "ready":
            # disabled / invalid_empty / error → fall back to sending the original on Enter
            state.draft = text
            state.source_text = None
            state.outbound_token = None
            state.original_confirm_text = text
            if dialog_id == self._current:
                composer.value = text
            if result.status == "error":
                self.notify("Перевод не удался — Enter отправит оригинал", severity="warning")
            return
        picked = await self.push_screen_wait(VariantPickScreen(result.variants, text))
        if picked is None:
            if dialog_id == self._current:
                composer.value = text
                composer.focus()
            return
        if picked == ORIGINAL_SENTINEL:
            state.draft = text
            state.source_text = None
            state.outbound_token = None
            state.original_confirm_text = text
            self.notify("Enter отправит оригинал")
            if dialog_id == self._current:
                composer.value = text
                composer.focus()
            return
        state.draft = picked
        state.source_text = text
        state.outbound_token = result.token  # consumed on the send that follows
        state.original_confirm_text = None
        if dialog_id == self._current:
            composer.value = picked
            composer.focus()

    async def _send_text(
        self, peer: int, text: str, source_text: str | None = None, token: str | None = None,
    ) -> None:
        async def _do_send(target, body):
            return await self._client.send_text(target, body)

        try:
            if token is not None:
                # variant path: coordinator validates the token, records the source itself
                msg = await self._coordinator.send_variant(
                    peer, token, text, _do_send, owner_id=str(peer)
                )
            else:
                msg = await self._coordinator.send_original(peer, text, _do_send)
        except OutboundError:
            logger.warning("outbound token rejected on send (dialog %s)", peer)
            self.notify("Выбор перевода истёк — выберите вариант заново", severity="warning")
            self._restore_draft(peer, text)
            return
        except SendForbiddenError as exc:
            # TOCTOU net: composer was enabled but Telegram rejected the write on rights.
            # Surface the specific reason (#92), not a fixed line.
            logger.warning("send rejected (rights) (dialog %s): %s", peer, exc)
            self.notify(str(exc), severity="warning")
            self._restore_draft(peer, text)
            self._apply_composer_writable(peer)  # reflect the now-known read-only state
            return
        except Exception as exc:
            logger.exception("send failed (dialog %s)", peer)
            self.notify(f"Send failed: {exc}", severity="error")
            self._restore_draft(peer, text)
            return
        self._clear_compose_state_after_send(peer, text)
        self._remember_sent(peer, msg.id)  # suppress the echo from listen_outgoing()
        await self._touch_dialog_for_message(peer, msg, incoming=False)
        if peer == self._current:  # user may have switched dialogs mid-send
            pane = self.query_one("#messages", Vertical)
            bubble = self._message_bubble_for(msg, peer)
            await pane.mount(_wrap_bubble(bubble))
            self._scroll_messages_to_end(pane)

    async def _send_media(
        self, peer: int, path: str, caption: str | None, source_text: str | None = None,
    ) -> None:
        # source_text is the original "@file ... caption" command, cleared optimistically
        # in on_input_submitted — _restore_draft puts it back on failure (#89).
        try:
            msg = await self._client.send_media(peer, path, caption=caption)
        except SendForbiddenError as exc:
            logger.warning("send media rejected (rights) (dialog %s): %s", peer, exc)
            self.notify(str(exc), severity="warning")
            self._restore_draft(peer, source_text)
            self._apply_composer_writable(peer)  # reflect the now-known read-only state
            return
        except Exception as exc:
            logger.exception("send media failed (dialog %s, %s)", peer, path)
            self.notify(f"Send failed: {exc}", severity="error")
            self._restore_draft(peer, source_text)
            return
        self._remember_sent(peer, msg.id)  # suppress the echo from listen_outgoing()
        await self._touch_dialog_for_message(peer, msg, incoming=False)
        if peer == self._current:  # user may have switched dialogs mid-send
            pane = self.query_one("#messages", Vertical)
            bubble = self._message_bubble_for(msg, peer)
            await pane.mount(_wrap_bubble(bubble))
            self._scroll_messages_to_end(pane)

    async def _react_from_bubble(self, peer: int, message_id: int) -> None:
        # #93: the "r" hotkey path — open the 4-emoji picker, then send. No composer text
        # is involved, so _send_reaction is called with source_text=None (restore no-ops).
        emoticon = await self.push_screen_wait(EmojiPickerScreen())
        if emoticon is None:
            return  # picker dismissed (Escape) — nothing sent
        await self._send_reaction(peer, message_id, emoticon)

    async def _send_reaction(
        self, peer: int, message_id: int, emoticon: str, source_text: str | None = None,
    ) -> None:
        # source_text is unused by the "r" hotkey path (always None — nothing in the composer
        # to restore); kept for signature stability. Reactions are NOT gated by posting
        # permission, so no _apply_composer_writable here (unlike text/media).
        try:
            await self._client.send_reaction(peer, message_id, emoticon)
        except SendForbiddenError as exc:
            logger.warning(
                "reaction rejected (rights) (dialog %s, message %s): %s", peer, message_id, exc
            )
            self.notify(str(exc), severity="warning")
            self._restore_draft(peer, source_text)
            return
        except Exception as exc:
            logger.exception("send reaction failed (dialog %s, message %s)", peer, message_id)
            self.notify(f"Reaction failed: {exc}", severity="error")
            self._restore_draft(peer, source_text)
            return
        self._remember_sent_reaction(peer, message_id, emoticon)
        if peer == self._current:
            # #106: attach under the reacted message instead of a separate bubble.
            self._apply_reaction(peer, message_id, emoticon)
        else:
            # #105: the reaction landed in a dialog the user has navigated away from — the
            # in-pane echo bubble would contaminate the wrong chat, so confirm with a transient
            # toast instead (parity with web #103/#97). Title is best-effort — neutral fallback.
            title = self._dialog_title(peer)
            self.notify(
                f"Реакция в {title} {emoticon}" if title else f"Реакция отправлена {emoticon}"
            )

    async def _drain_incoming(self) -> None:
        try:
            async for ev in self._client.listen_all():  # groups too, not just DMs
                await self._touch_dialog_for_message(ev.dialog_id, ev.message, incoming=True)
                if ev.dialog_id == self._current:
                    pane = self.query_one("#messages", Vertical)
                    bubble = self._message_bubble_for(ev.message, ev.dialog_id)
                    await pane.mount(_wrap_bubble(bubble))
                    if not ev.message.translated_text and self._translator is not None:
                        self.run_worker(
                            self._translate_bubble(ev.dialog_id, ev.message, bubble),
                            group="translate-live",
                            exclusive=False,
                        )
                    self._scroll_messages_to_end(pane)
                    self.run_worker(
                        self._mark_read(ev.dialog_id, ev.message.id),
                        group="mark_read",
                        exclusive=True,
                    )
                    self._maybe_suggest(ev.dialog_id)
        except Exception:
            logger.exception("incoming listener failed")
            self.notify("Incoming listener failed — see log.", severity="error")

    async def _drain_outgoing(self) -> None:
        """Render our OWN messages sent from another device (phone/CLI/web).

        Mirrors _drain_incoming but as out=True and with NO suggestion (those are
        for incoming only). Echoes of messages we just sent from this composer are
        in _sent_ids and skipped, so they aren't drawn twice.
        """
        try:
            async for ev in self._client.listen_outgoing():  # own messages, any device
                if (ev.dialog_id, ev.message.id) in self._sent_ids:
                    continue  # our own optimistic bubble already shows it
                await self._touch_dialog_for_message(ev.dialog_id, ev.message, incoming=False)
                if ev.dialog_id == self._current:
                    pane = self.query_one("#messages", Vertical)
                    bubble = self._message_bubble_for(ev.message, ev.dialog_id)
                    await pane.mount(_wrap_bubble(bubble))
                    if not ev.message.translated_text and self._translator is not None:
                        self.run_worker(
                            self._translate_bubble(ev.dialog_id, ev.message, bubble),
                            group="translate-live",
                            exclusive=False,
                        )
                    self._scroll_messages_to_end(pane)
        except Exception:
            logger.exception("outgoing listener failed")
            self.notify("Outgoing listener failed — see log.", severity="error")

    async def _drain_reactions(self) -> None:
        try:
            async for ev in self._client.listen_reactions():
                key = (ev.dialog_id, ev.message_id, ev.emoticon)
                if key in self._sent_reactions:
                    self._sent_reactions.pop(key, None)
                    continue  # our own optimistic reaction is already shown under the message
                if ev.dialog_id == self._current:
                    # #106: other people's reactions attach under the reacted message too.
                    self._apply_reaction(ev.dialog_id, ev.message_id, ev.emoticon)
        except Exception:
            logger.exception("reaction listener failed")
            self.notify("Reaction listener failed — see log.", severity="error")

    def _maybe_suggest(self, dialog_id: int) -> None:
        """Kick off a reply suggestion for an incoming message in the open dialog.

        No-op when the feature is off (no suggester) or the user is already typing
        — we never clobber a half-written reply. Network/LLM runs in a worker.
        """
        if self._suggester is None:
            return
        if not self._is_dm_dialog(dialog_id):
            return
        if self.query_one("#composer", Input).value:
            return  # don't suggest over a draft the user is writing
        self.run_worker(self._suggest(dialog_id), group="suggest", exclusive=True)

    def _is_dm_dialog(self, dialog_id: int) -> bool:
        return any(d.id == dialog_id and d.kind == "dm" for d in self._all_dialogs)

    def _dialog_kind(self, dialog_id: int) -> str | None:
        # #108: kind from the already-loaded list (no network) — author lines show only in groups.
        return next((d.kind for d in self._all_dialogs if d.id == dialog_id), None)

    def _kind_for_rendering(self, dialog_id: int) -> str | None:
        # #108 (Codex review): the kind to drive the author-line decision for a rendered message.
        # For the OPEN dialog prefer the kind captured at selection time (_current_kind) — it
        # stays correct after a tab switch drops the dialog from _all_dialogs. Fall back to a
        # fresh list lookup when it wasn't captured (dialog still in the list) or for a non-open
        # dialog; the captured value wins only when present.
        if dialog_id == self._current and self._current_kind is not None:
            return self._current_kind
        return self._dialog_kind(dialog_id)

    def _dialog_title(self, dialog_id: int) -> str | None:
        # #105: best-effort title from the already-loaded dialog list (no network — flood
        # discipline), for the cross-dialog reaction toast. None → the caller falls back to a
        # neutral confirmation. Mirrors web dialogTitleById (#103).
        return next((d.title for d in self._all_dialogs if d.id == dialog_id), None)

    def _dialog_can_send(self, dialog_id: int) -> bool:
        # #90: the one shared POST-capability rule over the already-loaded dialog list
        # (unknown dialog → fail-safe writable, matches core).
        return can_send_in(self._all_dialogs, dialog_id)

    def _apply_composer_writable(self, dialog_id: int) -> None:
        """Disable the composer in a read-only chat (mirrors the real Telegram client)."""
        composer = self.query_one("#composer", Input)
        can = self._dialog_can_send(dialog_id)
        composer.disabled = not can
        composer.placeholder = "Message…" if can else "Только чтение"

    def _dialog_telegram_lang_hint(self, dialog_id: int) -> str | None:
        for dialog in self._all_dialogs:
            if dialog.id == dialog_id:
                return getattr(dialog, "telegram_lang_code", None)
        return None

    async def _suggest(self, dialog_id: int) -> None:
        try:
            draft = await self._suggester.suggest(dialog_id)
        except Exception:
            logger.exception("suggest failed (dialog %s)", dialog_id)
            return
        # the user may have switched dialogs or started typing while we waited
        if dialog_id != self._current or self.query_one("#composer", Input).value:
            return
        self._pending_suggestion = draft
        self.query_one("#suggestion", Static).update(f"{SUGGEST_PREFIX}{draft}" if draft else "")

    def action_accept_suggestion(self) -> None:
        """Tab: accept a pending suggestion into the composer, else fall through to forward
        focus cycling (#114 — the unified scheme; Shift+Tab cycles backward)."""
        if not self._pending_suggestion:
            # nothing to accept: hand Tab back to normal forward focus traversal
            self.screen.focus_next()
            return
        composer = self.query_one("#composer", Input)
        composer.value = self._pending_suggestion
        self._clear_suggestion()
        composer.focus()

    def _clear_suggestion(self) -> None:
        self._pending_suggestion = None
        self.query_one("#suggestion", Static).update("")

    def action_toggle_help(self) -> None:
        """? / F1 — toggle the key-help overlay (#124).

        The SAME key opens and closes it: if HelpScreen is already on top, pop it; else push it.
        (HelpScreen also binds ?/F1/Escape to dismiss, so pressing them while it's open closes it
        via its own bindings — the isinstance guard here covers the app-side path defensively.)
        """
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        elif isinstance(self.screen, ModalScreen):
            # #124 cleanup: F1 is a priority binding, so it fires even over another open modal
            # (login, emoji picker, account settings…). Don't stack HelpScreen on top of it — the
            # underlying modal owns the screen; do nothing rather than bury it under help.
            return
        else:
            self.push_screen(HelpScreen())

    def action_clear_search(self) -> None:
        """Escape — clear a non-empty search filter and re-render (a small, predictable global).

        A no-op when the search box is already empty, so Escape stays unsurprising elsewhere.
        """
        search = self.query_one("#search", Input)
        if not search.value:
            return
        search.value = ""  # triggers on_input_changed → _render_dialogs

    def action_open_settings(self) -> None:
        """Ctrl+S — account settings (#115) + inbound-translation settings when a Translator is wired."""
        self.run_worker(self._open_settings(), exclusive=True)

    async def _open_settings(self) -> None:
        store = self._session_store or session_store_from_env()
        # the settings screen dismisses with a rebuilt Translator when the user changes the model;
        # swap it in so live/history/whole-chat translation uses the new model. Otherwise it's None.
        result = await self.push_screen_wait(
            AccountsScreen(
                profiles=store.list_profiles(),
                active=self._session_name,
                store=store,
                account_client_factory=self._account_client_factory,
                translator=self._translator,
            )
        )
        if result is not None:
            self._translator = result
