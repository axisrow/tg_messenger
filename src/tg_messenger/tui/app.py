"""Textual TUI: dialog list (left) + message view + composer (right).

Reuses the bubble/list pattern. A background worker drains ``client.listen()``
and appends incoming bubbles for the selected dialog.
"""

from __future__ import annotations

import os

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, ListItem, ListView, Static


def _make_real_client(session_name: str):
    from tg_messenger.core.client import StandaloneTelegramClient

    return StandaloneTelegramClient(
        api_id=int(os.environ.get("TG_API_ID", "0")),
        api_hash=os.environ.get("TG_API_HASH", ""),
        session_name=session_name,
    )


class DialogItem(ListItem):
    def __init__(self, dialog_id: int, title: str):
        super().__init__(Static(f"{dialog_id} — {title}"))
        self.dialog_id = dialog_id


class MessageBubble(Static):
    def __init__(self, text: str, out: bool):
        super().__init__(text, classes="out" if out else "in")


class MessengerTUI(App):
    CSS = """
    #dialogs { width: 32; border-right: solid $primary; }
    .out { color: $accent; text-align: right; }
    .in { color: $text; }
    #composer { dock: bottom; }
    """

    def __init__(self, *, client=None, session_name: str = "default"):
        super().__init__()
        self._client = client
        self._session_name = session_name
        self._current: int | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield ListView(id="dialogs")
            with Vertical():
                yield Vertical(id="messages")
                yield Input(placeholder="Message…", id="composer")

    async def on_mount(self) -> None:
        if self._client is None:
            self._client = _make_real_client(self._session_name)
        await self._client.connect()
        await self._load_dialogs()
        self.run_worker(self._drain_incoming(), exclusive=False)

    async def _load_dialogs(self) -> None:
        lv = self.query_one("#dialogs", ListView)
        for d in await self._client.dialogs(dm_only=True):
            await lv.append(DialogItem(d.id, d.title))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, DialogItem):
            self._current = item.dialog_id
            await self._show_history(item.dialog_id)

    async def _show_history(self, dialog_id: int) -> None:
        pane = self.query_one("#messages", Vertical)
        await pane.remove_children()
        for m in await self._client.history(dialog_id, limit=50):
            await pane.mount(MessageBubble(m.text or "<media>", m.out))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._current is None or not event.value.strip():
            return
        await self._client.send_text(self._current, event.value)
        pane = self.query_one("#messages", Vertical)
        await pane.mount(MessageBubble(event.value, out=True))
        event.input.value = ""

    async def _drain_incoming(self) -> None:
        async for ev in self._client.listen():
            if ev.dialog_id == self._current:
                pane = self.query_one("#messages", Vertical)
                await pane.mount(MessageBubble(ev.message.text or "<media>", out=False))
