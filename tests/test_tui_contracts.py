"""Contract: no interactive TUI element is silently unreachable (#223).

Two mechanical detectors, mirroring the ``tests/test_stream_consumers.py`` pattern
(source-text/behavioral contracts that fail loudly instead of relying on someone
remembering to write a regression test):

* **Detector A** ("field with no addressee") — an ``Input`` posts ``Input.Submitted``
  on Enter; if nothing on the bubbling path *does* anything observable with it, the
  field is silently dead (no error, no log — Textual just drops the message). An
  ancestor-walk-for-a-method-name is useless here: ``MessengerTUI.on_input_submitted``
  exists at the end of EVERY bubbling path, but for most fields it is a sink, not a
  handler. So this detector is effect-based: focus the field, type a value, press
  Enter, and assert something OBSERVABLE changed. This is exactly the class of bug
  that hid ``#new-profile`` having no Enter handler at all (issue #223). Two tests
  apply it: one to every field on ``AccountsScreen`` (its cards' richer state — saved
  settings, screen pushes — needs a multi-part snapshot), one to ``LoginScreen`` and
  ``ReadLangScreen`` (each dismisses or updates a label instead) — proving the
  mechanism isn't secretly scoped to the one screen the reported bug happened to be on.

* **Detector B** ("key swallowed by a focused field") — a printable-key Binding on a
  screen that also has a focusable Input can be entirely unreachable once focus is in
  that Input (Textual routes printable keys to the focused widget first). A static
  scan over-reports (e.g. ``t`` vs the differently-named ``ctrl+t`` action are not
  decidable by regex), so this detector presses the key for real and asks: did the
  bound action actually fire, or did the input just grow a letter? ``AccountsScreen``
  is the only screen in the app where a printable Binding and a focusable Input
  coexist (checked against every screen's BINDINGS — see the comment above
  ``_PRINTABLE_BINDING_ALTERNATIVES``), so it's the only one this detector visits.

Plus two narrow static guards pinning the structural leak fix (#223 defect 3):
input ids are enumerated so a new field can't silently skip both detectors, and the
composer allowlist in ``app.py`` is pinned against regressing back to a denylist.
"""

from __future__ import annotations

import re
from pathlib import Path

from textual.widgets import Input, Label

from tests.test_tui import (
    FakeSessionStore,
    FakeTuiLoginSession,
    SavingStubClient,
    StubSuggesterTUI,
    StubTranslator,
    TuiStubClient,
    _pause_until,
)
from tg_messenger.tui.app import AccountsScreen, MessengerTUI
from tg_messenger.tui.screens import LoginScreen, ReadLangScreen

_SRC = Path(__file__).resolve().parent.parent / "src" / "tg_messenger"
_TUI = _SRC / "tui"
_APP_PY = _TUI / "app.py"


# --- static: enumerate every Input id in the TUI (tripwire, test_stream_consumers.py pattern) ---


def _tui_input_ids() -> set[str]:
    ids = set()
    for path in _TUI.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        ids.update(re.findall(r'Input\([^)]*\bid="([^"]+)"', text))
    return ids


