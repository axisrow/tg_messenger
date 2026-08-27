"""Mechanical contracts for promised TUI interactions (#223, #235).

The population is derived from mounted widgets and runtime classes. Registries exist only where
constructors or contextual promises need a human decision; adding a modal, field, or printable
screen binding without updating its contract fails with one collected list of violations.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from pathlib import Path

from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListView, Static

from tests.test_tui import (
    FakeSessionStore,
    FakeTuiLoginSession,
    SavingStubClient,
    StubSuggesterTUI,
    StubTranslator,
    TuiStubClient,
    _pause_until,
)
from tg_messenger.tui import app as app_module
from tg_messenger.tui import bubbles, screens, settings, widgets
from tg_messenger.tui.app import (
    EMPTY_MESSAGES_HINT,
    OUTBOUND_TRANSLATING,
    AccountsScreen,
    MessengerTUI,
)
from tg_messenger.tui.screens import (
    HELP_TEXT,
    ConfirmScreen,
    DismissableModal,
    EmojiPickerScreen,
    HelpScreen,
    LoginScreen,
    ProfileScreen,
    ReadLangScreen,
    VariantPickScreen,
)

_SRC = Path(__file__).resolve().parent.parent / "src" / "tg_messenger"
_TUI = _SRC / "tui"
_APP_PY = _TUI / "app.py"


def _accounts_screen_with_everything() -> tuple[
    AccountsScreen, FakeSessionStore, StubTranslator, StubSuggesterTUI
]:
    store = FakeSessionStore(["alice", "bob"])
    translator = StubTranslator({"mode": "off", "target": "", "known": [], "unknown": []})
    suggester = StubSuggesterTUI()
    screen = AccountsScreen(
        profiles=store.list_profiles(),
        active="alice",
        store=store,
        account_client_factory=lambda name: SavingStubClient(name, store),
        login_session=FakeTuiLoginSession(),
        translator=translator,
        suggester=suggester,
    )
    return screen, store, translator, suggester


# Constructors differ, so this registry is deliberately explicit. The class population below is
# computed from ModalScreen itself; a ninth modal without a factory fails before it can be skipped.
_MODAL_FACTORIES = {
    ProfileScreen: lambda: ProfileScreen(["alice", "bob"]),
    LoginScreen: lambda: LoginScreen(FakeTuiLoginSession()),
    VariantPickScreen: lambda: VariantPickScreen(["hola"], "hello"),
    EmojiPickerScreen: EmojiPickerScreen,
    HelpScreen: HelpScreen,
    ConfirmScreen: lambda: ConfirmScreen("Continue?"),
    ReadLangScreen: ReadLangScreen,
    AccountsScreen: lambda: _accounts_screen_with_everything()[0],
}


def _all_subclasses(cls: type) -> set[type]:
    found: set[type] = set()
    pending = list(cls.__subclasses__())
    while pending:
        child = pending.pop()
        if child in found:
            continue
        found.add(child)
        pending.extend(child.__subclasses__())
    return found


def _tui_modal_classes() -> set[type[ModalScreen]]:
    return {
        cls
        for cls in _all_subclasses(ModalScreen)
        if cls is not DismissableModal and cls.__module__.startswith("tg_messenger.tui.")
    }


def test_every_tui_modal_has_a_factory():
    actual = _tui_modal_classes()
    assert actual == set(_MODAL_FACTORIES), (
        "Every ModalScreen needs a contract factory. "
        f"missing={sorted(c.__name__ for c in actual - set(_MODAL_FACTORIES))}, "
        f"stale={sorted(c.__name__ for c in set(_MODAL_FACTORIES) - actual)}"
    )


async def test_every_tui_modal_closes_on_escape():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        trapped = []
        for modal_cls, factory in _MODAL_FACTORIES.items():
            modal = factory()
            app.push_screen(modal)
            await _pause_until(pilot, lambda: modal.is_mounted)
            await pilot.press("escape")
            await pilot.pause()
            if app.screen is modal:
                trapped.append(modal_cls.__name__)
                app.pop_screen()
                await pilot.pause()
        assert not trapped, (
            f"modals trapped keyboard users after Escape: {trapped}. "
            "Inherit DismissableModal or deliberately override action_cancel."
        )


# --- Help-text contract ---------------------------------------------------------------


def _binding_keys(cls: type) -> set[str]:
    """Binding keys declared anywhere in a class' MRO, split into Textual aliases."""
    keys = set()
    for base in cls.__mro__:
        for binding in base.__dict__.get("BINDINGS", []):
            if isinstance(binding, Binding):
                keys.update(part.strip().lower() for part in binding.key.split(","))
            elif isinstance(binding, tuple):
                keys.update(part.strip().lower() for part in binding[0].split(","))
    return keys


