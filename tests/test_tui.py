import asyncio
import inspect
from datetime import datetime, timezone

import pytest
from textual.containers import Vertical
from textual.widgets import Input, ListView, Static, Tabs

from tg_messenger.core.client import SendForbiddenError
from tg_messenger.core.models import (
    Dialog,
    IncomingEvent,
    Message,
    OutgoingEvent,
    ReactionEvent,
    User,
)
from tg_messenger.tui.app import (
    REACTION_PRESETS,
    DialogItem,
    EmojiPickerScreen,
    MessageBubble,
    MessengerTUI,
    ProfileItem,
    VariantItem,
    parse_lang_command,
    parse_media_command,
)


def test_parse_media_simple():
    assert parse_media_command("@a.jpg") == ("a.jpg", None)


def test_parse_media_quoted_path_with_caption():
    assert parse_media_command('@"с пробелом.png" подпись') == ("с пробелом.png", "подпись")


def test_parse_lang_command():
    assert parse_lang_command("/lang en") == ("set", "en")
    assert parse_lang_command("/lang auto") == ("auto", None)
    assert parse_lang_command("/lang off") == ("off", None)
    assert parse_lang_command("hello") is None
    with pytest.raises(ValueError):
        parse_lang_command("/lang")


def test_parse_media_path_and_caption():
    assert parse_media_command("@/path/x.jpg caption here") == ("/path/x.jpg", "caption here")


def test_parse_media_non_at_is_none():
    assert parse_media_command("hello world") is None


def test_parse_media_empty_after_at_is_none():
    assert parse_media_command("@") is None
    assert parse_media_command("@   ") is None


