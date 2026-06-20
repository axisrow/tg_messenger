"""Textual TUI: dialog list (left, Telegram-style tabs) + message view + composer.

Reuses the bubble/list pattern. A background worker drains ``client.listen_all()``
and appends incoming bubbles for the selected dialog (any kind, groups included).

This module is the facade: ``MessengerTUI`` (the App) lives here, while the widgets, message
bubbles, modal screens, settings cards, and the pure parsing/formatting helpers live in sibling
modules (``widgets``/``bubbles``/``screens``/``settings``/``parsing``/``format``). They are all
re-exported below so ``from tg_messenger.tui.app import X`` keeps working for every X that moved.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListView, LoadingIndicator, Static, Tab, Tabs

from tg_messenger.agent.outbound_coordinator import OutboundError, OutboundSendCoordinator
from tg_messenger.core.auth import LoginSession, session_store_from_env
from tg_messenger.core.client import READ_ONLY_MESSAGE, SendForbiddenError
from tg_messenger.core.search import can_send_in

# --- facade re-exports (these names moved to sibling modules; tests + back-compat import them here) ---
from tg_messenger.tui.bubbles import (  # noqa: F401
    REACTION_PRESETS,
    BubbleRow,
    MessageBubble,
    _focus_first_bubble_or_composer,
    _navigable_bubbles,
    _wrap_bubble,
)
from tg_messenger.tui.format import (  # noqa: F401
    _message_author_line,
    _message_body,
    _split_id_prefix,
    _terminal_safe_display_text,
)
from tg_messenger.tui.parsing import (  # noqa: F401
    _strip_first_token,
    parse_lang_command,
    parse_media_command,
    parse_tlang_command,
)
from tg_messenger.tui.screens import (  # noqa: F401
    HELP_TEXT,
    ORIGINAL_SENTINEL,
    ConfirmScreen,
    EmojiPickerScreen,
    HelpScreen,
    LoginScreen,
    ProfileItem,
    ProfileScreen,
    ReadLangScreen,
    VariantItem,
    VariantPickScreen,
)
from tg_messenger.tui.settings import (  # noqa: F401
    DEFAULT_SUGGEST_HISTORY,
    AccountItem,
    AccountsScreen,
    ProfileListCard,
    SuggestSettingsCard,
    TranslateSettingsCard,
    _make_real_client,
)
from tg_messenger.tui.widgets import (  # noqa: F401
    ComposerInput,
    DialogItem,
    DialogListView,
    SearchInput,
    SidebarTabs,
)

logger = logging.getLogger(__name__)

# shown before a draft in the suggestion strip; Tab accepts it into the composer
SUGGEST_PREFIX = "💡 Tab: "
# #158: shown in the suggestion strip while an explicit Ctrl+G LLM call is in flight (~seconds),
# so the user sees the suggester is working instead of "nothing happening"
SUGGEST_THINKING = "⏳ Суфлёр думает…"


@dataclass
class ComposeState:
    draft: str = ""
    source_text: str | None = None
    original_confirm_text: str | None = None
    ignore_next_empty_change: bool = False
    # #73: the coordinator token for the picked variant; consumed on the send that follows
    outbound_token: str | None = None


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
        # #124/#156: Escape clears the FOCUSED input — the composer when it's focused (#156, so
        # Ctrl+G gets a clean field), else a non-empty search filter (the original global). NOT
        # priority, so any open modal's own Escape still wins (the modal chain truncates above the app).
        Binding("escape", "clear_search", "Clear search", show=False),
        # #126: inbound translation. `t` toggles auto-translate; it is PRINTABLE so — like `?` — it
        # is filtered out while an Input (composer/search) is focused and works everywhere else
        # (tabs/dialogs/a bubble). Making it priority would steal every literal "t" typed into a
        # message, so it stays non-priority. The on-demand whole-chat translate is Ctrl+T
        # (`translate_all`, bound above) — non-printable + priority, so it fires from inside the
        # composer or a read-only channel where the composer is disabled.
        Binding("t", "toggle_auto_translate", "Авто-перевод", show=True),
        # #155: suggest a reply for the OPEN DM on demand. The automatic 💡 hint only fires on a
        # NEW incoming message (_drain_incoming); opening a DM with existing history never triggers
        # it, so the suggester felt dead when reading an already-delivered message. Ctrl+G generates
        # a draft for the current dialog. Non-printable + priority so it fires from inside the
        # composer too (like Ctrl+T), without stealing a literal key typed into a message.
        Binding("ctrl+g", "suggest_reply", "Подсказать ответ", priority=True, show=True),
    ]

    CSS = """
    /* #110: max-width lets the fixed 32-wide sidebar yield space on narrow terminals so the
       chat pane (and composer) don't collapse off-screen; on terminals >=64 cols 50% >= 32,
       so the default 32 width is unchanged. */
    #sidebar { width: 32; max-width: 50%; border-right: solid $primary; }
    #chat { width: 1fr; min-width: 16; }
    /* #160: the dialog rows must paint their FULL row background. Rich computes Thai/Indic
       combining marks (Unicode category Mn) as 0-width while the terminal paints them wider, so a
       row whose background spans only the Rich-computed text width leaves a stray dark patch at the
       right edge (most visible as a gap in the blue highlight on the selected row). Pinning the row
       and its inner Static to the full list width makes Textual paint the whole row regardless of
       the per-glyph width disagreement — without touching the (load-bearing) combining marks. */
    #dialogs > DialogItem { width: 1fr; }
    #dialogs > DialogItem > Static { width: 1fr; }
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
    /* the whole-chat translate (Ctrl+T) status — centered container: accent label + animated dots.
       both children take the full container width and center their own content, so the label and
       the dots are vertically stacked AND horizontally aligned to the same centre line. */
    #messages .translate-status {
        width: 1fr;
        height: 1fr;
        align: center middle;
    }
    #messages .translate-status-label {
        width: 1fr;
        text-align: center;
        color: $accent;
        text-style: bold;
    }
    #messages .translate-status LoadingIndicator {
        width: 1fr;
        height: auto;
        content-align-horizontal: center;
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
    /* #170: bordered so a multi-line draft reads as ONE framed block, not N prefix-less rows
       (only the first line carries the 💡 Tab: prefix). Starts hidden (display: none) and
       _set_suggestion_strip flips it on only for non-empty text — else the empty border floats
       above the composer from launch until the first dialog open (caught in review). */
    #suggestion { color: $text-muted; height: auto; border: round $panel; padding: 0 1; display: none; }
    #composer { dock: bottom; }
    /* #116: shared modal card — a centered, bordered, width-capped box (was full-width, top-left,
       unframed). Centering (align: center middle) lives on each ModalScreen's DEFAULT_CSS so it is
       not affected by App.CSS-vs-screen scoping; these rules only shape the box. */
    #profile-box, #login-box, #variant-box, #emoji-box, #help-box, #readlang-box {
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
    #readlang-title { text-style: bold; }
    """

    def __init__(self, *, client=None, session_name: str = "default",
                 profiles: list[str] | None = None, client_factory=None, deps_factory=None,
                 suggester=None, login_session=None, store=None, translator=None,
                 outbound=None, session_store=None, account_client_factory=None,
                 auto_translate=False):
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
        # #128: a monotonic dialog-switch generation. Bumped on every _open_dialog; each
        # MessageBubble is stamped with the generation it was mounted under. action_react
        # rejects a reaction on a bubble whose generation is OLDER than the current one — i.e.
        # a stale previous-dialog bubble still lingering in the DOM during the async history
        # render. A deliberate cross-dialog target (#102/#105) carries the CURRENT generation
        # (its dialog != _current, but it belongs to the live view), so it is still allowed.
        self._switch_gen = 0
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
        # #126: inbound auto-translate toggle. Default OFF so a fresh session never spends LLM
        # tokens until the user opts in (key `t` or TG_TRANSLATE_AUTO=on). A per-profile KV
        # override (translate_auto) is loaded in _startup and wins over this seed value.
        self._auto_translate = bool(auto_translate)
        self._outbound = outbound
        # #73: one UI-agnostic coordinator owns the outbound flow (prepare/timeout/token/
        # send/source-recording). Rebuilt after a ProfileScreen pick swaps the deps.
        self._coordinator = self._build_coordinator()
        self._compose_states: dict[int, ComposeState] = {}
        self._bubble_index: dict[int, MessageBubble] = {}
        # #126: the messages currently mounted in the open dialog — the source for on-demand
        # #126: re-entrancy guard so a second t/Ctrl+T can't stack a second reading-language modal.
        self._lang_prompt_open = False
        self._pending_suggestion: str | None = None
        # #158: which dialog the pending draft belongs to — so an instant Ctrl+G never shows a
        # draft pre-generated for dialog A while dialog B is open.
        self._pending_suggestion_dialog: int | None = None
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
        # #160 r3: when a SINGLE bubble is appended to an already-tall pane (a send into a long
        # chat), `max_scroll_y` is still the pre-mount value at scroll_once() time, so the loop
        # converges to the OLD bottom and the new bubble lands just below the viewport — invisible
        # until a manual reopen (the reported bug). call_after_refresh fires once Textual has
        # recomputed the layout, so this final pass sees the real max_scroll_y and reaches it.
        self.call_after_refresh(scroll_once)
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
                        self._auto_translate = deps.auto_translate  # #126: per-profile env default
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
        # #126: a persisted toggle (set last session via `t`) overrides the env default. Loaded in
        # a worker — _startup must not await IO (it stalls the pump); harmless if it lands late.
        if self._translator is not None:
            self.run_worker(
                self._load_auto_translate_pref(), group="translate-pref", exclusive=False
            )
        # #144: the 💡 draft feature is off when no suggester is wired (no [agent] extra or
        # TG_AGENT_MODEL). Tell the user ONCE, with the actionable reason, instead of a silent
        # strip that never appears.
        if self._suggester is None:
            self._notify_suggester_disabled()

    def _notify_suggester_disabled(self) -> None:
        try:
            from tg_messenger.agent.suggest import suggester_disabled_reason

            reason = suggester_disabled_reason()
        except Exception:
            logger.exception("could not determine why the suggester is disabled")
            reason = None
        if reason:
            self.notify(f"Суфлёр (💡) выключен: {reason}", severity="warning", timeout=8)

    async def _load_auto_translate_pref(self) -> None:
        try:
            stored = await self._translator.auto_enabled()
        except Exception:
            logger.exception("loading auto-translate preference failed")
            return
        if stored is not None:
            self._auto_translate = stored

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
        # #128: a real dialog switch advances the generation. The previous dialog's bubbles stay
        # mounted until the async _show_history below runs remove_children; they now carry an
        # OLDER generation than _switch_gen, so a mouse-click + react on one (the render-window
        # gap) is rejected by MessageBubble.action_react — while a same-view cross-dialog target
        # (#102/#105) keeps the current generation and is still allowed.
        self._switch_gen += 1
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
        # #128: stamp the switch generation this bubble was built under. A later dialog switch
        # bumps _switch_gen, leaving this (now previous-view) bubble's generation older — which
        # action_react rejects, closing the stale-bubble mouse-react window.
        bubble.switch_gen = self._switch_gen
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
        if self._translator is not None and self._auto_translate:
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
        """Ctrl+T — translate the whole open chat (up to the configured cap), with a spinner.

        Routes through ``_ensure_lang_then_translate_all`` so it prompts for a reading language
        (ReadLangScreen) when none is set — works in read-only channels where the composer/`/tlang`
        is disabled — then runs the whole-chat pass regardless of the auto-translate toggle (#126).
        """
        if self._translator is None:
            self.notify("Переводчик не настроен", severity="warning")
            return
        if self._current is None:
            self.notify("Нет открытого диалога", severity="warning")
            return
        self.run_worker(
            self._ensure_lang_then_translate_all(self._current),
            group="translate-all",
            exclusive=True,
        )

    async def _translate_whole_dialog(self, dialog_id: int) -> None:
        """Reload up to N recent messages and translate them in one pass, showing a spinner.

        Long by design (a reasoning model over a whole chat) — the #messages pane shows its loading
        spinner for the duration. A failed/empty pass is surfaced via notify (the Ctrl+T path is
        explicit, unlike the silent background auto-translate on open).
        """
        # the worker is scheduled, not run inline: the user may have opened another dialog between
        # pressing Ctrl+T and this body running. Bail BEFORE touching the pane, else we'd wipe the
        # new dialog's history and mount this dialog's spinner into the wrong chat.
        if dialog_id != self._current:
            return
        pane = self.query_one("#messages", Vertical)
        # mount a labelled status WITH the animated LoadingIndicator (the same blinking dots the
        # built-in pane.loading shows) — pane.loading would overlay/hide a mounted label, so we
        # compose both ourselves: the label on top, the indicator below.
        await pane.remove_children()
        # remove_children() awaited (yielded the loop): the user may have switched dialogs in that
        # window. Re-check BEFORE clearing the shared bubble index / mounting this dialog's spinner,
        # else the stale spinner could land on top of the new dialog's history.
        if dialog_id != self._current:
            return
        self._bubble_index.clear()
        await pane.mount(
            Vertical(
                Label("Идёт перевод…", classes="translate-status-label"),
                LoadingIndicator(),
                id="translate-status",
                classes="translate-status",
            )
        )
        ok = False
        try:
            cap = await self._translator.max_messages()
            if self._store is not None:
                messages = await self._store.history(dialog_id, limit=cap)
            else:
                messages = await self._client.history(dialog_id, limit=cap)
            translated = await self._translator.translate_history(dialog_id, messages)
            if dialog_id == self._current:
                # drop the status line, then mount the TRANSLATED snapshot — translate_history
                # returns model-copies carrying translated_text; mounting `messages` (the originals)
                # would show the untranslated text and the whole pass would appear to do nothing.
                await pane.remove_children()
                self._bubble_index.clear()
                bubbles = [self._message_bubble_for(m, dialog_id) for m in translated]
                await pane.mount(*(_wrap_bubble(b) for b in bubbles))
                self._drain_pending_reactions(dialog_id)  # replay reactions buffered during the pass
                self._scroll_messages_to_end(pane)
            # success = the pass ran (no exception). A chat that's entirely in the target language
            # legitimately yields zero translations — that is NOT a failure.
            ok = True
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
            tlang_command = parse_tlang_command(text)
        except ValueError as exc:
            logger.warning("bad tlang command in dialog %s: %s", dialog_id, exc)
            self.notify(str(exc), severity="error")
            return
        if tlang_command is not None:
            self._optimistic_clear(dialog_id, event.input)
            self.run_worker(
                self._apply_tlang_command(tlang_command),
                group="reading-lang",
                exclusive=True,
            )
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
            # verify the bubble's own SOURCE dialog before attaching, mirroring the
            # _apply_reaction guard (#102/#128): _bubble_index is keyed by bare message_id,
            # which is not unique across dialogs, so a colliding stale bubble must never get
            # the replayed reaction.
            if bubble is not None and bubble.dialog_id == dialog_id:
                bubble.add_reaction(emoticon)

    async def _apply_tlang_command(self, command: tuple[str, str | None]) -> None:
        """#126: set/clear the global inbound reading language via Translator.set_target_lang."""
        if self._translator is None:
            self.notify("Перевод не настроен (нет TG_TRANSLATE_MODEL).", severity="warning")
            return
        action, value = command
        try:
            if action == "off":
                await self._translator.set_target_lang(None)
            elif action == "set" and value is not None:
                await self._translator.set_target_lang(value)
        except ValueError as exc:
            logger.warning("invalid reading language code: %s", exc)
            self.notify(str(exc), severity="error")
            return
        except Exception:
            logger.exception("reading language command failed")
            self.notify("Не удалось сохранить язык перевода.", severity="error")
            return
        self.notify(
            "Перевод входящих отключён." if action == "off" else "Язык перевода сохранён."
        )

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
        # #160 (+ regression fix): ARM echo-dedup BEFORE the await window. `_touch_dialog_for_message`
        # awaits `_render_dialogs` (several loop turns); the real account echoes our just-sent message
        # back on `listen_outgoing()` during that window. If the key isn't set yet, `_drain_outgoing`
        # (membership-only, no pop) draws its OWN bubble and we then draw a second — a DUPLICATE.
        self._remember_sent(peer, msg.id)  # suppress the listen_outgoing() echo
        await self._touch_dialog_for_message(peer, msg, incoming=False)
        if peer == self._current:  # user may have switched dialogs mid-send
            pane = self.query_one("#messages", Vertical)
            bubble = self._message_bubble_for(msg, peer)
            await pane.mount(_wrap_bubble(bubble))
            self._scroll_messages_to_end(pane)
        else:
            # #160: navigated away mid-send → no optimistic bubble drawn. Un-arm the dedup so the
            # live echo is rendered by the `_drain_outgoing` fallback on return, instead of the
            # message staying invisible until a manual reopen.
            self._sent_ids.pop((peer, msg.id), None)

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
        # #160 (+ regression fix): arm echo-dedup BEFORE the await window — see `_send_text`.
        self._remember_sent(peer, msg.id)  # suppress the listen_outgoing() echo
        await self._touch_dialog_for_message(peer, msg, incoming=False)
        if peer == self._current:  # user may have switched dialogs mid-send
            pane = self.query_one("#messages", Vertical)
            bubble = self._message_bubble_for(msg, peer)
            await pane.mount(_wrap_bubble(bubble))
            self._scroll_messages_to_end(pane)
        else:
            # #160: navigated away → no bubble; un-arm so `_drain_outgoing` renders the live echo.
            self._sent_ids.pop((peer, msg.id), None)

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
                    if (
                        not ev.message.translated_text
                        and self._translator is not None
                        and self._auto_translate
                    ):
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
                    if (
                        not ev.message.translated_text
                        and self._translator is not None
                        and self._auto_translate
                    ):
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

    def action_suggest_reply(self) -> None:
        """Ctrl+G — suggest a reply for the OPEN dialog on demand (#155).

        The automatic 💡 hint only fires on a new incoming message; this lets the user
        ask for a draft while reading already-delivered history. Each rejection NOTIFIES
        (the user pressed a key and expects feedback) instead of silently no-opping.

        A non-empty composer blocks it: on_input_changed wipes the draft hint the moment
        the field is touched, and silently clobbering typed text is worse. The toast points
        at Escape, which clears the composer (action_clear_search) so Ctrl+G runs clean.
        """
        if self._suggester is None:
            self.notify("Суфлёр не настроен", severity="warning")
            return
        if self._current is None:
            self.notify("Сначала откройте диалог", severity="warning")
            return
        # _kind_for_rendering prefers the kind captured at open time (_current_kind), so the DM
        # check stays correct even after a tab switch (e.g. to Archive) drops the open dialog from
        # _all_dialogs — without it Ctrl+G would wrongly reject a DM as "not a DM" there.
        if self._kind_for_rendering(self._current) != "dm":
            self.notify("Суфлёр работает только в личных сообщениях", severity="warning")
            return
        if self.query_one("#composer", Input).value:
            self.notify("Очистите поле ввода (Esc) — черновик не перезаписывается", severity="warning")
            return
        # #158: a draft pre-generated for THIS dialog (e.g. by the auto-path on an incoming message)
        # is shown INSTANTLY — no second LLM call, no ⏳ wait. _pending_suggestion is left intact so
        # Tab still accepts it. Otherwise fall through to a fresh call WITH the thinking indicator.
        if self._pending_suggestion and self._pending_suggestion_dialog == self._current:
            self._set_suggestion_strip(f"{SUGGEST_PREFIX}{self._pending_suggestion}")
            return
        self.run_worker(
            self._suggest(self._current, notify_empty=True, show_thinking=True),
            group="suggest",
            exclusive=True,
        )

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

    async def _suggest(
        self, dialog_id: int, *, notify_empty: bool = False, show_thinking: bool = False
    ) -> None:
        # #158: show "⏳ Суфлёр думает…" BEFORE the (multi-second) LLM await, so an explicit Ctrl+G
        # isn't a silent ~20s wait. Synchronous on purpose — the only way it's visible during the
        # real call, and the only way a stub-based test can observe it. Auto-path passes
        # show_thinking=False so it never flickers on every incoming message.
        thinking_shown = (
            show_thinking
            and dialog_id == self._current
            and not self.query_one("#composer", Input).value
        )
        if thinking_shown:
            self._set_suggestion_strip(SUGGEST_THINKING)
        try:
            draft = await self._suggester.suggest(dialog_id)
        except Exception:
            logger.exception("suggest failed (dialog %s)", dialog_id)
            if thinking_shown and dialog_id == self._current:
                self._set_suggestion_strip("")  # don't leave ⏳ hanging
            return
        # the user may have switched dialogs or started typing while we waited
        if dialog_id != self._current or self.query_one("#composer", Input).value:
            return
        # Ctrl+G is an explicit request, so an empty draft (suggester off / nothing to add) gets a
        # toast — silence would read as "the key did nothing". The auto-path passes notify_empty=False.
        if not draft and notify_empty:
            if thinking_shown:
                self._set_suggestion_strip("")  # clear ⏳ before the toast
            self.notify("Суфлёр не предложил ответ", severity="warning")
            return
        self._pending_suggestion = draft
        self._pending_suggestion_dialog = dialog_id if draft else None
        self._set_suggestion_strip(f"{SUGGEST_PREFIX}{draft}" if draft else "")

    def action_accept_suggestion(self) -> None:
        """Tab: accept a pending suggestion into the composer, else fall through to forward
        focus cycling (#114 — the unified scheme; Shift+Tab cycles backward)."""
        if not self._pending_suggestion:
            # nothing to accept: hand Tab back to normal forward focus traversal
            self.screen.focus_next()
            return
        composer = self.query_one("#composer", Input)
        composer.value = self._pending_suggestion
        if self._current is not None:
            # #160: the programmatic value set above doesn't reach on_input_changed (the
            # stale-value guard early-returns), so persist the accepted draft into the per-dialog
            # state ourselves — otherwise switching dialogs before Enter silently drops it.
            self._compose_state_for(self._current).draft = self._pending_suggestion
        self._clear_suggestion()
        composer.focus()

    def _set_suggestion_strip(self, text: str) -> None:
        """Update the #suggestion strip and toggle its visibility (#170).

        One place owns the show/hide: a bordered strip (App.CSS) must be hidden when empty, else its
        empty border floats above the composer. Non-empty text → shown; "" → hidden.
        """
        strip = self.query_one("#suggestion", Static)
        strip.update(text)
        strip.display = bool(text)

    def _clear_suggestion(self) -> None:
        self._pending_suggestion = None
        self._pending_suggestion_dialog = None  # #158: keep the dialog scope in sync with the draft
        self._set_suggestion_strip("")

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
        """Escape — clear the FOCUSED input (#155/#156).

        Context-aware so it never destroys an unrelated draft: when the composer is focused,
        Escape clears the composer (so Ctrl+G has a clean field) and the per-dialog draft goes
        with it; otherwise it clears a non-empty search filter (the long-standing global). This
        avoids the data-loss trap where Escape-to-clear-search would also wipe a reply typed in
        the composer (the draft is persisted via on_input_changed → state.draft, unrecoverable on
        a dialog switch). A no-op when the relevant field is already empty.
        """
        composer = self.query_one("#composer", Input)
        if composer.has_focus:
            if composer.value:
                composer.value = ""  # explicit: clear the draft so a fresh Ctrl+G / reply is clean
            return
        search = self.query_one("#search", Input)
        if search.value:
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
                suggester=self._suggester,
            )
        )
        if result is not None:
            self._translator = result

    def action_toggle_auto_translate(self) -> None:
        """`t` — flip inbound auto-translate on/off (#126).

        Persists the choice per-profile (loaded next launch) and, when turning ON with a chat
        open, immediately translates it (prompting for a reading language first if none is set).
        """
        if self._translator is None:
            self.notify("Переводчик не настроен", severity="warning")
            return
        self._auto_translate = not self._auto_translate
        self.notify(f"Авто-перевод {'включён' if self._auto_translate else 'выключен'}")
        self.run_worker(
            self._persist_auto_translate(self._auto_translate),
            group="translate-pref",
            exclusive=False,
        )
        # turning ON with a chat open: kick the same on-demand whole-chat translate Ctrl+T runs,
        # so the open chat is translated immediately instead of waiting for the next message.
        if self._auto_translate and self._current is not None:
            self.run_worker(
                self._ensure_lang_then_translate_all(self._current),
                group="translate-all",
                exclusive=True,
            )

    async def _persist_auto_translate(self, enabled: bool) -> None:
        try:
            await self._translator.set_auto_enabled(enabled)
        except Exception:
            logger.exception("persisting auto-translate preference failed")

    async def _ensure_lang_then_translate_all(self, dialog_id: int) -> None:
        """Ensure a reading language is set (prompt via ReadLangScreen if not), then run the
        whole-chat translate pass (#126/#136).

        The on-demand Ctrl+T path and the `t`-turns-ON path both route here: ``_translate_whole_dialog``
        translates into ``target_lang`` and silently yields the originals when no reading language is
        set, so we prompt for one first (works in read-only channels where the composer is disabled).
        """
        try:
            target = await self._translator.target_lang()
        except Exception:
            logger.exception("reading target language failed")
            return
        if not target:
            if self._lang_prompt_open:
                return  # another t/Ctrl+T already has the modal open
            self._lang_prompt_open = True
            try:
                code = await self.push_screen_wait(ReadLangScreen())
            finally:
                self._lang_prompt_open = False
            if not code:
                self.notify("Язык перевода не задан.", severity="warning")
                return
            try:
                await self._translator.set_target_lang(code)
            except ValueError as exc:
                self.notify(str(exc), severity="error")
                return
            except Exception:
                logger.exception("saving reading language failed")
                self.notify("Не удалось сохранить язык перевода.", severity="error")
                return
        await self._translate_whole_dialog(dialog_id)