def _tui_widget_classes() -> set[type]:
    classes = set()
    for module in (app_module, bubbles, screens, settings, widgets):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__:
                classes.add(cls)
    return classes


def _help_label_texts() -> set[str]:
    """Literal Label text whose id marks it as contextual help."""
    texts = set()
    for path in _TUI.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "Label" or not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            label_id = next(
                (
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "id" and isinstance(kw.value, ast.Constant)
                ),
                "",
            )
            if isinstance(label_id, str) and label_id.endswith("-help"):
                texts.add(str(node.args[0].value))
    return texts


_KEY_TOKEN_RE = re.compile(
    r"(?<!\w)(Ctrl\+[A-Za-z]|Shift\+Tab|F1|Esc|Enter|Delete|Tab|Space|[?←→↑↓]|[drtxyn])(?!\w)",
    re.IGNORECASE,
)
_KEY_NAMES = {
    "↑": "up",
    "↓": "down",
    "←": "left",
    "→": "right",
    "esc": "escape",
    "?": "question_mark",
}


def _promised_keys() -> set[str]:
    tokens = set(_KEY_TOKEN_RE.findall(HELP_TEXT))
    for label in _help_label_texts():
        tokens.update(_KEY_TOKEN_RE.findall(label))
    return {_KEY_NAMES.get(token.lower(), token.lower()) for token in tokens}


# These promises are real but contextual rather than globally available. Keeping the exact phrase
# here makes a wording change force another human decision instead of silently broadening a claim.
_CONDITIONAL_PROMISES = {
    "→ from list": "Right opens only from the conversation list.",
    "← on empty pane": "Left leaves from a message or an empty composer, not while editing text.",
    "outside text inputs": "Printable shortcuts must not steal characters from an Input.",
    "Search, then Enter": "Enter advances from search into the filtered conversation list.",
}


def test_help_text_only_promises_bound_keys():
    bound = set().union(*(_binding_keys(cls) for cls in _tui_widget_classes()))
    missing = sorted(_promised_keys() - bound)
    assert not missing, (
        f"help text promises keys with no TUI binding: {missing}. "
        "Bind the key or correct the promise."
    )


def test_contextual_help_promises_stay_explicit():
    all_help = "\n".join((HELP_TEXT, EMPTY_MESSAGES_HINT, *_help_label_texts()))
    missing = [phrase for phrase in _CONDITIONAL_PROMISES if phrase not in all_help]
    assert not missing, (
        f"conditional help promises changed without a registry decision: {missing}. "
        "Update the text and _CONDITIONAL_PROMISES together."
    )


# --- Mounted Input population and Enter behavior -------------------------------------


async def _mounted_input_ids() -> set[str]:
    ids: set[str] = set()
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        ids.update(inp.id for inp in app.query(Input) if inp.id)
        for factory in _MODAL_FACTORIES.values():
            modal = factory()
            app.push_screen(modal)
            await _pause_until(pilot, lambda: modal.is_mounted)
            ids.update(inp.id for inp in modal.query(Input) if inp.id)
            app.pop_screen()
            await pilot.pause()
    return ids


async def test_every_mounted_tui_input_id_is_known():
    # A new Input (including a subclass, multiline construction, or variable id) is discovered
    # from the mounted tree and requires a deliberate Enter-behavior decision here.
    assert await _mounted_input_ids() == {
        "search",
        "composer",
        "new-profile",
        "target-lang",
        "known-langs",
        "unknown-langs",
        "translate-model",
        "translate-max",
        "suggest-history",
        "suggest-model",
        "login-input",
        "readlang-input",
    }


def test_app_input_submitted_is_an_allowlist():
    text = _APP_PY.read_text(encoding="utf-8")
    match = re.search(r"async def on_input_submitted.*?(?=\n    async def |\n    def |\Z)", text, re.S)
    assert match, "MessengerTUI.on_input_submitted not found"
    body = match.group(0)
    assert 'id != "composer"' in body, (
        "on_input_submitted must allowlist only the search behavior and composer send path; "
        "a denylist lets modal fields fall through into a real send."
    )


