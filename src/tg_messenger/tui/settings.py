"""Account/settings screen for the TUI (#163): a thin AccountsScreen orchestrating three cards
— the saved-profile list, inbound-translation settings, and reply-suggester settings.

Each settings card (Vertical subclass) owns its own compose + _load/_save + load-echo guards and
handles its OWN children's RadioSet/Switch/Input messages (Textual bubbles widget messages to the
owning widget). The add/remove account actions stay on AccountsScreen (key-bound a/d, they mount
LoginScreen/ConfirmScreen). Re-exported from ``tg_messenger.tui.app`` for backward-compatible
imports — including ``_make_real_client`` (the default account-client factory and a test seam).
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, RadioButton, RadioSet, Static, Switch

from tg_messenger.core.auth import LoginSession
from tg_messenger.core.languages import parse_lang_codes, validate_supported_lang_code
from tg_messenger.core.names import is_safe_profile_name, sanitize_profile_name
from tg_messenger.tui.screens import ConfirmScreen, LoginScreen

logger = logging.getLogger(__name__)

# default context size for the suggester settings field (#143); mirrors suggest.DEFAULT_HISTORY_LIMIT
DEFAULT_SUGGEST_HISTORY = 30


def _make_real_client(session_name: str):
    """Back-compat shim — the canonical factory lives in ``tg_messenger.tui.app`` (#178).

    Resolved lazily off the ``app`` module so a ``monkeypatch.setattr(tui_app, "_make_real_client",
    ...)`` is honored here too (and so importing this module never imports ``app`` at load time —
    that would be a cycle). ``AccountsScreen`` resolves its default through the same path.
    """
    from tg_messenger.tui import app as _app

    return _app._make_real_client(session_name)


class AccountItem(ListItem):
    """One saved account profile in the settings screen; the active one is marked."""

    def __init__(self, profile: str, active: bool):
        mark = "  (текущий)" if active else ""
        super().__init__(Static(f"{profile}{mark}", markup=False))
        self.profile = profile


class ProfileListCard(Vertical):
    """The saved-profile list section of AccountsScreen (#163).

    A thin composition wrapper: it owns the profile-list markup (`#accounts` + `#new-profile`),
    but the add/remove ACTIONS stay on AccountsScreen — they are key-bound (`a`/`d`) and mount the
    LoginScreen/ConfirmScreen sub-screens, which is naturally screen-level work. The screen reaches
    into `#accounts`/`#new-profile` (query searches the whole DOM subtree) for those flows.
    """

    def __init__(self, *, profiles, active):
        super().__init__(id="profiles-section")
        self._profiles = list(profiles)
        self._active = active

    def compose(self) -> ComposeResult:
        yield Label("Аккаунты", id="accounts-title")
        yield ListView(
            *(AccountItem(p, p == self._active) for p in self._profiles),
            id="accounts",
        )
        yield Label("a — добавить · d — удалить · Esc — закрыть", id="accounts-help")
        yield Input(placeholder="Имя нового профиля", id="new-profile")

    async def refresh_profiles(self, profiles, active) -> None:
        self._profiles = list(profiles)
        self._active = active
        lv = self.query_one("#accounts", ListView)
        await lv.clear()
        for p in self._profiles:
            await lv.append(AccountItem(p, p == self._active))


class TranslateSettingsCard(Vertical):
    """Inbound-translation settings (#143/#163), composed only when a Translator is wired.

    Owns its own compose + load/save + the load-echo guards; it handles its OWN children's
    RadioSet.Changed / Input.Submitted (Textual bubbles widget messages to the owning widget).
    A model change can't dismiss the screen from here — the card posts ModelChanged and the screen
    dismisses with the rebuilt Translator.
    """

    # Inbound-translation modes, in display order. The id is the stored TranslateMode literal.
    _TRANSLATE_MODE_LABELS = (
        ("off", "Выкл — не переводить"),
        ("all_unknown", "Всё незнакомое (кроме моих языков)"),
        ("skip_known", "Кроме знакомых (список ниже)"),
        ("only_unknown", "Только указанные (список ниже)"),
    )

    class ModelChanged(Message):
        """Posted when a new translation model is committed — the screen dismisses with it."""

        def __init__(self, translator) -> None:
            self.translator = translator
            super().__init__()

    def __init__(self, *, translator):
        super().__init__(id="translate-section")
        # inbound-translation settings live on the injected Translator (it owns the Storage).
        self._translator = translator
        # the mode last loaded-from / saved-to storage. RadioSet.Changed is delivered async (after
        # the worker that set it returns), so a sync "loading" flag can't gate it; instead we save
        # only when the pressed mode actually DIFFERS from this, which the programmatic load never does.
        self._applied_mode: str | None = None
        # the model last loaded-from / saved-to storage; a save only re-probes/rebuilds the
        # Translator when this actually changes (an empty string means "fall back to env").
        self._applied_model: str = ""

    def compose(self) -> ComposeResult:
        yield Label("Перевод входящих", id="translate-title")
        with RadioSet(id="translate-mode"):
            for mode_id, label in self._TRANSLATE_MODE_LABELS:
                yield RadioButton(label, id=f"mode-{mode_id}")
        # THREE explicit fields, each with a PERSISTENT border_title caption (visible on
        # the frame even with a value typed — a placeholder vanishes on input). The two
        # language lists are always shown so nothing silently changes meaning by mode.
        target = Input(placeholder="напр. ru", id="target-lang")
        target.border_title = "Мой язык (на что переводить)"
        yield target
        known = Input(placeholder="напр. ru, en", id="known-langs")
        known.border_title = "Не переводить"
        yield known
        unknown = Input(placeholder="напр. en, ja", id="unknown-langs")
        unknown.border_title = "Переводить (пусто = всё переводить)"
        yield unknown
        model_field = Input(placeholder="напр. openai:glm-5.1", id="translate-model")
        model_field.border_title = "Модель для перевода"
        yield model_field
        max_field = Input(placeholder="напр. 100", id="translate-max")
        max_field.border_title = "Сколько переводить за раз (Ctrl+T)"
        yield max_field
        yield Label("Enter в поле — сохранить", id="translate-help")

    async def _load(self) -> None:
        if self._translator is None:
            return
        try:
            settings = await self._translator.get_settings()
        except Exception:
            logger.exception("settings: failed to load translation settings")
            return
        # select the stored mode in the RadioSet; record it so the resulting (async) RadioSet.Changed
        # is recognised as the load echo, not a user change, and doesn't auto-save.
        mode = settings.get("mode") or "off"
        self._applied_mode = mode
        try:
            self.query_one(f"#mode-{mode}", RadioButton).value = True
        except Exception:
            logger.warning("settings: unknown stored translate mode %r", mode)
        # three independent fields → three independent settings (no per-mode branching)
        self.query_one("#target-lang", Input).value = settings.get("target") or ""
        self.query_one("#known-langs", Input).value = ", ".join(settings.get("known") or [])
        self.query_one("#unknown-langs", Input).value = ", ".join(settings.get("unknown") or [])
        self.query_one("#translate-model", Input).value = settings.get("model") or ""
        # remember the loaded model so a save only re-probes/rebuilds when it actually changed
        self._applied_model = settings.get("model") or ""
        max_msgs = settings.get("max_messages")
        self.query_one("#translate-max", Input).value = str(max_msgs) if max_msgs else ""

    def _selected_mode(self) -> str:
        """The currently-pressed translate-mode RadioButton id → its TranslateMode literal."""
        pressed = self.query_one("#translate-mode", RadioSet).pressed_button
        if pressed is not None and pressed.id and pressed.id.startswith("mode-"):
            return pressed.id[len("mode-"):]
        return "off"

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "translate-mode":
            return
        mode = self._selected_mode()
        # ignore the echo of the programmatic selection done while loading — only a genuine user
        # mode change (differs from what's stored/applied) should persist.
        if mode == self._applied_mode:
            return
        self.run_worker(self._save(), exclusive=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in any translation field saves. The message still bubbles on past this card to the
        # App regardless, so the explicit id allowlist below — not bubbling isolation — is what keeps
        # a foreign field (e.g. the sibling profile-name Input) from triggering a translate save.
        if event.input.id in ("target-lang", "known-langs", "unknown-langs",
                               "translate-model", "translate-max"):
            self.run_worker(self._save(), exclusive=True)

    async def _save(self) -> None:
        if self._translator is None:
            return
        mode = self._selected_mode()
        target = self.query_one("#target-lang", Input).value.strip()
        max_raw = self.query_one("#translate-max", Input).value.strip()
        try:
            # three independent fields → persist all three every save (no per-mode branching),
            # so editing one field never clobbers another
            target_code = validate_supported_lang_code(target) if target else None
            known = parse_lang_codes(self.query_one("#known-langs", Input).value)
            unknown = parse_lang_codes(self.query_one("#unknown-langs", Input).value)
            max_messages = self._parse_max_messages(max_raw)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        model = self.query_one("#translate-model", Input).value.strip()
        # Validate-then-commit, atomic across the WHOLE save: a CHANGED model is built+probed FIRST
        # (validation only — no persistence, no cache clear, no translator swap). Only once the model
        # is proven AND nothing else can fail do we commit everything together (settings + the model
        # override). This way a bad model never wipes the cache / half-writes settings, and a later
        # settings failure can't leave a persisted model diverging from the live translator.
        model_changed = model != self._applied_model
        new_translator = None
        if model_changed:
            new_translator = await self._validate_model(model)
            if new_translator is None:
                return  # bad model → nothing persisted, cache untouched
        try:
            # commit the other settings; for a changed model also persist it in the SAME block so the
            # two writes succeed or fail together (no persisted-model-without-settings window).
            await self._translator.set_settings(
                mode=mode, target=target_code, known=known, unknown=unknown,
                max_messages=max_messages,
            )
            if model_changed:
                from tg_messenger.agent.translate import set_translate_model
                # blank field → clear the kv override (fall back to env); else persist the model
                await set_translate_model(self._translator.storage, model or None)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        except Exception:
            logger.exception("settings: failed to save translation settings")
            self.notify("Не удалось сохранить настройки перевода", severity="error")
            return
        self._applied_mode = mode
        if model_changed:
            # everything committed → adopt the new translator and hand it back to the app via the
            # screen (a card can't dismiss the screen; it posts ModelChanged and the screen does).
            # NB: the probe only checks structured-output support, NOT credentials — an invalid API
            # key surfaces on the first actual translation, not here, so don't claim "verified".
            self._translator = new_translator
            self._applied_model = model
            self.notify("Модель сохранена — проверьте перевод в чате")
            self.post_message(self.ModelChanged(new_translator))
            return
        self.notify("Настройки перевода сохранены")

    @staticmethod
    def _parse_max_messages(raw: str) -> int | None:
        """Parse the per-pass cap field: blank → None (use env/default); else a positive int."""
        if not raw:
            return None
        try:
            n = int(raw)
        except ValueError as exc:
            raise ValueError("Сколько переводить: введите число") from exc
        if n < 1:
            raise ValueError("Сколько переводить: число должно быть ≥ 1")
        return n

    async def _validate_model(self, model: str):
        """Build+probe a freshly chosen model and return the candidate Translator, or None on failure.

        VALIDATION ONLY — no side effects: it does NOT persist the model, clear the cache, swap
        self._translator, or change _applied_model. The caller commits all of that atomically only
        after this succeeds, so a bad model leaves storage and the live translator untouched.

        (build_translator_with_probe does cache the per-model structured-output method in kv; that is
        a harmless detection cache keyed by model name, not the model override or any translation.)
        """
        # #187: "Сохраняю модель…", not "Проверяю модель…" — the probe only checks structured-output
        # SUPPORT, not credentials (an invalid key surfaces on the first real translation). "Проверяю"
        # over-promised a validation that doesn't happen; the success toast already says to check the
        # translation in the chat.
        self.notify("Сохраняю модель…")
        try:
            from tg_messenger.agent.factory import build_translator_with_probe
            from tg_messenger.agent.translate import translate_model_from_env
        except ImportError:
            logger.exception("settings: agent extra unavailable for model change")
            self.notify("Переводчик недоступен (нет extra [agent])", severity="error")
            return None
        target_model = model or translate_model_from_env()
        if not target_model:
            self.notify("Не задана модель перевода", severity="error")
            return None
        # the SQLite Storage the Translator caches into lives on the current translator.
        # Reuse it so settings/cache stay in one DB.
        storage = self._translator.storage
        try:
            new_translator = await build_translator_with_probe(storage, target_model)
        except Exception:
            logger.exception("settings: failed to build translator for model %r", target_model)
            self.notify("Не удалось применить модель — проверьте имя/ключ", severity="error")
            return None
        return new_translator


class SuggestSettingsCard(Vertical):
    """Reply-suggester settings (#143/#163), composed only when a Suggester is wired.

    Owns its own compose + load/save + the enabled-toggle load-echo guard, and handles its OWN
    children's Switch.Changed / Input.Submitted.
    """

    def __init__(self, *, suggester):
        super().__init__(id="suggest-section")
        # reply-suggester settings (#143) live on the injected Suggester (it owns its Storage).
        self._suggester = suggester
        # the suggester model last loaded/saved; a save only rebuilds the suggest_fn on a real change.
        self._applied_suggest_model: str = ""
        # the enabled state last loaded/saved. Switch.Changed is delivered ASYNC (after the worker
        # that set Switch.value returns), so a synchronous "loading" flag can't gate it — instead we
        # save only when the toggled value DIFFERS from this, exactly like the translate _applied_mode.
        self._applied_suggest_enabled: bool = True

    def compose(self) -> ComposeResult:
        yield Label("Суфлёр ответов (💡)", id="suggest-title")
        with Horizontal(id="suggest-enabled-row"):
            yield Label("Подсказывать ответы")
            yield Switch(value=True, id="suggest-enabled")
        history_field = Input(placeholder="напр. 30", id="suggest-history")
        history_field.border_title = "Сколько сообщений контекста"
        yield history_field
        suggest_model_field = Input(placeholder="напр. openai:gpt-4o", id="suggest-model")
        suggest_model_field.border_title = "Модель суфлёра (пусто = по умолчанию)"
        yield suggest_model_field
        yield Label("Enter в поле — сохранить", id="suggest-help")

    async def _load(self) -> None:
        if self._suggester is None:
            return
        try:
            settings = await self._suggester.get_settings()
        except Exception:
            logger.exception("settings: failed to load suggester settings")
            return
        enabled = bool(settings.get("enabled", True))
        # record the loaded value FIRST so the async Switch.Changed echo is recognised as the load,
        # not a user toggle, and doesn't auto-save (the synchronous flag couldn't — Changed fires later).
        self._applied_suggest_enabled = enabled
        self.query_one("#suggest-enabled", Switch).value = enabled
        self.query_one("#suggest-history", Input).value = str(settings.get("history") or "")
        model = settings.get("model") or ""
        self.query_one("#suggest-model", Input).value = model
        self._applied_suggest_model = model

    async def _save(self) -> None:
        if self._suggester is None:
            return
        enabled = self.query_one("#suggest-enabled", Switch).value
        history_raw = self.query_one("#suggest-history", Input).value.strip()
        model = self.query_one("#suggest-model", Input).value.strip()
        try:
            history = int(history_raw) if history_raw else DEFAULT_SUGGEST_HISTORY
        except ValueError:
            self.notify("Контекст: введите число", severity="error")
            return
        model_changed = model != self._applied_suggest_model
        if model_changed and model:
            self.notify("Проверяю модель суфлёра…")
        try:
            # save_settings validates the model (building its suggest_fn) BEFORE persisting, so a bad
            # model name raises here and nothing is half-committed (mirrors the translator ordering).
            await self._suggester.save_settings(enabled=enabled, history=history, model=model or None)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        except Exception:
            logger.exception("settings: failed to save suggester settings")
            self.notify("Не удалось сохранить настройки суфлёра", severity="error")
            return
        self._applied_suggest_model = model
        self._applied_suggest_enabled = enabled
        self.notify("Настройки суфлёра сохранены")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id != "suggest-enabled":
            return
        # ignore the async echo of the programmatic load (value == what we just loaded) — only a
        # genuine user toggle (differs from the applied state) should persist. Same shape as the
        # translate RadioSet.Changed guard, which is timing-robust where a sync flag is not.
        if event.value == self._applied_suggest_enabled:
            return
        self.run_worker(self._save(), exclusive=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("suggest-history", "suggest-model"):
            self.run_worker(self._save(), exclusive=True)


class AccountsScreen(ModalScreen[object]):
    """Account settings (#115): list saved profiles (active marked), add a new one, remove one.

    A thin orchestrator (#163): it composes three sibling cards — ProfileListCard,
    TranslateSettingsCard, SuggestSettingsCard — inside one scroll container and coordinates
    dismiss. Each settings card owns its own load/save/guards.

    Dismisses with a rebuilt Translator when the user picks a new translation model (so the main
    app can swap it in), or with None otherwise.

    Adding runs the SAME LoginScreen/LoginSession wizard against a freshly-built client for the
    typed profile name, then persists via the client's save_session() (→ SessionStore). Switching
    the active profile in-session is deferred (it would need a full deps rebuild + reconnect).
    Reuses LoginSession + SessionStore — login is NOT reimplemented here.
    """

    # #116-parity: a centered, bordered card (the box geometry mirrors the other modals).
    DEFAULT_CSS = (
        "AccountsScreen { align: center middle; } "
        # VerticalScroll: keep the card sized to its content (height auto) but capped at 80% of the
        # screen; past that it scrolls instead of clipping the trailing suggester section. Override
        # VerticalScroll's default height: 1fr so an empty-ish card doesn't balloon to full height.
        "#accounts-box { width: 60%; max-width: 64; height: auto; max-height: 80%; "
        "padding: 1 2; border: round $primary; background: $surface; } "
        "#profiles-section { height: auto; } "
        # ListView defaults to height: 1fr; #accounts-box is a VerticalScroll (a scroll VIEWPORT),
        # which hands a 1fr child the full viewport height (= the 80% cap) even with one profile —
        # so the modal ballooned to 80% of the screen when no translate/suggest cards sized it.
        # height: auto sizes the list to its content; overflow is the box's job (its max-height: 80%
        # is the single scroll region), so no separate per-list cap is needed.
        "#accounts { height: auto; } "
        "#translate-section { height: auto; margin-top: 1; border-top: solid $primary; padding-top: 1; } "
        "#translate-section RadioSet { height: auto; } "
        "#suggest-section { height: auto; margin-top: 1; border-top: solid $primary; padding-top: 1; } "
        "#suggest-enabled-row { height: auto; } "
        "#suggest-enabled-row Label { padding: 1 1 0 0; } "
        # the field captions live in border_title, which inherits the (grey, blurred) border colour
        # and is nearly unreadable by default — give the translation inputs a visible border + a
        # bold accent caption so the labels stay legible whether the field is focused or not.
        "#target-lang, #known-langs, #unknown-langs, #translate-model, #translate-max, "
        "#suggest-history, #suggest-model "
        "{ border: round $primary; border-title-color: $accent; border-title-style: bold; }"
    )

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit", priority=True, show=False),
        Binding("escape", "close", "Close", show=False),
        Binding("a", "add_account", "Add account", show=False),
        Binding("d", "remove_account", "Remove", show=False),
    ]

    def __init__(self, *, profiles, active, store, account_client_factory=None,
                 login_session=None, translator=None, suggester=None):
        super().__init__()
        # #121: profiles from list_profiles() are already canonical (sanitized stems); the active
        # name comes from the raw session_name, so canonicalize it ONCE here. Marker + delete guard
        # then compare canonical-to-canonical, so an active raw name that sanitizes differently
        # (e.g. "work/personal" → "work_personal") is still recognised and protected.
        self._profiles = list(profiles)
        self._active = sanitize_profile_name(active)
        self._store = store
        # test seams: build the new-profile client / skip the network login flow.
        # #178: resolve the default off the `app` module at call time (not the module-local name),
        # so monkeypatch.setattr(tui_app, "_make_real_client", ...) reaches this default exactly as
        # it did in the pre-split monolith — the canonical binding lives in tg_messenger.tui.app.
        if account_client_factory is None:
            from tg_messenger.tui import app as _app

            account_client_factory = _app._make_real_client
        self._account_client_factory = account_client_factory
        self._login_session = login_session
        # None means the [agent] extra / model isn't configured — the section's card is then hidden.
        self._translator = translator
        self._suggester = suggester

    def compose(self) -> ComposeResult:
        # VerticalScroll (not a plain Vertical): once profiles + translate + suggester sections
        # exceed max-height the card must SCROLL, and the scroll container has to be focusable so
        # arrow/page keys actually move it — a bare Vertical with overflow-y clips the last section
        # (the suggester settings) below the card edge and swallows scroll keys into the background.
        with VerticalScroll(id="accounts-box"):
            yield ProfileListCard(profiles=self._profiles, active=self._active)
            # the settings cards are composed only when their dependency is wired ([agent] extra +
            # the relevant model). Their values load async in on_mount (can't read in compose).
            if self._translator is not None:
                yield TranslateSettingsCard(translator=self._translator)
            if self._suggester is not None:
                yield SuggestSettingsCard(suggester=self._suggester)

    def on_mount(self) -> None:
        if self._translator is not None:
            self.run_worker(self.query_one(TranslateSettingsCard)._load(), exclusive=False)
        if self._suggester is not None:
            self.run_worker(self.query_one(SuggestSettingsCard)._load(), exclusive=False)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # #187: selecting a profile in the accounts list looked like it would switch accounts, but
        # did nothing (a dead end). In-session switching needs a full deps rebuild + reconnect, so
        # give explicit feedback pointing at the real path (restart) instead of silence. The active
        # profile is a no-op; add/remove use their own key bindings, not selection.
        item = event.item
        if isinstance(item, AccountItem) and item.profile != self._active:
            self.notify(f"Переключение на «{item.profile}»: перезапустите приложение")

    def on_translate_settings_card_model_changed(
        self, event: "TranslateSettingsCard.ModelChanged"
    ) -> None:
        # a card can't dismiss the screen; it posts ModelChanged and we hand the rebuilt translator
        # back to the app (so it can swap the live translator in).
        self._translator = event.translator
        self.dismiss(event.translator)

    # --- thin shims so direct-call tests keep targeting the screen (the card owns the real work) ---

    async def _save_translate_settings(self) -> None:
        await self.query_one(TranslateSettingsCard)._save()

    async def _save_suggest_settings(self) -> None:
        await self.query_one(SuggestSettingsCard)._save()

    async def _validate_model(self, model: str):
        return await self.query_one(TranslateSettingsCard)._validate_model(model)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_add_account(self) -> None:
        name = self.query_one("#new-profile", Input).value.strip()
        if not name:
            self.query_one("#new-profile", Input).focus()
            return
        # #121: reject a name that isn't already its own filename-safe form (it would silently
        # collapse onto a DIFFERENT session file and overwrite another account), or whose canonical
        # form already exists. Checked BEFORE building a client — no network on a bad name.
        if not is_safe_profile_name(name):
            self.notify(
                f"Недопустимое имя профиля: {name} (только латиница, цифры, _.-)",
                severity="error",
            )
            self.query_one("#new-profile", Input).focus()
            return
        if name in self._store.list_profiles():
            self.notify(f"Профиль уже существует: {name}", severity="error")
            self.query_one("#new-profile", Input).focus()
            return
        self.run_worker(self._add_account(name), exclusive=True)

    async def _add_account(self, name: str) -> None:
        # build + connect a client for the NEW profile, then run the existing login wizard.
        # name is already validated as safe + unique by action_add_account.
        client = None
        try:
            if self._login_session is not None:  # test seam: skip the real client/network
                session = self._login_session
                client = self._account_client_factory(name)
            else:
                client = self._account_client_factory(name)
                await client.connect()
                session = LoginSession(getattr(client, "_client", client))
            ok = await self.app.push_screen_wait(LoginScreen(session))
            if not ok:
                return
            save_session = getattr(client, "save_session", None)
            if save_session is not None:
                save_session()  # → SessionStore.save(name, ...)
        except Exception:
            logger.exception("settings: add account failed")  # name only; no secrets logged
            self.notify(f"Не удалось добавить профиль: {name}", severity="error")
            return
        finally:
            if client is not None:
                disconnect = getattr(client, "disconnect", None)
                if disconnect is not None:
                    try:
                        await disconnect()
                    except Exception:
                        logger.warning("settings: client disconnect failed", exc_info=True)
        await self._refresh(self._store.list_profiles() or [*self._profiles, name])
        self.query_one("#new-profile", Input).value = ""
        self.notify(f"Профиль добавлен: {name}")

    def action_remove_account(self) -> None:
        lv = self.query_one("#accounts", ListView)
        item = lv.highlighted_child
        # #121: both sides are canonical (item.profile from list_profiles, self._active sanitized
        # in __init__), so the active profile is recognised even when its raw name differs.
        if not isinstance(item, AccountItem) or item.profile == self._active:
            return  # never remove the active profile
        # #121: destructive — confirm before deleting a saved session (parity with CLI).
        self.run_worker(self._confirm_remove(item.profile), exclusive=True)

    async def _confirm_remove(self, profile: str) -> None:
        ok = await self.app.push_screen_wait(
            ConfirmScreen(f"Удалить профиль «{profile}»?")
        )
        if not ok:
            return
        try:
            self._store.delete(profile)
        except Exception:
            logger.exception("settings: remove account failed")
            self.notify(f"Не удалось удалить профиль: {profile}", severity="error")
            return
        await self._refresh(self._store.list_profiles())
        self.notify(f"Профиль удалён: {profile}")

    async def _refresh(self, profiles) -> None:
        # the profile list lives on its card now; the screen only coordinates the add/remove flow.
        self._profiles = list(profiles)
        await self.query_one(ProfileListCard).refresh_profiles(self._profiles, self._active)


