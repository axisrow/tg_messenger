"""Modal screens for the TUI: the account picker, the login wizard, the translation-variant
and reaction pickers, the help overlay, the yes/no confirm, and the inbound-language prompt.

Each is a thin ``ModalScreen`` that dismisses with its result; the geometry of each card is shaped
by the main App.CSS (the ``#*-box`` ids), while the centering lives in each screen's DEFAULT_CSS.
Re-exported from ``tg_messenger.tui.app`` for backward-compatible imports.
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from tg_messenger.core.auth import LoginError, delivery_hint
from tg_messenger.tui.bubbles import REACTION_PRESETS

logger = logging.getLogger(__name__)

# this sentinel marks "send the original (untranslated) draft" in the variant picker.
ORIGINAL_SENTINEL = "__tg_messenger_original__"

# #124: the key-help overlay text (HelpScreen, opened with ? / F1). English, matching every frontend.
HELP_TEXT = """Navigation (arrow keys):
  ↑ / ↓     focus chain: Search → Tabs → Conversations → Messages → Input
  ← / →     enter conversation (→ from list) · leave (← on empty pane); switch tabs on the tabs row
  Space      jump to the end/start of a list (conversations and messages)
  Enter      open a conversation · send a message

Actions:
  Tab       accept the reply suggestion (otherwise move focus forward)
  Shift+Tab move focus backward
  r / x     react to the selected message
  Ctrl+S    settings: accounts + incoming translation (mode and languages)
  t         toggle incoming auto-translation (outside text inputs)
  Ctrl+T    translate the whole chat now
  @path [caption]  attach a file (input command)
  /tlang    incoming translation language (input command; prompts if omitted)
  /lang     outgoing translation language for the current conversation (input command)
  ? / F1    this help
  Esc       clear search · close window
  Ctrl+C    quit"""


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
            yield Label("Sign in to Telegram", id="login-title")
            yield Label("Phone number (international format):", id="login-prompt")
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
        # #187: in-flight feedback — after submitting the number the input clears but the network
        # request can take a moment; without this the screen looked frozen. Show "Sending code…"
        # until the delivery hint (or an error) replaces it.
        self.query_one("#login-prompt", Label).update("Sending code…")
        try:
            delivery = await self._session.submit_phone(phone)
        except Exception as exc:
            logger.exception("login: submit_phone failed")  # phone stays out of the log
            self.notify(f"Could not send code: {exc}", severity="error")
            return
        self.query_one("#login-prompt", Label).update(delivery_hint(delivery))
        self.query_one("#login-input", Input).placeholder = "Code"

    async def _do_code(self, code: str) -> None:
        try:
            await self._session.submit_code(code)
        except LoginError as exc:
            self.notify(str(exc), severity="error")  # state preserved — retry
            return
        except Exception as exc:
            logger.exception("login: submit_code failed")
            self.notify(f"Login error: {exc}", severity="error")
            return
        if self._session.state == "password":
            self.query_one("#login-prompt", Label).update("2FA password:")
            self.query_one("#login-input", Input).placeholder = "2FA password"
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
            self.notify(f"Login error: {exc}", severity="error")
            return
        self.dismiss(True)


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
            # #187: the "send original" row is a fixed label — it must NOT inline the whole draft
            # (a long message wrapped over many lines and duplicated the text already in the
            # composer). The draft is right there in the composer; the label just names the choice.
            rows.append(VariantItem("Send original", ORIGINAL_SENTINEL))
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
            yield Label("Keyboard shortcuts", id="help-title")
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
            yield Label("y — yes · n / Esc — no", id="confirm-help")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ReadLangScreen(ModalScreen[str | None]):
    """Prompt for the inbound reading language (#126).

    The reading language (``user_lang``) was previously settable only via TG_USER_LANG / the CLI.
    This modal lets the user set it from the TUI — crucially, it works in read-only channels where
    the composer (and so the /tlang command) is disabled. Dismisses the entered code, or None on
    Esc / empty submit. The code is validated by Translator.set_target_lang downstream.
    """

    DEFAULT_CSS = "ReadLangScreen { align: center middle; }"

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="readlang-box"):
            yield Label("Incoming translation language", id="readlang-title")
            yield Input(placeholder="e.g. ru, en, de", id="readlang-input")
            yield Label("Enter — save · Esc — cancel", id="readlang-help")

    def on_mount(self) -> None:
        self.query_one("#readlang-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        code = event.value.strip()
        self.dismiss(code or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