def _accounts_snapshot(screen, store, translator, suggester, notes):
    return (
        tuple(store.list_profiles()),
        len(translator.saved),
        len(suggester.saved),
        len(notes),
        type(screen.app.screen).__name__ if screen.app else None,
    )


_VALID_VALUE_BY_ID = {
    "target-lang": "ru",
    "translate-max": "50",
    "suggest-history": "50",
}


async def test_no_settings_input_is_silent_on_enter():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen, store, translator, suggester = _accounts_screen_with_everything()
        notes = []
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        app.push_screen(screen)
        await _pause_until(pilot, lambda: screen.is_mounted)

        silent = []
        for inp in list(screen.query(Input)):
            before = _accounts_snapshot(screen, store, translator, suggester, notes)
            inp.focus()
            inp.value = _VALID_VALUE_BY_ID.get(inp.id or "", "zz")
            await pilot.press("enter")
            await pilot.pause()
            after = _accounts_snapshot(screen, store, translator, suggester, notes)
            if before == after:
                silent.append(inp.id)
            if app.screen is not screen:
                app.pop_screen()
                await pilot.pause()

        assert not silent, (
            f"settings fields with no observable Enter effect: {silent}. "
            "Wire a handler or explicitly register and justify the no-op."
        )


async def test_no_login_or_readlang_input_is_silent_on_enter():
    cases = (
        (ReadLangScreen(), "ru", None),
        (LoginScreen(FakeTuiLoginSession()), "+10000000000", "login-prompt"),
    )
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        silent = []
        for screen, value, label_id in cases:
            dismissed = []
            app.push_screen(screen, callback=dismissed.append)
            await _pause_until(pilot, lambda: screen.is_mounted)
            before_label = screen.query_one(f"#{label_id}", Label).content if label_id else None
            inp = screen.query_one(Input)
            inp.focus()
            inp.value = value
            await pilot.press("enter")
            await pilot.pause()
            after_label = (
                screen.query_one(f"#{label_id}", Label).content
                if label_id and app.screen is screen
                else before_label
            )
            if not dismissed and after_label == before_label:
                silent.append(inp.id)
            if app.screen is screen:
                app.pop_screen()
                await pilot.pause()
        assert not silent, f"login-like fields with no observable Enter effect: {silent}"


async def test_search_enter_focuses_the_first_filtered_conversation():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.focus()
        search.value = "ann"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        dialogs = app.query_one("#dialogs", ListView)
        assert app.focused is dialogs
        assert dialogs.index == 0


# --- Printable screen binding reachability -------------------------------------------


def _printable_binding_keys(cls: type) -> set[str]:
    return {key for key in _binding_keys(cls) if len(key) == 1 and key.isprintable()}


_PRINTABLE_BINDING_ALTERNATIVES = {
    # (screen, swallowed key): (focus selector, alternative keys, screen action)
    ("AccountsScreen", "a"): ("#new-profile", ("enter",), "add_account"),
}


async def _mounted_screens_with_inputs_and_printable_bindings() -> set[str]:
    app = MessengerTUI(client=TuiStubClient())
    candidates = set()
    async with app.run_test() as pilot:
        await pilot.pause()
        for modal_cls, factory in _MODAL_FACTORIES.items():
            modal = factory()
            app.push_screen(modal)
            await _pause_until(pilot, lambda: modal.is_mounted)
            if modal.query(Input) and _printable_binding_keys(modal_cls):
                candidates.add(modal_cls.__name__)
            app.pop_screen()
            await pilot.pause()
    return candidates


async def test_printable_binding_detector_scope_is_computed():
    actual = await _mounted_screens_with_inputs_and_printable_bindings()
    registered = {screen_name for screen_name, _ in _PRINTABLE_BINDING_ALTERNATIVES}
    assert actual == registered, (
        "Screens combining Input with printable bindings changed. "
        f"unregistered={sorted(actual - registered)}, stale={sorted(registered - actual)}"
    )


async def test_registered_printable_binding_alternatives_actually_fire():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen, *_ = _accounts_screen_with_everything()
        app.push_screen(screen)
        await _pause_until(pilot, lambda: screen.is_mounted)

        failures = []
        for (screen_name, swallowed_key), (selector, keys, action) in (
            _PRINTABLE_BINDING_ALTERNATIVES.items()
        ):
            fired = []
            original = getattr(screen, f"action_{action}")

            def record(*args, **kwargs):
                fired.append(True)

            setattr(screen, f"action_{action}", record)
            try:
                target = screen.query_one(selector)
                target.focus()
                if isinstance(target, Input):
                    target.value = ""
                for key in keys:
                    await pilot.press(key)
                    await pilot.pause()
            finally:
                setattr(screen, f"action_{action}", original)
            if not fired:
                failures.append(f"{screen_name}.{swallowed_key} via {selector} {keys}")

        assert not failures, f"registered alternatives did not fire their action: {failures}"


