"""Sidebar + composer widgets for the TUI: tabs, the dialog list, the search/composer inputs,
and the dialog row. These wire up the arrow-key focus chain (search ↔ tabs ↔ dialogs ↔ messages
↔ composer, #124); the bubble-pane helpers they hand off to live in ``bubbles``. Re-exported from
``tg_messenger.tui.app`` for backward-compatible imports.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Input, ListItem, ListView, Static, Tabs

from tg_messenger.tui.bubbles import (
    _focus_first_bubble_or_composer,
    _navigable_bubbles,
)
from tg_messenger.tui.format import _terminal_safe_display_text


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

