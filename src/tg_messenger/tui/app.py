"""Textual TUI: dialog list (left, DM/groups tabs) + message view + composer (right).

Reuses the bubble/list pattern. A background worker drains ``client.listen_all()``
and appends incoming bubbles for the selected dialog (any kind, groups included).
"""

from __future__ import annotations

import asyncio
import logging
import os

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static, Tab, Tabs

from tg_messenger.core.auth import LOGIN_HINT

logger = logging.getLogger(__name__)


def _make_real_client(session_name: str):
    from tg_messenger.core.client import StandaloneTelegramClient

    return StandaloneTelegramClient(
        api_id=int(os.environ.get("TG_API_ID", "0")),
        api_hash=os.environ.get("TG_API_HASH", ""),
        session_name=session_name,
    )


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


class SidebarTabs(Tabs):
    """DM/groups tabs that hand focus down to the sibling dialog list.

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
    def __init__(self, dialog_id: int, title: str):
        # markup=False: titles/messages are untrusted text, [brackets] must render literally
        super().__init__(Static(f"{dialog_id} — {title}", markup=False))
        self.dialog_id = dialog_id


class MessageBubble(Static):
    def __init__(self, text: str, out: bool):
        super().__init__(text, classes="out" if out else "in", markup=False)


class MessengerTUI(App):
    # priority: quitting must work even while focus sits in the composer Input
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", priority=True)]

    CSS = """
    #sidebar { width: 32; border-right: solid $primary; }
    .out { color: $accent; text-align: right; }
    .in { color: $text; }
    #composer { dock: bottom; }
    """

    def __init__(self, *, client=None, session_name: str = "default",
                 profiles: list[str] | None = None, client_factory=None):
        super().__init__()
        self._client = client
        self._session_name = session_name
        self._profiles = profiles or []
        # how a client is built once a profile is chosen (injectable for tests)
        self._client_factory = client_factory or _make_real_client
        self._current: int | None = None
        self._tab = "dm"
        self._started = False  # gates tab events until _startup finished
        self._all_dialogs: list = []  # full loaded list; search filters it locally

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Input(placeholder="Поиск…", id="search")
                yield SidebarTabs(Tab("DM", id="dm"), Tab("Группы", id="groups"), id="tabs")
                yield DialogListView(id="dialogs")
            with Vertical():
                yield Vertical(id="messages")
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
        if self._client is None:
            if len(self._profiles) > 1:
                # >1 account and none preselected → ask which one, then build it
                chosen = await self.push_screen_wait(ProfileScreen(self._profiles))
                self._session_name = chosen
                self._client = self._client_factory(chosen)
            elif len(self._profiles) == 1:
                self._session_name = self._profiles[0]
                self._client = self._client_factory(self._profiles[0])
            else:
                self._client = self._client_factory(self._session_name)
        try:
            await self._client.connect()
            if not await self._client.is_authorized():
                self.exit(return_code=1, message=LOGIN_HINT)
                return
            await self._load_dialogs()
        except Exception as exc:
            logger.exception("TUI startup failed")
            self.exit(return_code=1, message=f"Startup failed: {exc}")
            return
        self.query_one("#dialogs", ListView).loading = False
        self._started = True
        self.run_worker(self._drain_incoming(), exclusive=False)

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    async def _load_dialogs(self) -> None:
        if self._tab == "groups":
            items = await self._client.group_dialogs()
        else:
            items = await self._client.dialogs()
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
        await lv.clear()
        for d in filter_dialogs(self._all_dialogs, query):
            await lv.append(DialogItem(d.id, d.title))

    async def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        # Tabs fires this once at mount, before the client exists — _started gates it.
        # (NOT named _ready: Textual's App already has a _ready coroutine.)
        # Network goes through a worker: awaiting here would stall the message pump.
        self._tab = event.tab.id or "dm"
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
            self._current = item.dialog_id
            # exclusive group: selecting another dialog cancels a still-loading history
            self.run_worker(self._show_history(item.dialog_id), group="history", exclusive=True)

    async def _show_history(self, dialog_id: int) -> None:
        pane = self.query_one("#messages", Vertical)
        await pane.remove_children()
        pane.loading = True
        try:
            messages = await self._client.history(dialog_id, limit=50)
        except Exception as exc:
            pane.loading = False
            logger.exception("history load failed (dialog %s)", dialog_id)
            self.notify(f"History failed: {exc}", severity="error")
            return
        pane.loading = False
        await pane.mount(*(MessageBubble(m.text or "<media>", m.out) for m in messages))

    async def on_input_changed(self, event: Input.Changed) -> None:
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
        event.input.value = ""  # clear optimistically; restored on failure
        self.run_worker(self._send_text(self._current, text), exclusive=False)

    async def _send_text(self, peer: int, text: str) -> None:
        try:
            await self._client.send_text(peer, text)
        except Exception as exc:
            logger.exception("send failed (dialog %s)", peer)
            self.notify(f"Send failed: {exc}", severity="error")
            composer = self.query_one("#composer", Input)
            if not composer.value:  # don't clobber a draft typed meanwhile
                composer.value = text
            return
        if peer == self._current:  # user may have switched dialogs mid-send
            pane = self.query_one("#messages", Vertical)
            await pane.mount(MessageBubble(text, out=True))

    async def _drain_incoming(self) -> None:
        try:
            async for ev in self._client.listen_all():  # groups too, not just DMs
                if ev.dialog_id == self._current:
                    pane = self.query_one("#messages", Vertical)
                    await pane.mount(MessageBubble(ev.message.text or "<media>", out=False))
        except Exception:
            logger.exception("incoming listener failed")
            self.notify("Incoming listener failed — see log.", severity="error")
