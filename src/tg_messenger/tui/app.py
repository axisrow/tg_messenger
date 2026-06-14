"""Textual TUI: dialog list (left, Telegram-style tabs) + message view + composer.

Reuses the bubble/list pattern. A background worker drains ``client.listen_all()``
and appends incoming bubbles for the selected dialog (any kind, groups included).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from collections import OrderedDict
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static, Tab, Tabs

from tg_messenger.agent.outbound_coordinator import OutboundError, OutboundSendCoordinator
from tg_messenger.core.auth import LoginError, LoginSession, delivery_hint
from tg_messenger.core.client import READ_ONLY_MESSAGE, SendForbiddenError

logger = logging.getLogger(__name__)

# shown before a draft in the suggestion strip; Tab accepts it into the composer
SUGGEST_PREFIX = "💡 Tab: "
ORIGINAL_SENTINEL = "__tg_messenger_original__"
# #73: the outbound prepare timeout now lives in OutboundSendCoordinator.


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


def parse_reaction_command(text: str) -> tuple[int, str] | None:
    """Parse ``/react MESSAGE_ID EMOTICON`` from the composer."""
    parts = text.split(maxsplit=2)
    if not parts or parts[0] != "/react":
        return None
    if len(parts) != 3:
        raise ValueError("usage: /react MESSAGE_ID EMOTICON")
    if not parts[1].isdigit():
        raise ValueError("message id must be a positive integer")
    emoticon = parts[2].strip()
    if not emoticon:
        raise ValueError("reaction cannot be empty")
    return int(parts[1]), emoticon


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


def _message_label(message) -> str:
    return f"[{message.id}] {message.text or '<media>'}"


def _reaction_emoticon(emoticon: str | None) -> str:
    return emoticon if emoticon is not None else "<custom>"


def _reaction_label(event) -> str:
    return f"reaction [{event.message_id}]: {_reaction_emoticon(event.emoticon)}"


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
    ]

    def action_focus_dialogs(self) -> None:
        self.screen.query_one("#dialogs", ListView).focus()


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
    ]

    def on_focus(self) -> None:
        if self.index is None and len(self) > 0:
            self.index = 0

    def action_cursor_up_or_tabs(self) -> None:
        if self.index in (None, 0):
            self.screen.focus_previous()  # the tabs are the prior focusable
        else:
            self.action_cursor_up()


class DialogItem(ListItem):
    def __init__(self, dialog_id: int, title: str, unread: int = 0, kind: str = "dm"):
        # markup=False: titles/messages are untrusted text, [brackets] must render literally
        badge = f" ({unread})" if unread else ""
        super().__init__(Static(f"{dialog_id} — {title}{badge}", markup=False))
        self.dialog_id = dialog_id
        self.kind = kind


class MessageBubble(Static):
    def __init__(self, text: str, out: bool):
        self._base_text = text
        super().__init__(text, classes="out" if out else "in", markup=False)

    def show_translation(self, text: str) -> None:
        self.update(f"{self._base_text}\n↳ {text}")


class VariantItem(ListItem):
    def __init__(self, label: str, value: str):
        super().__init__(Static(label, markup=False))
        self.value = value


class VariantPickScreen(ModalScreen[str | None]):
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

    def key_escape(self) -> None:
        self.dismiss(None)


class MessengerTUI(App):
    # priority: quitting must work even while focus sits in the composer Input.
    # Tab accepts a pending reply suggestion (priority so the Input doesn't eat it
    # for focus traversal); the binding is a no-op when there's nothing to accept.
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("tab", "accept_suggestion", "Accept suggestion", priority=True, show=False),
    ]

    CSS = """
    #sidebar { width: 32; border-right: solid $primary; }
    #messages { overflow-y: auto; }
    #messages MessageBubble { width: 100%; text-wrap: wrap; text-overflow: fold; }
    .out { color: $accent; text-align: right; }
    .in { color: $text; }
    #suggestion { dock: bottom; color: $text-muted; height: auto; }
    #composer { dock: bottom; }
    """

    def __init__(self, *, client=None, session_name: str = "default",
                 profiles: list[str] | None = None, client_factory=None, deps_factory=None,
                 suggester=None, login_session=None, store=None, translator=None,
                 outbound=None):
        super().__init__()
        self._client = client
        self._session_name = session_name
        self._profiles = profiles or []
        # login wizard state machine (test seam); built from the client otherwise
        self._login_session = login_session
        # how a client is built once a profile is chosen (injectable for tests)
        self._client_factory = client_factory or _make_real_client
        # #52: builds the WHOLE dependency set (client + suggester/store/translator/
        # outbound) for a profile chosen via ProfileScreen — used by the `tui` entrypoint
        # so the in-app picker, not a CLI menu, resolves the profile. None = library path.
        self._deps_factory = deps_factory
        self._current: int | None = None
        self._tab = "all"
        self._started = False  # gates tab events until _startup finished
        self._all_dialogs: list = []  # full loaded list; search filters it locally
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
                yield Input(placeholder="Поиск…", id="search")
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
            with Vertical():
                yield Vertical(id="messages")
                yield Static("", id="suggestion", markup=False)
                yield Input(placeholder="Message…", id="composer")

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
                await self._store.connect()
                self.run_worker(self._run_store(), group="message-store", exclusive=False)
        except Exception as exc:
            logger.exception("TUI startup failed")
            self.exit(return_code=1, message=f"Startup failed: {exc}")
            return
        self.query_one("#dialogs", ListView).loading = False
        self._started = True
        self.run_worker(self._drain_incoming(), exclusive=False)
        self.run_worker(self._drain_outgoing(), exclusive=False)
        self.run_worker(self._drain_reactions(), exclusive=False)

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

    async def _load_dialogs(self) -> None:
        if self._tab == "archive":
            archived_dialogs = getattr(self._client, "archived_dialogs", None)
            if archived_dialogs is None:
                items = []
            else:
                items = await archived_dialogs()
        elif self._tab in {"contacts", "non_contacts", "groups", "channels", "bots", "unread"}:
            all_dialogs = [dialog for dialog in await self._client.dialogs(dm_only=False) if not dialog.archived]
            if self._tab == "contacts":
                items = [dialog for dialog in all_dialogs if dialog.kind == "dm" and dialog.is_contact is True]
            elif self._tab == "non_contacts":
                items = [dialog for dialog in all_dialogs if dialog.kind == "dm" and dialog.is_contact is False]
            elif self._tab == "unread":
                items = [dialog for dialog in all_dialogs if dialog.unread > 0]
            else:
                kind_by_tab = {"groups": "group", "channels": "channel", "bots": "bot"}
                kind = kind_by_tab[self._tab]
                items = [dialog for dialog in all_dialogs if dialog.kind == kind]
        else:
            items = [dialog for dialog in await self._client.dialogs(dm_only=False) if not dialog.archived]
        self._all_dialogs = list(items)  # keep the full list for local search
        await self._render_dialogs()

    async def _render_dialogs(self) -> None:
        """Redraw the dialog list from the cached full list, applying the search filter.

        Local and network-free: filtering happens over ``self._all_dialogs`` (already
        fetched), never re-querying the client.
        """
        from tg_messenger.core.search import filter_dialogs

        lv = self.query_one("#dialogs", ListView)
        query = self.query_one("#search", Input).value
        selected_id = None
        if isinstance(lv.highlighted_child, DialogItem):
            selected_id = lv.highlighted_child.dialog_id
        filtered = list(filter_dialogs(self._all_dialogs, query))
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
            self._save_current_compose_state()
            self._current = item.dialog_id
            self._restore_compose_state(item.dialog_id)
            self._apply_composer_writable(item.dialog_id)
            self._clear_suggestion()
            self._bubble_index.clear()
            # exclusive group: selecting another dialog cancels a still-loading history
            self.run_worker(self._show_history(item.dialog_id), group="history", exclusive=True)

    async def _mark_read(self, dialog_id: int, max_id: int) -> None:
        try:
            await self._client.mark_read(dialog_id, max_id=max_id)
        except Exception:
            logger.warning("mark_read failed (dialog %s) — continuing", dialog_id, exc_info=True)

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
            bubble = MessageBubble(_message_label(m), m.out)
            if m.translated_text:
                bubble.show_translation(m.translated_text)
            self._bubble_index[m.id] = bubble
            bubbles.append(bubble)
        await pane.mount(*bubbles)
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
        # #86: reactions are NOT gated by posting permission — parse /react and dispatch it
        # BEFORE the can_send guard so a read-only chat can still react. A rights rejection is
        # handled inside _send_reaction (notify + restore the typed command). The per-message
        # reaction hotkey UI is deferred to a sub-issue (the TUI has no message-selection
        # mechanism yet); the /react composer command stays for now.
        try:
            reaction = parse_reaction_command(text)
        except ValueError as exc:
            logger.warning("bad reaction command in dialog %s: %s", dialog_id, exc)
            self.notify(str(exc), severity="error")
            return
        if reaction is not None:
            message_id, emoticon = reaction
            state.draft = ""
            self._clear_pending_outbound(dialog_id)
            event.input.value = ""  # clear optimistically; restored on failure
            self.run_worker(
                self._send_reaction(dialog_id, message_id, emoticon, source_text=text),
                exclusive=False,
            )
            return
        # The posting-permission gate applies to everything that is NOT a reaction
        # (text / lang / media / outbound).
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
            state.draft = ""
            self._clear_pending_outbound(dialog_id)
            event.input.value = ""
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
            state.draft = ""
            self._clear_pending_outbound(dialog_id)
            event.input.value = ""  # clear optimistically; restored on failure
            self.run_worker(
                self._send_media(dialog_id, path, caption, source_text=text),
                exclusive=False,
            )
            return
        if state.source_text is not None:
            source = state.source_text
            token = state.outbound_token
            state.draft = ""
            self._clear_pending_outbound(dialog_id)
            event.input.value = ""
            self.run_worker(
                self._send_text(dialog_id, text, source_text=source, token=token),
                exclusive=False,
            )
            return
        if state.original_confirm_text == text:
            state.draft = ""
            self._clear_pending_outbound(dialog_id)
            event.input.value = ""
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
        state.draft = ""
        self._clear_pending_outbound(dialog_id)
        event.input.value = ""  # clear optimistically; restored on failure
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
            state = self._compose_state_for(peer)
            state.draft = text
            if peer == self._current:
                composer = self.query_one("#composer", Input)
                if not composer.value:
                    composer.value = text
            return
        except SendForbiddenError:
            # TOCTOU net: composer was enabled but Telegram rejected the write on rights
            logger.warning("send rejected (rights) (dialog %s)", peer)
            self.notify(READ_ONLY_MESSAGE, severity="warning")
            state = self._compose_state_for(peer)
            state.draft = text  # restore — the optimistic clear must not lose the message
            if peer == self._current:
                composer = self.query_one("#composer", Input)
                if not composer.value:  # don't clobber a draft typed meanwhile
                    composer.value = text
            self._apply_composer_writable(peer)  # reflect the now-known read-only state
            return
        except Exception as exc:
            logger.exception("send failed (dialog %s)", peer)
            self.notify(f"Send failed: {exc}", severity="error")
            state = self._compose_state_for(peer)
            state.draft = text
            if peer == self._current:
                composer = self.query_one("#composer", Input)
                if not composer.value:  # don't clobber a draft typed meanwhile
                    composer.value = text
            return
        self._clear_compose_state_after_send(peer, text)
        self._remember_sent(peer, msg.id)  # suppress the echo from listen_outgoing()
        await self._touch_dialog_for_message(peer, msg, incoming=False)
        if peer == self._current:  # user may have switched dialogs mid-send
            pane = self.query_one("#messages", Vertical)
            bubble = MessageBubble(_message_label(msg), out=True)
            if msg.translated_text:
                bubble.show_translation(msg.translated_text)
            self._bubble_index[msg.id] = bubble
            await pane.mount(bubble)
            self._scroll_messages_to_end(pane)

    async def _send_media(
        self, peer: int, path: str, caption: str | None, source_text: str | None = None,
    ) -> None:
        # source_text is the original "@file ... caption" command, cleared optimistically
        # in on_input_submitted — restore it on any failure so a typed command is not lost.
        def _restore() -> None:
            if source_text is None:
                return
            self._compose_state_for(peer).draft = source_text
            if peer == self._current:
                composer = self.query_one("#composer", Input)
                if not composer.value:  # don't clobber a draft typed meanwhile
                    composer.value = source_text

        try:
            msg = await self._client.send_media(peer, path, caption=caption)
        except SendForbiddenError:
            logger.warning("send media rejected (rights) (dialog %s)", peer)
            self.notify(READ_ONLY_MESSAGE, severity="warning")
            _restore()
            self._apply_composer_writable(peer)  # reflect the now-known read-only state
            return
        except Exception as exc:
            logger.exception("send media failed (dialog %s, %s)", peer, path)
            self.notify(f"Send failed: {exc}", severity="error")
            _restore()
            return
        self._remember_sent(peer, msg.id)  # suppress the echo from listen_outgoing()
        await self._touch_dialog_for_message(peer, msg, incoming=False)
        if peer == self._current:  # user may have switched dialogs mid-send
            pane = self.query_one("#messages", Vertical)
            bubble = MessageBubble(_message_label(msg), out=True)
            self._bubble_index[msg.id] = bubble
            await pane.mount(bubble)
            self._scroll_messages_to_end(pane)

    async def _send_reaction(
        self, peer: int, message_id: int, emoticon: str, source_text: str | None = None,
    ) -> None:
        # source_text is the original "/react ..." command, cleared optimistically in
        # on_input_submitted — restore it on any failure so the typed command is not lost.
        def _restore() -> None:
            if source_text is None:
                return
            self._compose_state_for(peer).draft = source_text
            if peer == self._current:
                composer = self.query_one("#composer", Input)
                if not composer.value:  # don't clobber a draft typed meanwhile
                    composer.value = source_text

        try:
            await self._client.send_reaction(peer, message_id, emoticon)
        except SendForbiddenError:
            logger.warning("reaction rejected (rights) (dialog %s, message %s)", peer, message_id)
            self.notify(READ_ONLY_MESSAGE, severity="warning")
            _restore()
            return
        except Exception as exc:
            logger.exception("send reaction failed (dialog %s, message %s)", peer, message_id)
            self.notify(f"Reaction failed: {exc}", severity="error")
            _restore()
            return
        self._remember_sent_reaction(peer, message_id, emoticon)
        if peer == self._current:
            pane = self.query_one("#messages", Vertical)
            await pane.mount(MessageBubble(f"reaction [{message_id}]: {emoticon}", out=True))
            self._scroll_messages_to_end(pane)

    async def _drain_incoming(self) -> None:
        try:
            async for ev in self._client.listen_all():  # groups too, not just DMs
                await self._touch_dialog_for_message(ev.dialog_id, ev.message, incoming=True)
                if ev.dialog_id == self._current:
                    pane = self.query_one("#messages", Vertical)
                    bubble = MessageBubble(_message_label(ev.message), out=False)
                    self._bubble_index[ev.message.id] = bubble
                    await pane.mount(bubble)
                    if ev.message.translated_text:
                        bubble.show_translation(ev.message.translated_text)
                    elif self._translator is not None:
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
                    bubble = MessageBubble(_message_label(ev.message), out=True)
                    self._bubble_index[ev.message.id] = bubble
                    await pane.mount(bubble)
                    if ev.message.translated_text:
                        bubble.show_translation(ev.message.translated_text)
                    elif self._translator is not None:
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
                    continue  # our own optimistic bubble already shows it
                if ev.dialog_id == self._current:
                    pane = self.query_one("#messages", Vertical)
                    await pane.mount(MessageBubble(_reaction_label(ev), out=False))
                    self._scroll_messages_to_end(pane)
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

    def _dialog_can_send(self, dialog_id: int) -> bool:
        # default True for an unknown dialog (fail-safe writable, matches core)
        return next(
            (d.can_send for d in self._all_dialogs if d.id == dialog_id), True
        )

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
        """Tab — move a pending suggestion into the composer (else fall through)."""
        if not self._pending_suggestion:
            # nothing to accept: hand Tab back to normal focus traversal
            self.screen.focus_next()
            return
        composer = self.query_one("#composer", Input)
        composer.value = self._pending_suggestion
        self._clear_suggestion()
        composer.focus()

    def _clear_suggestion(self) -> None:
        self._pending_suggestion = None
        self.query_one("#suggestion", Static).update("")
