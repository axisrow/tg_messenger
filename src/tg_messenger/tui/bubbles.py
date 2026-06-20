"""Message bubble widget and bubble-pane navigation helpers for the TUI.

``MessageBubble`` composes three independent pieces of state — author line, body, translation,
and reactions — via ``_build()`` so neither clobbers the other regardless of arrival order
(#106). Bubble navigation (``_navigable_bubbles``/``_focus_first_bubble_or_composer``) only ever
lands on bubbles belonging to the now-current dialog (#124). Re-exported from
``tg_messenger.tui.app`` for backward-compatible imports.
"""

from __future__ import annotations

import logging

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Input, ListView, Static

from tg_messenger.tui.format import _split_id_prefix, _terminal_safe_display_text

logger = logging.getLogger(__name__)

# #93: the per-message reaction presets — the same four as the web palette (chat.html).
REACTION_PRESETS = ["👍", "❤️", "🔥", "😂"]


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
        # #128: the app's dialog-switch generation when this bubble was mounted (set by
        # _message_bubble_for). Stays None for bubbles built outside that path (e.g. tests
        # that mount a MessageBubble directly); action_react then can't classify it as stale.
        self.switch_gen: int | None = None
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
            # the translation gets the theme accent so it visibly separates from the white original
            # (author/id use "dim"); without a style it was the same white and merged in.
            t.append(
                f"↳ {_terminal_safe_display_text(self._translation)}",
                style=self._translation_style(),
            )
        if self._reactions:
            t.append("\n")
            t.append(_terminal_safe_display_text(" ".join(self._reactions)))
        return t

    def _translation_style(self) -> str:
        """Rich style for the translation line — the theme accent, resolved best-effort.

        Text.append(style=) takes a Rich style (a colour name/hex), not a Textual CSS var, so we
        resolve $accent to its hex via app.theme_variables. Before mount self.app raises
        NoActiveAppError; fall back to a named colour so the translation is NEVER plain white
        (a translation always re-renders after mount, so the real accent applies in practice).
        """
        try:
            return self.app.theme_variables.get("accent") or "cyan"
        except Exception:
            # expected before mount (NoActiveAppError) — debug-logged, not silently swallowed
            logger.debug("translation accent unavailable (bubble not mounted yet); using fallback")
            return "cyan"

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
        # #128: reject a reaction on a STALE bubble — one mounted under an older dialog-switch
        # generation than the app's current, i.e. a previous-dialog bubble still lingering in the
        # DOM during the async history render after a switch (reachable only via a mouse click;
        # the keyboard path is already gated by _navigable_bubbles). A deliberate cross-dialog
        # target (#102/#105) carries the CURRENT generation, so it stays allowed.
        app_gen = getattr(self.app, "_switch_gen", None)
        if self.switch_gen is not None and app_gen is not None and self.switch_gen < app_gen:
            logger.debug(
                "ignoring reaction on stale bubble (dialog %s, gen %s < %s)",
                self.dialog_id, self.switch_gen, app_gen,
            )
            return
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