async def test_remove_profile_keys_fire_only_from_the_profile_list():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen, *_ = _accounts_screen_with_everything()
        app.push_screen(screen)
        await _pause_until(pilot, lambda: screen.is_mounted)
        fired = []
        original = screen.action_remove_account
        screen.action_remove_account = lambda: fired.append(True)  # type: ignore[method-assign]
        try:
            inp = screen.query_one("#new-profile", Input)
            inp.focus()
            inp.value = "xy"
            await pilot.press("d")
            await pilot.press("delete")
            await pilot.pause()
            assert inp.value != "xy", "the keys should edit the field while it owns focus"
            assert fired == []

            accounts = screen.query_one("#accounts", ListView)
            accounts.focus()
            for key in ("d", "delete"):
                await pilot.press(key)
                await pilot.pause()
            assert fired == [True, True]
        finally:
            screen.action_remove_account = original  # type: ignore[method-assign]


async def test_profile_no_ops_explain_why_they_did_nothing():
    notes = []
    app = MessengerTUI(client=TuiStubClient())
    app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        screen, *_ = _accounts_screen_with_everything()
        app.push_screen(screen)
        await _pause_until(pilot, lambda: screen.is_mounted)
        accounts = screen.query_one("#accounts", ListView)

        accounts.index = None
        screen.action_remove_account()
        accounts.index = 0
        screen.action_remove_account()
        active = next(item for item in screen.query(settings.AccountItem) if item.profile == "alice")
        screen.on_list_view_selected(ListView.Selected(accounts, active, 0))

        assert any("Select a profile" in note for note in notes)
        assert any("cannot be removed" in note for note in notes)
        assert any("already the current profile" in note for note in notes)


async def test_left_from_a_message_returns_to_conversations():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        bubble = list(app.query(bubbles.MessageBubble))[0]
        bubble.focus()
        await pilot.press("left")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


# --- Targeted slow-path regressions ----------------------------------------------------


async def test_add_account_mounts_login_before_connect_finishes():
    class SlowConnectClient:
        def __init__(self):
            self._client = object()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.disconnected = False

        async def connect(self):
            self.started.set()
            await self.release.wait()

        async def disconnect(self):
            self.disconnected = True

    store = FakeSessionStore(["alice"])
    client = SlowConnectClient()
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(),
            active="alice",
            store=store,
            account_client_factory=lambda name: client,
        )
        app.push_screen(screen)
        await _pause_until(pilot, lambda: screen.is_mounted)
        inp = screen.query_one("#new-profile", Input)
        inp.value = "bob"
        inp.focus()
        await pilot.press("enter")
        await _pause_until(pilot, lambda: isinstance(app.screen, LoginScreen))
        assert client.started.is_set()
        assert not client.release.is_set(), "LoginScreen waited for the slow connection before mounting"
        await pilot.press("escape")
        await pilot.pause()


async def test_outbound_prepare_shows_status_before_slow_await():
    from tg_messenger.agent.outbound_coordinator import PrepareResult

    class SlowCoordinator:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def prepare(self, dialog_id, text, *, telegram_lang_code=None, owner_id=None):
            self.started.set()
            await self.release.wait()
            return PrepareResult(status="error", error="Translation failed.")

    app = MessengerTUI(client=TuiStubClient(), outbound=object())
    coordinator = SlowCoordinator()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        app._coordinator = coordinator
        task = asyncio.create_task(app._outbound_flow(7, "hello"))
        await _pause_until(pilot, coordinator.started.is_set)
        strip = app.query_one("#suggestion", Static)
        assert strip.display is True
        assert str(strip.render()) == OUTBOUND_TRANSLATING
        coordinator.release.set()
        await task
        assert strip.display is False


async def test_cancelling_startup_profile_picker_exits_without_building_client():
    built = []
    app = MessengerTUI(
        profiles=["alice", "bob"],
        client_factory=lambda name: built.append(name) or TuiStubClient(),
    )
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, ProfileScreen))
        await pilot.press("escape")
        await pilot.pause()
    assert built == []