def test_every_tui_input_id_is_known():
    # tripwire: a new Input id means someone must DECIDE its Enter behavior (wire it into
    # a real handler, or explicitly document why it's a no-op like #search) and add it here.
    # Silently doing neither is exactly how #new-profile's dead Enter went unnoticed.
    assert _tui_input_ids() == {
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


# --- static: pin the structural leak fix (denylist -> allowlist) against regression ---


def test_app_input_submitted_is_an_allowlist():
    # #223 defect 3: MessengerTUI.on_input_submitted used to be a DENYLIST (only "search" was
    # excluded), so any modal field whose own handler didn't event.stop() the message could fall
    # through into the SEND path — reproduced live: typing "secret" into the settings screen's
    # profile-name field and pressing Enter sent "secret" to the open chat. The allowlist makes
    # that structurally impossible regardless of what future fields get added. Pin it so a future
    # refactor can't quietly flip it back to a denylist.
    text = _APP_PY.read_text(encoding="utf-8")
    match = re.search(r"async def on_input_submitted.*?(?=\n    async def |\n    def |\Z)", text, re.S)
    assert match, "MessengerTUI.on_input_submitted not found"
    body = match.group(0)
    assert 'id != "composer"' in body, (
        "on_input_submitted must allowlist ONLY the composer — a denylist (e.g. "
        '\'id == "search"\') lets any other field fall through into the send path.'
    )


# --- behavioral: Detector A — every settings field either has an observable Enter effect, ---
# --- or is an explicitly documented no-op.                                                ---

# fields where Enter is a deliberate no-op, with the reason (test_stream_consumers.py _DEFERRED
# pattern) — anything NOT listed here must produce an observable effect.
_SILENT_BY_DESIGN = {
    "search": "filters live on keystroke; Enter submitting is a documented no-op",
}


async def _accounts_screen_with_everything():
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "off", "target": "", "known": [], "unknown": []})
    suggester = StubSuggesterTUI()
    # #223 detector fix: without these two seams, Enter in #new-profile falls through to
    # AccountsScreen's real default (_make_real_client + a real client.connect()) — a genuine
    # Telethon connection to Telegram's servers from a "unit" test. That's not just a project-rule
    # violation (no network in tests); it's what made this very detector flaky: under full-suite
    # load the real connect attempt is slow enough that the observable-effect window closes before
    # the screen-stack change lands, and the detector wrongly reports #new-profile as silent.
    screen = AccountsScreen(
        profiles=store.list_profiles(), active="alice", store=store,
        account_client_factory=lambda name: SavingStubClient(name, store),
        login_session=FakeTuiLoginSession(),
        translator=translator, suggester=suggester,
    )
    return screen, store, translator, suggester


def _accounts_snapshot(screen, store, translator, suggester, notes):
    # observable state a settings-field Enter could plausibly change. A validation error that
    # aborts a save before it reaches the stub (e.g. "zz" is not a valid lang code) still counts
    # as an effect — it's a real `notify()` call, not silence — so track notifications too.
    return (
        tuple(store.list_profiles()),
        len(translator.saved),
        len(suggester.saved),
        len(notes),
        type(screen.app.screen).__name__ if screen.app else None,
    )


# values that are valid enough to reach a real save (not rejected by field-specific validation
# before anything observable happens) — a generic "zz" fails lang-code validation for target-lang
# and is silently meaningless for translate-max, which would make the detector cry wolf.
_VALID_VALUE_BY_ID = {
    "target-lang": "ru",
    "translate-max": "50",
    "suggest-history": "50",
}


async def test_no_settings_input_is_silent_on_enter():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen, store, translator, suggester = await _accounts_screen_with_everything()
        notes = []
        # notify() is a Widget method proxying to app.notify — a card's own self.notify() is NOT
        # caught by patching screen.notify (they're different bound methods); patch the app.
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        app.push_screen(screen)
        await _pause_until(pilot, lambda: screen.is_mounted)
        await pilot.pause()

        silent = []
        for inp in list(screen.query(Input)):
            if inp.id in _SILENT_BY_DESIGN:
                continue
            before = _accounts_snapshot(screen, store, translator, suggester, notes)
            inp.focus()
            await pilot.pause()
            inp.value = _VALID_VALUE_BY_ID.get(inp.id or "", "zz")
            await pilot.press("enter")
            await pilot.pause()  # let the worker/handler settle
            after = _accounts_snapshot(screen, store, translator, suggester, notes)
            if before == after:
                silent.append(inp.id)
            # new-profile's only observable effect is a NEW screen (LoginScreen) pushed on top —
            # pop it back off before the next field, so every field starts from the same clean
            # AccountsScreen state (otherwise "focus the next Input on `screen`" would target a
            # covered, inactive screen). action_add_account() pushes at most one screen per call,
            # so an `if` — not a `while` — states the actual invariant and fails fast (instead of
            # spinning) if that invariant is ever broken.
            if app.screen is not screen:
                app.pop_screen()
                await pilot.pause()

        assert not silent, (
            f"these Input fields produce NO observable effect on Enter: {silent}. "
            f"Either wire a real on_input_submitted handler, or add the id (with a reason) "
            f"to _SILENT_BY_DESIGN."
        )