class TuiStubClient:
    def __init__(self):
        self.sent = []
        self.sent_event = asyncio.Event()
        self.read_acks = []
        self.connected = False
        self.authorized = True
        self.dialogs_calls = 0
        self.save_session_calls = 0
        self.reactions = []
        self.channel_can_send = True  # flip to False to simulate a read-only channel

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_authorized(self):
        return self.authorized

    def save_session(self):
        self.save_session_calls += 1

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        # title contains markup-hostile brackets on purpose
        dms = [
            Dialog(id=7, title="Ann [/x", username="ann", unread=0, is_contact=True),
            Dialog(id=8, title="Stranger", username="stranger", unread=2, is_contact=False),
        ]
        if dm_only:
            return dms
        # повторяет контракт core: dm_only=False — все диалоги с kind и marked id
        return dms + [
            Dialog(id=-100200, title="Devs", kind="group", unread=1),
            Dialog(id=-100300, title="News", kind="channel", can_send=self.channel_can_send),
            Dialog(id=9, title="HelperBot", kind="bot"),
        ]

    async def archived_dialogs(self):
        return [
            Dialog(id=10, title="Archived Ann", username="oldann", is_contact=True, archived=True),
            Dialog(id=-100400, title="Archived Channel", kind="channel", archived=True),
        ]

    async def group_dialogs(self):
        return [d for d in await self.dialogs(dm_only=False) if d.kind != "dm"]

    async def history(self, peer, limit=50, offset_id=0):
        return [Message(id=1, dialog_id=peer, sender_id=peer, out=False,
                        text="oops [/bad] [red",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append((peer, text, reply_to))
        self.sent_event.set()
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def wait_sent_count(self, count=1, timeout=2.0):
        while len(self.sent) < count:
            self.sent_event.clear()
            if len(self.sent) >= count:
                break
            await asyncio.wait_for(self.sent_event.wait(), timeout=timeout)

    async def send_media(self, peer, file_path, *, caption=None, voice_note=False,
                         video_note=False, force_document=False):
        self.media_sent = (peer, str(file_path), caption)
        return Message(id=4, dialog_id=peer, sender_id=1, out=True, text=caption or "<media>",
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def send_reaction(self, peer, message_id, emoticon):
        self.reactions.append((peer, message_id, emoticon))

    async def mark_read(self, peer, max_id=None):
        self.read_acks.append((peer, max_id))

    async def listen_all(self):
        # idle forever; the worker just waits for events
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def listen_outgoing(self):
        # idle forever; the outgoing worker just waits for events
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def listen_reactions(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover


class TuiSourceStorage:
    async def get_value(self, key):
        if key == "user_lang":
            return "ru"
        return None


class TuiSourceStore:
    def __init__(self):
        self.storage = TuiSourceStorage()
        self.recorded = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def run(self):
        await asyncio.Event().wait()

    async def history(self, peer, limit=50):
        return [Message(id=1, dialog_id=peer, sender_id=peer, out=False,
                        text="history", date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def record_outgoing(self, dialog_id, message, *, source_text, source_lang):
        self.recorded.append((dialog_id, message.text, source_text, source_lang))


class RecordingOutbound:
    def __init__(self, *, target_lang="en", variants=None, fail=False):
        self.target_lang = target_lang
        self.variant_values = variants or ["hello"]
        self.fail = fail
        self.applies_calls = []
        self.variants_calls = []

    async def applies(self, dialog_id, text, *, telegram_lang_code=None):
        self.applies_calls.append((dialog_id, text))
        return self.target_lang

    async def variants(self, dialog_id, text, target_lang):
        self.variants_calls.append((dialog_id, text, target_lang))
        if self.fail:
            raise RuntimeError("llm down")
        return list(self.variant_values)

    async def prepare_variants(self, dialog_id, text, *, telegram_lang_code=None):
        # mirrors OutboundTranslator: one entry point composing applies()+variants()
        target_lang = await self.applies(dialog_id, text, telegram_lang_code=telegram_lang_code)
        if target_lang is None:
            return None, []
        return target_lang, await self.variants(dialog_id, text, target_lang)


class BlockingOutbound(RecordingOutbound):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def variants(self, dialog_id, text, target_lang):
        self.variants_calls.append((dialog_id, text, target_lang))
        await self.release.wait()
        raise RuntimeError("llm down")


def test_real_tui_client_gets_session_encryption_key(monkeypatch, tmp_path):
    from tg_messenger.tui import app as tui_app

    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", "shared-secret")
    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr("tg_messenger.core.client.StandaloneTelegramClient", FakeStandaloneTelegramClient)

    tui_app._make_real_client("default")

    assert captured["session_name"] == "default"
    assert captured["session_dir"] == str(tmp_path)
    assert captured["encryption_key"] == "shared-secret"


def test_real_tui_client_gets_send_rate(monkeypatch):
    from tg_messenger.tui import app as tui_app

    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SEND_RATE", "20")
    monkeypatch.setattr("tg_messenger.core.client.StandaloneTelegramClient", FakeStandaloneTelegramClient)

    tui_app._make_real_client("default")

    assert captured["send_rate_per_min"] == 20.0


async def test_tui_mounts_and_lists_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert [item.dialog_id for item in app.query(DialogItem)] == [7, 8, -100200, -100300, 9]


async def test_tui_dialog_item_shows_id():
    # цикл 63: DialogItem рендерит "id — title", id виден пользователю
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        item = list(app.query(DialogItem))[0]
        # Static внутри DialogItem рендерит "7 — Ann [/x" литерально
        rendered = str(item.query_one(Static).render())
        assert rendered.startswith("7 — ")


class UnreadClient(TuiStubClient):
    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        return [Dialog(id=7, title="Ann", username="ann", unread=3)]


async def test_tui_dialog_item_shows_unread_count():
    # цикл 81: непрочитанные показываются как "(N)" в строке диалога
    app = MessengerTUI(client=UnreadClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        item = list(app.query(DialogItem))[0]
        rendered = str(item.query_one(Static).render())
        assert "(3)" in rendered


async def test_tui_no_unread_marker_when_zero():
    app = MessengerTUI(client=TuiStubClient())  # Ann has unread=0
    async with app.run_test() as pilot:
        await pilot.pause()
        item = list(app.query(DialogItem))[0]
        rendered = str(item.query_one(Static).render())
        assert "(" not in rendered.split("—", 1)[1]  # no "(N)" badge after the title


async def test_tui_selecting_dialog_marks_read():
    # цикл 81: открытие диалога помечает прочитанным (через worker, best-effort)
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        item = list(app.query(DialogItem))[0]
        await app.on_list_view_selected(ListView.Selected(lv, item, 0))
        await pilot.pause()
        await pilot.pause()
    assert stub.read_acks == [(7, 1)]


async def test_tui_history_shows_visible_message_id():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        assert any(str(b.render()).startswith("[1] ") for b in bubbles)


# --- read-only chat gating (capability) ---


async def test_tui_readonly_channel_disables_composer():
    stub = TuiStubClient()
    stub.channel_can_send = False
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        # the read-only channel item (-100300)
        item = next(i for i in app.query(DialogItem) if i.dialog_id == -100300)
        await app.on_list_view_selected(ListView.Selected(lv, item, 0))
        await pilot.pause()
        composer = app.query_one("#composer", Input)
        assert composer.disabled is True
        assert composer.placeholder == "Только чтение"


async def test_tui_writable_dialog_enables_composer():
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        item = next(i for i in app.query(DialogItem) if i.dialog_id == 7)  # a DM
        await app.on_list_view_selected(ListView.Selected(lv, item, 0))
        await pilot.pause()
        composer = app.query_one("#composer", Input)
        assert composer.disabled is False
        assert composer.placeholder == "Message…"


async def test_tui_submit_in_readonly_channel_does_not_send():
    stub = TuiStubClient()
    stub.channel_can_send = False
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100300  # a read-only channel
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, "hello"))
        await pilot.pause()
        assert stub.sent == []  # the guard refused before any send worker


async def test_tui_react_in_readonly_channel_sends():
    # #93/#86: reactions are NOT gated by posting permission — the "r" hotkey must go
    # through in a read-only channel even though the text composer is disabled.
    stub = TuiStubClient()
    stub.channel_can_send = False
    app = MessengerTUI(client=stub)

    async def pick(screen):
        assert isinstance(screen, EmojiPickerScreen)
        return "👍"

    app.push_screen_wait = pick  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100300  # a read-only channel
        await app._show_history(-100300)  # mounts a bubble with message_id=1
        await pilot.pause()
        bubble = list(app.query(MessageBubble))[0]
        bubble.focus()
        await pilot.press("r")
        await _pause_until(pilot, lambda: bool(stub.reactions))
        assert stub.reactions == [(-100300, 1, "👍")]  # the reaction went out
        assert stub.sent == []  # ...and no text was sent


async def test_tui_send_forbidden_restores_draft():
    # Regression: composer is enabled (can_send=True / stale), but Telegram rejects the
    # write on rights at send time. on_input_submitted clears the composer optimistically
    # BEFORE the send; the SendForbiddenError handler must restore the typed text — like
    # the generic failure path — instead of silently dropping it.
    stub = TuiStubClient()

    async def forbidden(peer, text):
        raise SendForbiddenError("ChatWriteForbiddenError")

    stub.send_text = forbidden
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7  # a writable DM — composer is enabled
        composer = app.query_one("#composer", Input)
        # reproduce the optimistic clear done by on_input_submitted before the worker runs
        composer.value = ""
        app._compose_state_for(7).draft = ""
        await app._send_text(7, "hello")  # the rejected send path
        await pilot.pause()
        assert stub.sent == []  # nothing went out
        assert app._compose_state_for(7).draft == "hello"  # draft restored
        assert composer.value == "hello"  # typed text back in the composer


async def test_tui_send_forbidden_notifies_raw_text():
    # #92: the notify shows Telegram's specific reason, not the fixed read-only line.
    stub = TuiStubClient()

    async def forbidden(peer, text):
        raise SendForbiddenError("A premium account is required to execute this action")

    stub.send_text = forbidden
    app = MessengerTUI(client=stub)
    notifications = []
    app.notify = lambda message, **kw: notifications.append((message, kw))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._send_text(7, "hi")
        await pilot.pause()
    assert any("premium account is required" in m for m, _ in notifications)


async def test_tui_send_media_forbidden_restores_command(tmp_path):
    # Same regression as text, but for the @file media path: on_input_submitted clears the
    # composer before _send_media; a rights rejection must restore the original command.
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"x")
    stub = TuiStubClient()

    async def forbidden(peer, file_path, **kwargs):
        raise SendForbiddenError("ChatSendMediaForbiddenError")

    stub.send_media = forbidden
    app = MessengerTUI(client=stub)
    command = f'@"{media}" my caption'
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7  # a writable DM — composer is enabled
        composer = app.query_one("#composer", Input)
        # reproduce the optimistic clear done by on_input_submitted before the worker runs
        composer.value = ""
        app._compose_state_for(7).draft = ""
        await app._send_media(7, str(media), "my caption", source_text=command)
        await pilot.pause()
        assert not hasattr(stub, "media_sent")  # nothing went out
        assert app._compose_state_for(7).draft == command  # command restored
        assert composer.value == command  # typed command back in the composer


class LongHistoryClient(TuiStubClient):
    async def history(self, peer, limit=50, offset_id=0):
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return [
            Message(id=i, dialog_id=peer, sender_id=peer, out=False, text=f"msg {i}", date=date)
            for i in range(1, 80)
        ]


async def test_tui_history_scrolls_to_newest_message():
    app = MessengerTUI(client=LongHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        await app._show_history(7)
        pane = app.query_one("#messages")
        for _ in range(6):
            await pilot.pause()
            if pane.max_scroll_y > 0 and pane.scroll_y == pane.max_scroll_y:
                break
        assert pane.max_scroll_y > 0
        assert pane.scroll_y == pane.max_scroll_y


async def test_tui_scroll_helper_supports_textual_060_signature(monkeypatch):
    calls = []

    def scroll_end_without_immediate(
        self,
        *,
        animate=True,
        speed=None,
        duration=None,
        easing=None,
        force=False,
        on_complete=None,
        level="basic",
    ):
        calls.append(animate)

    monkeypatch.setattr(Vertical, "scroll_end", scroll_end_without_immediate)

    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._show_history(7)
        await pilot.pause()

    assert calls


class LongMessageClient(TuiStubClient):
    async def history(self, peer, limit=50, offset_id=0):
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        text = "long-message-" + "x" * 400
        return [Message(id=1, dialog_id=peer, sender_id=1, out=True, text=text, date=date)]


async def test_tui_long_message_bubble_stays_within_message_pane():
    app = MessengerTUI(client=LongMessageClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        await app._show_history(7)
        await pilot.pause()
        pane = app.query_one("#messages")
        bubble = list(app.query(MessageBubble))[0]
        assert bubble.size.width <= pane.size.width
        assert "long-message-" in str(bubble.render())


async def test_tui_survives_markup_hostile_text():
    # dialog titles and message text with [brackets] must render literally,
    # not be parsed as Textual markup (which raises MarkupError)
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) == 1


# --- циклы 133-134: TUI-экран логина (телефон→код→2FA) ---


class FakeTuiLoginSession:
    """LoginSession stand-in for the TUI login screen."""

    def __init__(self, *, needs_2fa=False, wrong_code=False):
        from tg_messenger.core.auth import CodeDelivery

        self.state = "phone"
        self.phones = []
        self.codes = []
        self.passwords = []
        self._needs_2fa = needs_2fa
        self._wrong_code = wrong_code
        self._delivery = CodeDelivery(kind="app", next_kind="sms")

    async def submit_phone(self, phone):
        self.phones.append(phone)
        self.state = "code"
        return self._delivery

    async def submit_code(self, code):
        from tg_messenger.core.auth import LoginError

        self.codes.append(code)
        if self._wrong_code:
            raise LoginError("Wrong code — try again.")
        if self._needs_2fa:
            self.state = "password"
            return
        self.state = "done"

    async def submit_password(self, password):
        self.passwords.append(password)
        self.state = "done"

    async def resend(self):
        return self._delivery


async def test_tui_shows_login_screen_when_not_logged_in():
    from tg_messenger.tui.app import LoginScreen

    stub = TuiStubClient()
    stub.authorized = False
    sess = FakeTuiLoginSession()
    app = MessengerTUI(client=stub, login_session=sess)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert app.return_code is None  # not exited — login screen is shown instead


async def test_tui_login_phone_then_code_loads_dialogs():
    stub = TuiStubClient()
    stub.authorized = False
    sess = FakeTuiLoginSession()
    app = MessengerTUI(client=stub, login_session=sess)
    async with app.run_test() as pilot:
        await pilot.pause()
        # phone step
        app.screen.query_one("#login-input", Input).value = "+10000000000"
        await pilot.press("enter")
        await pilot.pause()
        # code step
        app.screen.query_one("#login-input", Input).value = "12345"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert sess.phones == ["+10000000000"]
        assert sess.codes == ["12345"]
        # back on the main screen with dialogs loaded
        assert stub.dialogs_calls >= 1
        assert len(list(app.query(DialogItem))) >= 1
        assert stub.save_session_calls == 1


async def test_tui_login_2fa_branch():
    stub = TuiStubClient()
    stub.authorized = False
    sess = FakeTuiLoginSession(needs_2fa=True)
    app = MessengerTUI(client=stub, login_session=sess)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#login-input", Input).value = "+10000000000"
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#login-input", Input).value = "12345"
        await pilot.press("enter")
        await pilot.pause()
        # now on the password step
        app.screen.query_one("#login-input", Input).value = "hunter2"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert sess.passwords == ["hunter2"]
        assert stub.dialogs_calls >= 1
        assert stub.save_session_calls == 1


async def test_tui_login_wrong_code_notifies_and_stays(caplog):
    from tg_messenger.tui.app import LoginScreen

    stub = TuiStubClient()
    stub.authorized = False
    sess = FakeTuiLoginSession(wrong_code=True)
    app = MessengerTUI(client=stub, login_session=sess)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#login-input", Input).value = "+10000000000"
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#login-input", Input).value = "000"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        # still on the login screen, input cleared, app alive
        assert isinstance(app.screen, LoginScreen)
        assert app.screen.query_one("#login-input", Input).value == ""
        assert app.return_code is None
        assert stub.save_session_calls == 0


async def test_tui_login_ctrl_c_quits_cleanly():
    stub = TuiStubClient()
    stub.authorized = False
    sess = FakeTuiLoginSession()
    app = MessengerTUI(client=stub, login_session=sess)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#login-input", Input).value = "+10000000000"
        await pilot.press("enter")
        await pilot.pause()
        # Ctrl+C on the code step exits cleanly
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert app.return_code == 0
    assert stub.save_session_calls == 0


async def test_tui_startup_failure_exits_with_code_and_log(caplog):
    stub = TuiStubClient()

    async def boom(dm_only=True):
        raise RuntimeError("startup blew up")

    stub.dialogs = boom
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
    assert app.return_code == 1
    assert stub.connected is False
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_tui_send_failure_notifies_instead_of_crashing(caplog):
    stub = TuiStubClient()

    async def boom(peer, text):
        raise RuntimeError("send blew up")

    stub.send_text = boom
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            app._current = 7
            composer = app.query_one("#composer", Input)
            await app.on_input_submitted(Input.Submitted(composer, "hi"))
            await pilot.pause()
            assert app.return_code is None  # still alive
            assert list(app.query(MessageBubble)) == []  # nothing mounted
            assert composer.value == "hi"  # draft is given back, not lost
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_tui_react_hotkey_sends_reaction():
    # #93: focus a message bubble, press "r", pick an emoji → the reaction is sent.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)

    async def pick(screen):
        return "👍"

    app.push_screen_wait = pick  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # the stub history yields a Message(id=1)
        await pilot.pause()
        bubble = list(app.query(MessageBubble))[0]
        assert bubble.message_id == 1
        bubble.focus()
        await pilot.press("r")
        await _pause_until(pilot, lambda: bool(stub.reactions))
    assert stub.reactions == [(7, 1, "👍")]


async def test_tui_react_targets_bubble_dialog_not_current():
    # #102: a reaction targets the bubble's OWN source dialog (web #96 parity), not the
    # globally-current dialog — even if _current has since moved to another chat.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)

    async def pick(screen):
        return "👍"

    app.push_screen_wait = pick  # type: ignore[method-assign]
    notifications: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, **kw: notifications.append(message)  # type: ignore[method-assign]
        app._current = 7
        await app._show_history(7)  # bubble gets dialog_id=7
        await pilot.pause()
        bubble = list(app.query(MessageBubble))[0]
        assert bubble.dialog_id == 7 and bubble.message_id == 1
        app._current = -100300  # navigate away — the global current is now a DIFFERENT dialog
        bubble.focus()
        await pilot.press("r")
        await _pause_until(pilot, lambda: bool(stub.reactions))
    assert stub.reactions == [(7, 1, "👍")]  # reaction went to the bubble's dialog, not -100300
    # #105: cross-dialog reaction confirms via a toast (the in-pane echo is suppressed since
    # peer != _current), with the source dialog's title — parity with web #103/#97.
    assert notifications == ["Реакция в Ann [/x 👍"]


async def test_tui_react_picker_cancel_sends_nothing():
    # #93: dismissing the picker (Escape → None) sends no reaction.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)

    async def cancel(screen):
        return None

    app.push_screen_wait = cancel  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        bubble = list(app.query(MessageBubble))[0]
        bubble.focus()
        await pilot.press("r")
        await pilot.pause()
    assert stub.reactions == []


async def test_tui_react_hotkey_on_non_target_bubble_is_noop():
    # #93: a bubble with message_id=None is not a reaction target — "r" must not open the picker.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    picked = []

    async def pick(screen):
        picked.append(True)
        return "👍"

    app.push_screen_wait = pick  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        pane = app.query_one("#messages", Vertical)
        bubble = MessageBubble("system notice", out=False, message_id=None, dialog_id=7)
        await pane.mount(bubble)
        await pilot.pause()
        assert bubble.message_id is None
        bubble.focus()
        await pilot.press("r")
        await pilot.pause()
    assert picked == []  # the picker never opened


async def test_emoji_picker_lists_presets():
    # #93: the picker offers exactly the 4 web-parity presets.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(EmojiPickerScreen())
        await pilot.pause()
        items = list(app.screen.query(VariantItem))
        assert [it.value for it in items] == REACTION_PRESETS == ["👍", "❤️", "🔥", "😂"]


async def test_tui_optimistic_clear_and_restore_draft_units():
    # #89: pin the centralized helpers directly. _optimistic_clear wipes draft + all
    # pending-outbound fields + the composer; _restore_draft puts text back only into an
    # EMPTY composer (non-clobber guard) while always updating the stored draft; None is a
    # no-op; a non-current dialog updates state but never touches the composer.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        composer = app.query_one("#composer", Input)

        # seed a draft + pending outbound, then optimistically clear
        state = app._compose_state_for(7)
        state.draft = "hi"
        state.source_text = "orig"
        state.outbound_token = "tok"
        state.original_confirm_text = "orig"
        composer.value = "hi"
        app._optimistic_clear(7, composer)
        assert state.draft == ""
        assert state.source_text is None
        assert state.outbound_token is None
        assert state.original_confirm_text is None
        assert composer.value == ""

        # restore into an empty composer
        app._restore_draft(7, "hi")
        assert app._compose_state_for(7).draft == "hi"
        assert composer.value == "hi"

        # non-clobber: a draft typed meanwhile is preserved, but state.draft still updates
        composer.value = "typed meanwhile"
        app._restore_draft(7, "hi")
        assert composer.value == "typed meanwhile"  # composer untouched
        assert app._compose_state_for(7).draft == "hi"  # state still set

        # None is a no-op (media with no captured command)
        app._compose_state_for(7).draft = "keep"
        app._restore_draft(7, None)
        assert app._compose_state_for(7).draft == "keep"

        # restore to a non-current dialog: state set, composer untouched
        composer.value = "current"
        app._restore_draft(99, "other")
        assert app._compose_state_for(99).draft == "other"
        assert composer.value == "current"


async def test_tui_arrow_keys_move_focus_between_bubbles():
    # #93: up/down move the selection between message bubbles; clamp at the ends.
    app = MessengerTUI(client=LongHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) >= 2
        bubbles[0].focus()
        await pilot.pause()
        assert app.focused is bubbles[0]
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is bubbles[1]
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is bubbles[0]
        await pilot.press("up")  # at the top edge: clamp, stay put
        await pilot.pause()
        assert app.focused is bubbles[0]


async def test_tui_listener_failure_logged_app_stays_alive(caplog):
    stub = TuiStubClient()

    async def broken_listen():
        raise RuntimeError("listener blew up")
        yield  # pragma: no cover

    stub.listen_all = broken_listen
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.return_code is None  # worker died, app did not
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


class EagerSensitiveClient(TuiStubClient):
    """Mimics Telethon's MTProtoSender startup race.

    ``connect()`` spawns a pump task via ``loop.create_task`` and sets the
    running flag only AFTER — exactly like telethon sets ``_user_connected``
    after starting ``_send_loop``. Under ``asyncio.eager_task_factory`` (which
    Textual's real ``App.run()`` installs on py3.12+) the pump body runs at
    creation time, sees the flag still False and dies — every later request
    then waits forever, which is the "TUI connects but never loads" bug.
    """

    def __init__(self):
        super().__init__()
        self._running = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def _pump(self):
        while self._running:
            fut = await self._queue.get()
            fut.set_result(None)

    async def connect(self):
        self._pump_task = asyncio.get_running_loop().create_task(self._pump())
        self._running = True  # after create_task, like mtprotosender.py:134
        await super().connect()

    async def dialogs(self, dm_only=True):
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put(fut)
        await fut  # never resolves if the pump died at creation
        return await super().dialogs(dm_only=dm_only)


def hangs_forever(entered: asyncio.Event):
    """Stub coroutine factory: signals entry, then never returns."""

    async def hung(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()  # never resolves

    return hung


@pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"),
    reason="eager_task_factory is py3.12+; the regression it guards can't occur on 3.11",
)
async def test_tui_loads_dialogs_under_eager_task_factory():
    # the real App.run() installs eager_task_factory on the loop; run_test()
    # does not, which is why this regression was invisible to every other test
    loop = asyncio.get_running_loop()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        app = MessengerTUI(client=EagerSensitiveClient())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert [item.dialog_id for item in app.query(DialogItem)] == [7, 8, -100200, -100300, 9]
    finally:
        loop.set_task_factory(None)


async def test_tui_history_load_does_not_block_ui():
    stub = TuiStubClient()
    history_entered = asyncio.Event()
    stub.history = hangs_forever(history_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        item = list(app.query(DialogItem))[0]
        # the handler must return immediately, not await the network
        await asyncio.wait_for(app.on_list_view_selected(ListView.Selected(lv, item, 0)), 5)
        await asyncio.wait_for(history_entered.wait(), 5)
        await pilot.press("ctrl+c")  # quit works while history hangs
    assert app.return_code == 0


async def test_tui_history_failure_notifies_instead_of_crashing(caplog):
    stub = TuiStubClient()

    async def boom(peer, limit=50, offset_id=0):
        raise RuntimeError("history blew up")

    stub.history = boom
    app = MessengerTUI(client=stub)
    with caplog.at_level("ERROR", logger="tg_messenger.tui.app"):
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._show_history(7)
            await pilot.pause()
            assert app.return_code is None  # still alive
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


async def test_tui_send_does_not_block_ui():
    stub = TuiStubClient()
    send_entered = asyncio.Event()
    stub.send_text = hangs_forever(send_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        composer = app.query_one("#composer", Input)
        await asyncio.wait_for(app.on_input_submitted(Input.Submitted(composer, "hi")), 5)
        await asyncio.wait_for(send_entered.wait(), 5)
        assert composer.value == ""  # cleared optimistically while sending
        await pilot.press("ctrl+c")  # quit works while send hangs
    assert app.return_code == 0


async def test_tui_stays_responsive_and_quits_while_startup_hangs():
    # a hung network must not freeze the UI: the screen renders, keys are
    # processed, and ctrl+c / ctrl+q quit even before startup completes
    stub = TuiStubClient()
    connect_entered = asyncio.Event()
    stub.connect = hangs_forever(connect_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await asyncio.wait_for(connect_entered.wait(), 5)
        await pilot.pause()
        assert stub.dialogs_calls == 0  # still stuck in connect…
        await pilot.press("ctrl+c")  # …yet quitting must work
    assert app.return_code == 0  # clean quit, not a crash


async def test_tui_shows_loading_until_dialogs_arrive():
    stub = TuiStubClient()
    gate = asyncio.Event()
    real_dialogs = stub.dialogs

    async def gated_dialogs(dm_only=True):
        await gate.wait()
        return await real_dialogs(dm_only=dm_only)

    stub.dialogs = gated_dialogs
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#dialogs", ListView).loading is True
        gate.set()
        await pilot.pause()
        assert app.query_one("#dialogs", ListView).loading is False
        assert [item.dialog_id for item in app.query(DialogItem)] == [7, 8, -100200, -100300, 9]


# --- цикл 66: локальный поиск диалогов в TUI ---


async def test_tui_search_filters_dialogs():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert {item.dialog_id for item in app.query(DialogItem)} == {7, 8, -100200}
        search = app.query_one("#search", Input)
        search.value = "Bob"
        await app.on_input_changed(Input.Changed(search, "Bob"))
        await pilot.pause()
        # только Bob (id=8) остаётся видимым
        assert [item.dialog_id for item in app.query(DialogItem)] == [8]


async def test_tui_search_clear_restores_full_list():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "Bob"
        await app.on_input_changed(Input.Changed(search, "Bob"))
        await pilot.pause()
        search.value = ""
        await app.on_input_changed(Input.Changed(search, ""))
        await pilot.pause()
        assert {item.dialog_id for item in app.query(DialogItem)} == {7, 8, -100200}


async def test_tui_search_does_not_hit_network():
    stub = TwoDmClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls_before = stub.dialogs_calls
        search = app.query_one("#search", Input)
        search.value = "Bob"
        await app.on_input_changed(Input.Changed(search, "Bob"))
        await pilot.pause()
        # фильтрация локальная — поверх уже загруженного списка, без запроса
        assert stub.dialogs_calls == calls_before


# --- Цикл 36: вкладки Все / Контакты / Не контакты / Группы / Каналы / Боты / Непрочитанные / Архив ---


def _listed_ids(app):
    return [item.dialog_id for item in app.query(DialogItem)]


async def test_tui_has_all_tab_active_by_default():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        assert tabs.active == "all"
        assert [tab.label.plain for tab in tabs.query("Tab")] == [
            "Все",
            "Контакты",
            "Не контакты",
            "Группы/супер",
            "Каналы",
            "Боты",
            "Непрочитанные",
            "Архив",
        ]
        assert _listed_ids(app) == [7, 8, -100200, -100300, 9]


async def test_tui_contacts_tab_lists_contact_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "contacts"
        await pilot.pause()
        assert _listed_ids(app) == [7]


async def test_tui_non_contacts_tab_lists_non_contact_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "non_contacts"
        await pilot.pause()
        assert _listed_ids(app) == [8]


async def test_tui_groups_tab_lists_groups_only():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "groups"
        await pilot.pause()
        assert _listed_ids(app) == [-100200]


async def test_tui_channels_tab_lists_channel_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "channels"
        await pilot.pause()
        assert _listed_ids(app) == [-100300]


async def test_tui_bots_tab_lists_bot_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "bots"
        await pilot.pause()
        assert _listed_ids(app) == [9]


async def test_tui_unread_tab_lists_unread_non_archived_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "unread"
        await pilot.pause()
        assert _listed_ids(app) == [8, -100200]


async def test_tui_archive_tab_lists_archived_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "archive"
        await pilot.pause()
        assert _listed_ids(app) == [10, -100400]


async def test_tui_tab_switch_back_reloads_all():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        tabs.active = "groups"
        await pilot.pause()
        tabs.active = "all"
        await pilot.pause()
        assert _listed_ids(app) == [7, 8, -100200, -100300, 9]  # список перезагружен, не накоплен


async def test_tui_tab_activation_before_startup_is_safe():
    # переключение вкладки, пока connect ещё висит, не должно дёргать сеть
    stub = TuiStubClient()
    connect_entered = asyncio.Event()
    stub.connect = hangs_forever(connect_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await asyncio.wait_for(connect_entered.wait(), 5)
        await pilot.pause()
        app.query_one(Tabs).active = "groups"
        await pilot.pause()
        assert stub.dialogs_calls == 0  # клиент ещё не готов — запроса не было
        assert app.return_code is None  # и приложение живо
        await pilot.press("ctrl+c")
    assert app.return_code == 0


# --- Цикл 37: live-входящие из групп ---


class GroupEventClient(TuiStubClient):
    """listen_all, который по сигналу отдаёт два события: DM и групповое."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_all(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield IncomingEvent(dialog_id=7, message=Message(
            id=20, dialog_id=7, sender_id=7, out=False, text="из ЛС", date=date))
        yield IncomingEvent(dialog_id=-100200, message=Message(
            id=21, dialog_id=-100200, sender_id=9, out=False, text="из группы", date=date))
        await asyncio.Event().wait()  # idle forever


async def test_tui_group_incoming_appends_bubble_for_open_group_only():
    stub = GroupEventClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100200  # открыта группа
        stub.fire.set()
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        # ЛС-событие не дорисовано (чужой диалог), групповое — да.
        # #108: в группе у входящего сверху строка автора (sender=None → голый userid).
        assert [str(b.render()) for b in bubbles] == ["9\n[21] из группы"]


class GroupSenderEventClient(TuiStubClient):
    """Групповое событие с полным sender (имя/фамилия/username)."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_all(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield IncomingEvent(dialog_id=-100200, message=Message(
            id=21, dialog_id=-100200, sender_id=9, out=False, text="привет",
            date=date, sender=User(id=9, username="bob", first_name="Bob", last_name="Lee")))
        await asyncio.Event().wait()


async def test_tui_group_incoming_shows_full_author_line():
    # #108: userid @username First Last above the text in a group.
    stub = GroupSenderEventClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100200
        stub.fire.set()
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    assert [str(b.render()) for b in bubbles] == ["9 @bob Bob Lee\n[21] привет"]


async def test_tui_dm_incoming_has_no_author_line():
    # #108: in a DM the author is obvious — no author line even for incoming.
    stub = GroupSenderEventClient()  # reuse, but open a DM dialog instead
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7  # DM
        # fire a DM event by reusing _show_history (stub history returns a DM message id=1)
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    assert all("\n[" not in str(b.render()) for b in bubbles)  # no author line prefix
    assert any(str(b.render()).startswith("[1] ") for b in bubbles)


class IncomingDialogListClient(TuiStubClient):
    """listen_all emits one new DM after the initial dialog snapshot was rendered."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        dms = [
            Dialog(id=7, title="Ann", username="ann", unread=0),
            Dialog(id=8, title="Bob", username="bob", unread=0),
        ]
        if dm_only:
            return dms
        return dms + [Dialog(id=-100200, title="Devs", kind="group")]

    async def listen_all(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield IncomingEvent(
            dialog_id=8,
            message=Message(id=22, dialog_id=8, sender_id=8, out=False, text="fresh", date=date),
        )
        await asyncio.Event().wait()


class IncomingAnnDialogListClient(IncomingDialogListClient):
    async def listen_all(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield IncomingEvent(
            dialog_id=7,
            message=Message(id=23, dialog_id=7, sender_id=7, out=False, text="fresh", date=date),
        )
        await asyncio.Event().wait()


async def test_tui_incoming_updates_dialog_list_without_network_reload():
    stub = IncomingDialogListClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls_before = stub.dialogs_calls
        stub.fire.set()
        await pilot.pause()
        rendered = [str(item.query_one(Static).render()) for item in app.query(DialogItem)]
    assert rendered == ["8 — Bob (1)", "7 — Ann", "-100200 — Devs"]
    assert stub.dialogs_calls == calls_before


async def test_tui_incoming_sidebar_refresh_preserves_selected_dialog():
    stub = IncomingAnnDialogListClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.index = 1
        app._current = 8
        assert isinstance(lv.highlighted_child, DialogItem)
        assert lv.highlighted_child.dialog_id == 8

        stub.fire.set()
        await pilot.pause()
        await pilot.pause()

        assert isinstance(lv.highlighted_child, DialogItem)
        assert lv.highlighted_child.dialog_id == 8


async def test_tui_open_dialog_live_message_stays_read_and_marks_new_id():
    stub = IncomingDialogListClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 8
        stub.fire.set()
        await pilot.pause()
        await pilot.pause()
        rendered = [str(item.query_one(Static).render()) for item in app.query(DialogItem)]
        bubbles = [str(b.render()) for b in app.query(MessageBubble)]
    assert rendered == ["8 — Bob", "7 — Ann", "-100200 — Devs"]
    assert bubbles == ["[22] fresh"]
    assert stub.read_acks == [(8, 22)]


async def test_tui_live_mark_read_worker_replaces_superseded_calls(monkeypatch):
    stub = IncomingDialogListClient()
    app = MessengerTUI(client=stub)
    worker_calls = []

    def capture_worker(coro, *args, **kwargs):
        worker_calls.append(kwargs)
        coro.close()

    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 8
        monkeypatch.setattr(app, "run_worker", capture_worker)
        stub.fire.set()
        await pilot.pause()
        await pilot.pause()

    assert any(
        call.get("group") == "mark_read" and call.get("exclusive") is True
        for call in worker_calls
    )


class OutgoingEventClient(TuiStubClient):
    """listen_outgoing, который по сигналу отдаёт два своих сообщения с другого устройства."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_outgoing(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield OutgoingEvent(dialog_id=7, message=Message(
            id=30, dialog_id=7, sender_id=1, out=True, text="с телефона", date=date))
        yield OutgoingEvent(dialog_id=-100200, message=Message(
            id=31, dialog_id=-100200, sender_id=1, out=True, text="в другой чат", date=date))
        await asyncio.Event().wait()  # idle forever


async def test_tui_outgoing_from_another_device_appends_out_bubble_for_open_dialog_only():
    stub = OutgoingEventClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7  # открыт диалог 7
        stub.fire.set()
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        # своё сообщение в открытый диалог дорисовано (out=True), в чужой — нет
        assert [str(b.render()) for b in bubbles] == ["[30] с телефона"]
        assert all("out" in b.classes for b in bubbles)


class OutgoingDialogListClient(IncomingDialogListClient):
    async def listen_all(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def listen_outgoing(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield OutgoingEvent(
            dialog_id=8,
            message=Message(id=24, dialog_id=8, sender_id=1, out=True, text="from laptop", date=date),
        )
        await asyncio.Event().wait()


async def test_tui_outgoing_updates_dialog_list_without_unread_increment():
    stub = OutgoingDialogListClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls_before = stub.dialogs_calls
        stub.fire.set()
        await pilot.pause()
        rendered = [str(item.query_one(Static).render()) for item in app.query(DialogItem)]
    assert rendered == ["8 — Bob", "7 — Ann", "-100200 — Devs"]
    assert stub.dialogs_calls == calls_before


async def test_tui_local_send_updates_dialog_list_without_waiting_for_echo():
    stub = TwoDmClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls_before = stub.dialogs_calls
        app._current = 8
        await app._send_text(8, "from composer")
        await pilot.pause()
        rendered = [str(item.query_one(Static).render()) for item in app.query(DialogItem)]
    assert rendered == ["8 — Bob", "7 — Ann", "-100200 — Devs"]
    assert stub.dialogs_calls == calls_before


class OutgoingEchoClient(TuiStubClient):
    """listen_outgoing, отдающий эхо именно того id, что мы только что отправили."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_outgoing(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # id=2 — ровно то, что вернёт send_text стаба (см. TuiStubClient)
        yield OutgoingEvent(dialog_id=7, message=Message(
            id=2, dialog_id=7, sender_id=1, out=True, text="привет", date=date))
        await asyncio.Event().wait()


async def test_tui_own_send_is_not_duplicated_by_outgoing_echo():
    stub = OutgoingEchoClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._send_text(7, "привет")  # оптимистичный пузырёк + запоминание id=2
        stub.fire.set()  # эхо того же id=2 приходит через listen_outgoing()
        await pilot.pause()
        bubbles = [str(b.render()) for b in app.query(MessageBubble)]
        # ровно один пузырёк, эхо не продублировало
        assert bubbles == ["[2] привет"]


class OutgoingSameIdOtherDialogClient(TuiStubClient):
    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_outgoing(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield OutgoingEvent(dialog_id=7, message=Message(
            id=2, dialog_id=7, sender_id=1, out=True, text="same id", date=date))
        await asyncio.Event().wait()


async def test_tui_outgoing_does_not_skip_same_message_id_from_other_dialog():
    stub = OutgoingSameIdOtherDialogClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._send_text(9, "other")  # remembers (dialog=9, id=2), no bubble in dialog 7
        stub.fire.set()  # dialog 7 also has id=2; it must still render
        await pilot.pause()
        bubbles = [str(b.render()) for b in app.query(MessageBubble)]
        assert bubbles == ["[2] same id"]


class ReactionEventClient(TuiStubClient):
    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def history(self, peer, limit=50, offset_id=0):
        # message id 11 exists so the reaction targeting it can attach (id 10 does not)
        return [Message(id=11, dialog_id=peer, sender_id=peer, out=False, text="hi",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def listen_reactions(self):
        await self.fire.wait()
        yield ReactionEvent(dialog_id=9, message_id=10, emoticon="❤️")  # other dialog → ignored
        yield ReactionEvent(dialog_id=7, message_id=11, emoticon=None)  # custom → "<custom>"
        await asyncio.Event().wait()


async def test_tui_reaction_attaches_under_message_for_open_dialog_only():
    # #106: an incoming (other people's) reaction attaches UNDER its target message — no
    # separate bubble — and only for a message in the open dialog's loaded history.
    stub = ReactionEventClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # bubble id=11 enters _bubble_index
        await pilot.pause()
        stub.fire.set()
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        # still exactly ONE bubble (the message) — the reaction did not spawn its own
        assert len(bubbles) == 1
        rendered = str(bubbles[0].render())
        assert rendered.startswith("[11] hi")
        assert rendered.endswith("<custom>")  # custom/premium reaction label, attached under


class SentReactionEchoClient(TuiStubClient):
    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_reactions(self):
        await self.fire.wait()
        yield ReactionEvent(dialog_id=7, message_id=1, emoticon="👍")
        await asyncio.Event().wait()


async def test_tui_sent_reaction_echo_is_not_duplicated():
    # #106: our own optimistic reaction attaches under the message; the live echo for the
    # same (dialog, message, emoji) is deduped (_sent_reactions) so 👍 is shown once.
    stub = SentReactionEchoClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # bubble id=1 (stub history) enters the index
        await pilot.pause()
        await app._send_reaction(7, 1, "👍")  # optimistic attach + remembers sent
        stub.fire.set()  # live echo for (7,1,"👍") — deduped, must not double-attach
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    # one bubble (the message), reaction line shows a single 👍, not 👍 👍
    assert len(bubbles) == 1
    rendered = str(bubbles[0].render())
    assert rendered.count("👍") == 1
    assert rendered.startswith("[1] ")


async def test_tui_reaction_accumulates_distinct_emoji_and_dedups():
    # #106: multiple distinct reactions accumulate on one line; a repeat is not added twice.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # bubble id=1
        await pilot.pause()
        app._apply_reaction(7, 1, "👍")
        app._apply_reaction(7, 1, "❤️")
        app._apply_reaction(7, 1, "👍")  # duplicate — ignored
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    assert len(bubbles) == 1
    rendered = str(bubbles[0].render())
    assert rendered.endswith("👍 ❤️")
    assert rendered.count("👍") == 1


async def test_tui_reaction_and_translation_coexist_either_order():
    # #106: translation and reactions are separate bubble state — neither clobbers the other.
    bubble = MessageBubble("[1] hi", out=False, message_id=1, dialog_id=7)
    bubble.show_translation("привет")
    bubble.add_reaction("👍")
    first = str(bubble.render())
    assert "↳ привет" in first and "👍" in first and "[1] hi" in first

    bubble2 = MessageBubble("[2] yo", out=False, message_id=2, dialog_id=7)
    bubble2.add_reaction("🔥")
    bubble2.show_translation("здарова")  # reverse order
    second = str(bubble2.render())
    assert "↳ здарова" in second and "🔥" in second and "[2] yo" in second


async def test_tui_reaction_for_unknown_message_is_silently_ignored():
    # #106: a reaction whose message isn't in the loaded history attaches nowhere and
    # spawns no bubble — no exception (mirrors the translation no-op).
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # only message id=1 exists
        await pilot.pause()
        app._apply_reaction(7, 999, "👍")  # unknown id
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    assert len(bubbles) == 1  # still just the one message, no reaction bubble
    assert "👍" not in str(bubbles[0].render())


class ChannelReactionClient(TuiStubClient):
    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def history(self, peer, limit=50, offset_id=0):
        # a channel post (marked negative dialog id) — message id 50 in the loaded history
        return [Message(id=50, dialog_id=peer, sender_id=peer, out=False, text="post",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def listen_reactions(self):
        await self.fire.wait()
        yield ReactionEvent(dialog_id=-100300, message_id=50, emoticon="🔥")
        await asyncio.Event().wait()


async def test_tui_reaction_attaches_in_channel():
    # #106: reactions attach under messages in channels too (marked negative dialog id) —
    # not just DMs. Same path for bots/groups since nothing filters by dialog kind.
    stub = ChannelReactionClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100300
        await app._show_history(-100300)  # bubble id=50
        await pilot.pause()
        stub.fire.set()
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    assert len(bubbles) == 1
    assert str(bubbles[0].render()).endswith("🔥")


async def test_tui_reaction_during_history_load_is_buffered_and_replayed():
    # #106 (Codex review): a reaction for the open dialog that arrives while its history is
    # still loading (the bubble doesn't exist yet) must not be dropped — it is buffered and
    # replayed once _show_history mounts the bubbles.
    stub = TuiStubClient()  # history returns message id=1
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        # reaction arrives BEFORE history is loaded: index empty → buffered, not lost
        app._apply_reaction(7, 1, "👍")
        assert app._pending_reactions.get(7) == [(1, "👍")]
        await app._show_history(7)  # mounts bubble id=1, then replays the buffer
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    assert len(bubbles) == 1
    assert str(bubbles[0].render()).endswith("👍")
    assert app._pending_reactions.get(7) is None  # buffer drained, not left dangling


async def test_tui_buffered_reaction_for_other_dialog_is_not_kept():
    # #106: a reaction for a dialog other than the open one is never buffered (it would never
    # be replayed) — it is silently ignored, like an out-of-snapshot message.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        app._apply_reaction(9, 1, "👍")  # different dialog
        assert app._pending_reactions == {}


async def test_tui_reaction_not_attached_to_same_id_bubble_of_other_dialog():
    # #106 (Codex review, defense-in-depth): _bubble_index is keyed by bare message_id, which
    # is not unique across dialogs. If a stale bubble from a DIFFERENT dialog somehow sits in the
    # index under a colliding id, a reaction for the current dialog must NOT attach to it — the
    # bubble's own source dialog is verified before attaching. (The synchronous index clear +
    # exclusive history worker make this unreachable in practice; this guards the invariant.)
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        pane = app.query_one("#messages", Vertical)
        # a bubble whose SOURCE dialog is 9 (not the open dialog 7), indexed under id 1
        stale = MessageBubble("[1] from dialog 9", out=False, message_id=1, dialog_id=9)
        await pane.mount(stale)
        app._bubble_index[1] = stale
        app._apply_reaction(7, 1, "👍")  # current dialog 7, colliding id 1
        await pilot.pause()
        rendered = str(stale.render())
    assert "👍" not in rendered  # the reaction did not land under the other dialog's bubble
    assert app._pending_reactions == {}  # nor was it buffered (the bubble existed, just mismatched)


async def test_tui_group_incoming_does_not_trigger_suggester():
    class RecordingSuggester:
        def __init__(self):
            self.calls = []

        async def suggest(self, dialog_id):
            self.calls.append(dialog_id)
            return "draft"

    stub = GroupEventClient()
    suggester = RecordingSuggester()
    app = MessengerTUI(client=stub, suggester=suggester)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100200  # open group, but suggestion must stay DM-only
        stub.fire.set()
        await pilot.pause()
    assert suggester.calls == []


async def test_tui_disconnects_on_exit():
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert stub.connected is True
    assert stub.connected is False


async def test_tui_closes_suggester_on_exit():
    class ClosableSuggester:
        def __init__(self):
            self.closed = 0

        async def close(self):
            self.closed += 1

    suggester = ClosableSuggester()
    app = MessengerTUI(client=TuiStubClient(), suggester=suggester)
    async with app.run_test() as pilot:
        await pilot.pause()
    assert suggester.closed == 1


async def test_tui_switching_dialogs_clears_pending_suggestion():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._pending_suggestion = "draft for Ann"
        app.query_one("#suggestion", Static).update("Suggest: draft for Ann")
        lv = app.query_one("#dialogs", ListView)
        lv.index = 1
        lv.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert app._pending_suggestion is None
        assert str(app.query_one("#suggestion", Static).render()) == ""


# --- UX: Enter / стрелка-вниз с вкладок → фокус на список диалогов ---


async def test_down_arrow_on_tabs_moves_focus_to_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        tabs.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


async def test_enter_on_tabs_moves_focus_to_dialogs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(Tabs)
        tabs.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


async def test_down_focuses_first_dialog_so_it_is_navigable():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        assert lv.index == 0  # списком сразу можно листать


# --- UX: стрелка-вверх на первом диалоге → обратно на вкладки DM/Группы ---


async def test_up_on_first_dialog_returns_focus_to_tabs():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0  # первый элемент
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is app.query_one(Tabs)


async def test_up_on_non_first_dialog_scrolls_list_not_tabs():
    """Со второго диалога ↑ листает список вверх, фокус остаётся на списке."""
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 1  # второй элемент
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is lv  # фокус не ушёл на вкладки
        assert lv.index == 0  # поднялись на первый


class TwoDmClient(TuiStubClient):
    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        dms = [
            Dialog(id=7, title="Ann", username="ann", unread=0, is_contact=True),
            Dialog(id=8, title="Bob", username="bob", unread=0, is_contact=False),
        ]
        if dm_only:
            return dms
        return dms + [Dialog(id=-100200, title="Devs", kind="group")]


async def _pause_until(pilot, predicate, attempts=20):
    for _ in range(attempts):
        await pilot.pause()
        if predicate():
            return
    assert predicate()


async def _select_dialog(pilot, app, dialog_id: int):
    lv = app.query_one("#dialogs", ListView)
    for idx, item in enumerate(app.query(DialogItem)):
        if item.dialog_id == dialog_id:
            lv.index = idx
            lv.focus()
            await pilot.press("enter")
            await pilot.pause()
            return
    raise AssertionError(f"dialog {dialog_id} not found")


async def test_tui_composer_drafts_are_scoped_to_dialog():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)

        await _select_dialog(pilot, app, 7)
        composer.value = "draft A"
        await _select_dialog(pilot, app, 8)
        await pilot.pause()
        assert composer.value == ""

        composer.value = "draft B"
        await _select_dialog(pilot, app, 7)
        await pilot.pause()
        assert composer.value == "draft A"

        await _select_dialog(pilot, app, 8)
        await pilot.pause()
        assert composer.value == "draft B"


async def test_tui_ignores_stale_composer_changed_event():
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)

        await _select_dialog(pilot, app, 7)
        composer.value = "current"
        state = app._compose_state_for(7)
        state.draft = "current"

        await app.on_input_changed(Input.Changed(composer, "stale"))

    assert state.draft == "current"


async def test_tui_outbound_variant_state_is_scoped_to_dialog():
    stub = TwoDmClient()
    store = TuiSourceStore()
    outbound = RecordingOutbound(variants=["hello"])
    app = MessengerTUI(client=stub, store=store, outbound=outbound)

    async def pick_variant(screen):
        return "hello"

    app.push_screen_wait = pick_variant  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)

        await _select_dialog(pilot, app, 7)
        composer.value = "привет"
        await app.on_input_submitted(Input.Submitted(composer, "привет"))
        await _pause_until(pilot, lambda: composer.value == "hello")

        await _select_dialog(pilot, app, 8)
        await pilot.pause()
        assert composer.value == ""
        await app.on_input_submitted(Input.Submitted(composer, composer.value))
        await pilot.pause()
        assert stub.sent == []

        await _select_dialog(pilot, app, 7)
        await pilot.pause()
        assert composer.value == "hello"
        await app.on_input_submitted(Input.Submitted(composer, "hello"))
        await stub.wait_sent_count()

    assert stub.sent == [(7, "hello", None)]
    assert store.recorded == [(7, "hello", "привет", "ru")]


async def test_tui_editing_selected_variant_clears_stale_source_text():
    stub = TwoDmClient()
    store = TuiSourceStore()
    outbound = RecordingOutbound(variants=["hello"])
    app = MessengerTUI(client=stub, store=store, outbound=outbound)

    async def pick_variant(screen):
        return "hello"

    app.push_screen_wait = pick_variant  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)

        await _select_dialog(pilot, app, 7)
        composer.value = "привет"
        await app.on_input_submitted(Input.Submitted(composer, "привет"))
        await _pause_until(pilot, lambda: composer.value == "hello")

        outbound.target_lang = None
        composer.value = "hello!"
        await app.on_input_changed(Input.Changed(composer, "hello!"))
        await app.on_input_submitted(Input.Submitted(composer, "hello!"))
        await stub.wait_sent_count()

    assert stub.sent == [(7, "hello!", None)]
    assert store.recorded == []
    assert outbound.applies_calls == [(7, "привет"), (7, "hello!")]


async def test_tui_outbound_error_original_confirm_is_scoped_to_dialog():
    stub = TwoDmClient()
    outbound = RecordingOutbound(fail=True)
    app = MessengerTUI(client=stub, outbound=outbound)

    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)

        await _select_dialog(pilot, app, 7)
        composer.value = "привет"
        await app.on_input_submitted(Input.Submitted(composer, "привет"))
        await _pause_until(pilot, lambda: outbound.variants_calls)
        assert stub.sent == []
        assert composer.value == "привет"

        await _select_dialog(pilot, app, 8)
        await pilot.pause()
        assert composer.value == ""
        composer.value = "привет"
        await app.on_input_submitted(Input.Submitted(composer, "привет"))
        await pilot.pause()
        assert stub.sent == []

        await _select_dialog(pilot, app, 7)
        await pilot.pause()
        assert composer.value == "привет"
        await app.on_input_submitted(Input.Submitted(composer, "привет"))
        await stub.wait_sent_count()

    assert stub.sent == [(7, "привет", None)]


async def test_tui_outbound_cancel_restores_current_dialog_draft():
    stub = TwoDmClient()
    outbound = RecordingOutbound(variants=["hello"])
    app = MessengerTUI(client=stub, outbound=outbound)

    async def cancel_variant_picker(screen):
        return None

    app.push_screen_wait = cancel_variant_picker  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)

        await _select_dialog(pilot, app, 7)
        composer.value = "привет"
        await app.on_input_submitted(Input.Submitted(composer, "привет"))
        await _pause_until(pilot, lambda: outbound.variants_calls)

        assert composer.value == "привет"
        assert stub.sent == []


def test_tui_outbound_flow_delegates_to_coordinator_not_applies_variants():
    # #73 architecture regression: the TUI flow goes through the coordinator's prepare();
    # it no longer calls applies()/variants() directly, owns no local timeout, and the old
    # _prepare_outbound_variants fallback is gone.
    source = inspect.getsource(MessengerTUI._outbound_flow)
    assert "_coordinator.prepare(" in source
    assert ".applies(" not in source
    assert ".variants(" not in source
    assert "asyncio.wait_for(" not in source  # timeout lives in the coordinator
    assert not hasattr(MessengerTUI, "_prepare_outbound_variants")


async def test_tui_outbound_clears_composer_and_repeated_enter_does_not_restart_worker():
    stub = TwoDmClient()
    outbound = BlockingOutbound()
    app = MessengerTUI(client=stub, outbound=outbound)

    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)

        await _select_dialog(pilot, app, 7)
        composer.value = "привет"
        await app.on_input_submitted(Input.Submitted(composer, "привет"))
        await _pause_until(pilot, lambda: outbound.variants_calls)
        assert composer.value == ""

        await app.on_input_submitted(Input.Submitted(composer, composer.value))
        await pilot.pause()
        assert outbound.applies_calls == [(7, "привет")]
        assert outbound.variants_calls == [(7, "привет", "en")]
        assert stub.sent == []

        outbound.release.set()
        await _pause_until(pilot, lambda: composer.value == "привет")

    assert stub.sent == []


# --- цикл 60: TUI выбор профиля (мультилогин) ---

async def test_tui_profile_screen_picks_and_builds_client():
    captured = {}

    def factory(session_name):
        captured["session_name"] = session_name
        return TuiStubClient()

    app = MessengerTUI(profiles=["alice", "bob"], client_factory=factory)
    async with app.run_test() as pilot:
        # wait for the pushed profile screen to mount (it's a modal — query the screen)
        for _ in range(20):
            await pilot.pause()
            if app.screen.query(ProfileItem):
                break
        assert len(list(app.screen.query(ProfileItem))) == 2
        # select the second profile (alice, bob -> bob)
        lv = app.screen.query_one("#profiles", ListView)
        lv.index = 1
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if captured.get("session_name"):
                break
    assert captured.get("session_name") == "bob"


async def test_tui_single_profile_skips_screen():
    captured = {}

    def factory(session_name):
        captured["session_name"] = session_name
        return TuiStubClient()

    app = MessengerTUI(profiles=["solo"], client_factory=factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert list(app.query(ProfileItem)) == []  # no selection screen
        assert list(app.query(DialogItem))  # went straight to dialogs
    assert captured.get("session_name") == "solo"


# --- #52 point 2: ProfileScreen reachable from the `tui` entrypoint ---
# A deps_factory builds the WHOLE dependency set (client + suggester/store/translator/
# outbound) AFTER the in-app ProfileScreen picks a profile, so the command no longer
# has to resolve the profile via a CLI menu before constructing the TUI.


class _FakeDeps:
    def __init__(self, session_name):
        self.session_name = session_name
        self.client = TuiStubClient()
        self.suggester = object()
        self.store = None  # keep None so _startup's store block is a no-op in tests
        self.translator = object()
        self.outbound = object()


async def test_tui_startup_calls_deps_factory_after_profile_screen():
    calls = []

    def deps_factory(profile):
        calls.append(profile)
        return _FakeDeps(profile)

    app = MessengerTUI(profiles=["alice", "bob"], deps_factory=deps_factory)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause()
            if app.screen.query(ProfileItem):
                break
        lv = app.screen.query_one("#profiles", ListView)
        lv.index = 1  # alice, bob -> bob
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if calls:
                break
    assert calls == ["bob"]
    # every dep slot is populated from the factory result, not left at __init__ defaults
    assert app._session_name == "bob"
    assert isinstance(app._client, TuiStubClient)
    assert app._suggester is not None
    assert app._translator is not None
    assert app._outbound is not None


async def test_tui_startup_deps_factory_none_falls_back_to_client_factory():
    # When no deps_factory is injected (the library path), _startup keeps using
    # client_factory and leaves the other deps as the __init__ values (None here).
    built = {}

    def client_factory(profile):
        built["profile"] = profile
        return TuiStubClient()

    app = MessengerTUI(profiles=["alice", "bob"], client_factory=client_factory)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause()
            if app.screen.query(ProfileItem):
                break
        lv = app.screen.query_one("#profiles", ListView)
        lv.index = 0  # alice
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if built.get("profile"):
                break
    assert built.get("profile") == "alice"
    assert isinstance(app._client, TuiStubClient)
    assert app._suggester is None  # untouched __init__ default
    assert app._store is None


async def test_tui_at_command_sends_media(tmp_path):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, f"@{f} cap"))
        await pilot.pause()
        await pilot.pause()
        assert getattr(stub, "media_sent", None) == (7, str(f), "cap")
        bubbles = list(app.query(MessageBubble))
        assert any("cap" in str(b.render()) for b in bubbles)


async def test_tui_at_command_missing_file_notifies(tmp_path):
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        missing = str(tmp_path / "nope.jpg")
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, f"@{missing}"))
        await pilot.pause()
        assert getattr(stub, "media_sent", None) is None
        assert app.return_code is None  # still alive
