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
from textual.widgets import Input, ListItem, ListView, Static, Tab, Tabs

from tg_messenger.core.auth import LOGIN_HINT

logger = logging.getLogger(__name__)


def _make_real_client(session_name: str):
    from tg_messenger.core.client import StandaloneTelegramClient

    return StandaloneTelegramClient(
        api_id=int(os.environ.get("TG_API_ID", "0")),
        api_hash=os.environ.get("TG_API_HASH", ""),
        session_name=session_name,
    )


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

    def __init__(self, *, client=None, session_name: str = "default"):
        super().__init__()
        self._client = client
        self._session_name = session_name
        self._current: int | None = None
        self._tab = "dm"
        self._started = False  # gates tab events until _startup finished

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Tabs(Tab("DM", id="dm"), Tab("Группы", id="groups"))
                yield ListView(id="dialogs")
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
            self._client = _make_real_client(self._session_name)
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
        lv = self.query_one("#dialogs", ListView)
        if self._tab == "groups":
            items = [d for d in await self._client.dialogs(dm_only=False) if d.kind != "dm"]
        else:
            items = await self._client.dialogs(dm_only=True)
        for d in items:
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

    async def on_input_submitted(self, event: Input.Submitted) -> None:
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