# Detector A generalizes beyond AccountsScreen: LoginScreen and ReadLangScreen each own a single
# Input outside the settings cards, and both are exercised here so the mechanism isn't secretly
# scoped to one screen (the #223 altitude concern — a docstring claiming "no interactive TUI
# element" that only ever visits AccountsScreen would miss the same bug class on any other modal).
# Both currently pass (LoginScreen/ReadLangScreen are NOT broken) — this is proof the detector
# generalizes, not a regression test for a known-bad field like #new-profile.
#
# (input id, screen factory, value to submit, a label id whose CONTENT the handler is expected to
# change OR None when the only expected effect is dismiss()) — dismiss is always tracked via the
# push_screen callback, so a screen that only ever dismisses (ReadLangScreen) still gets a real
# effect check without needing a label at all.
_LOGIN_LIKE_SCREENS = (
    ("readlang-input", lambda: ReadLangScreen(), "ru", None),
    ("login-input", lambda: LoginScreen(FakeTuiLoginSession()), "+10000000000", "login-prompt"),
)


async def test_no_login_or_readlang_input_is_silent_on_enter():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]

        silent = []
        for input_id, make_screen, value, label_id in _LOGIN_LIKE_SCREENS:
            dismissed = []
            app.push_screen(make_screen(), callback=dismissed.append)
            await pilot.pause()
            before_label = app.screen.query_one(f"#{label_id}", Label).content if label_id else None
            before = (len(dismissed), len(notes), before_label)

            inp = app.screen.query_one(Input)
            inp.focus()
            await pilot.pause()
            inp.value = value
            await pilot.press("enter")
            await pilot.pause()  # let the worker (LoginScreen's network step) settle

            still_open = not dismissed
            after_label = (
                app.screen.query_one(f"#{label_id}", Label).content
                if label_id and still_open else before_label
            )
            after = (len(dismissed), len(notes), after_label)
            if before == after:
                silent.append(input_id)

            if still_open:
                app.pop_screen()
                await pilot.pause()

        assert not silent, (
            f"these Input fields produce NO observable effect on Enter: {silent}. "
            f"Either wire a real on_input_submitted handler, or add the id (with a reason) "
            f"to _SILENT_BY_DESIGN."
        )


# --- behavioral: Detector B — every printable-key binding on a screen with a focusable ---
# --- Input must be reachable some other way once focus is in that Input.               ---

# AccountsScreen is the only screen this detector needs to visit: it's the only one in tui/*.py
# with BOTH a focusable Input AND a printable-key Binding (checked against every BINDINGS list in
# screens.py/settings.py — LoginScreen/ReadLangScreen have an Input but only non-printable
# bindings; ConfirmScreen/HelpScreen have printable bindings but no Input). A screen combining
# both in the future would need registering here too, same as AccountsScreen's two entries below.

# (screen factory, key, action attr name, alternative-path description) — the alternative is
# asserted to exist as a REGISTERED fact here, not inferred, so a human decided it's sufficient.
_PRINTABLE_BINDING_ALTERNATIVES = {
    ("AccountsScreen", "a"): "Enter in #new-profile (ProfileListCard.AddRequested)",
    ("AccountsScreen", "d"): "the delete key alias (Binding('delete', 'remove_account', ...))",
}


async def test_no_printable_binding_is_unreachable_from_a_focused_input():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen, *_ = await _accounts_screen_with_everything()
        app.push_screen(screen)
        await _pause_until(pilot, lambda: screen.is_mounted)
        await pilot.pause()

        inp = screen.query_one("#new-profile", Input)
        trapped = []
        for binding_key, action in (("a", "add_account"), ("d", "remove_account")):
            fired = []
            orig = getattr(type(screen), f"action_{action}")

            def wrapper(self, *a, _orig=orig, _fired=fired, **kw):
                _fired.append(True)
                return _orig(self, *a, **kw)

            setattr(type(screen), f"action_{action}", wrapper)
            try:
                inp.focus()
                await pilot.pause()
                inp.value = ""
                await pilot.press(binding_key)
                await pilot.pause()
                swallowed = not fired and inp.value == binding_key
            finally:
                setattr(type(screen), f"action_{action}", orig)

            if swallowed:
                key = ("AccountsScreen", binding_key)
                if key not in _PRINTABLE_BINDING_ALTERNATIVES:
                    trapped.append(binding_key)

        assert not trapped, (
            f"these printable bindings are swallowed by a focused Input with NO registered "
            f"alternative: {trapped}. Either add a non-printable alias, or register the "
            f"existing alternative path in _PRINTABLE_BINDING_ALTERNATIVES."
        )
