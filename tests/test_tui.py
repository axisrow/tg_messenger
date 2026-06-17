import asyncio
import inspect
import unicodedata
from datetime import datetime, timedelta, timezone

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.css.scalar import Unit
from textual.widgets import Footer, Input, Label, ListView, LoadingIndicator, Static, Tabs

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
    AccountItem,
    AccountsScreen,
    ConfirmScreen,
    DialogItem,
    EmojiPickerScreen,
    MessageBubble,
    MessengerTUI,
    ProfileItem,
    ProfileListCard,
    ReadLangScreen,
    SuggestSettingsCard,
    TranslateSettingsCard,
    VariantItem,
    _terminal_safe_display_text,
    parse_lang_command,
    parse_media_command,
    parse_tlang_command,
)


def test_parse_media_simple():
    assert parse_media_command("@a.jpg") == ("a.jpg", None)


def test_tui_terminal_safe_display_text_preserves_letters_and_emoji():
    # #129: the workaround must NOT corrupt text. Combining marks are load-bearing LETTERS in
    # these scripts; emoji must stay visible. Only the zero-width width-ambiguous glyphs
    # (variation selectors FE0E/FE0F, ZWJ) are stripped.
    thai = "เปิดทุกคู่"  # Mn marks U+0E34/0E38/0E39/0E48 must survive
    devanagari = "नमस्ते"  # virama U+094D + matras
    arabic = "السَّلامُ"  # shadda/harakat (Mn)
    hebrew = "שָׁלוֹם"  # niqqud points (Mn)
    for s in (thai, devanagari, arabic, hebrew):
        out = _terminal_safe_display_text(s)
        # canonically identical (the function NFC-normalizes) — NO letter/mark dropped
        assert out == unicodedata.normalize("NFC", s)
        # every combining mark is still present (the exact corruption #129 fixes)
        decomposed = unicodedata.normalize("NFD", out)
        for ch in unicodedata.normalize("NFD", s):
            if unicodedata.category(ch) == "Mn":
                assert ch in decomposed

    url = "https://777sportplus.net/register?m_ref=bh"
    assert _terminal_safe_display_text(url) == url  # URLs/ASCII untouched

    # emoji stay visible — never replaced with "*"
    assert _terminal_safe_display_text("👍") == "👍"
    mixed = "⚽ เปิด 🇩🇪"
    assert "*" not in _terminal_safe_display_text(mixed)
    assert "⚽" in _terminal_safe_display_text(mixed)
    assert "เปิด" in _terminal_safe_display_text(mixed)

    # the three width-ambiguous zero-width glyphs ARE stripped (the realignment that remains)
    assert "\ufe0f" not in _terminal_safe_display_text("❤️")  # FE0F dropped...
    assert "❤" in _terminal_safe_display_text("❤️")  # ...heart survives
    assert "\u200d" not in _terminal_safe_display_text("👨‍👩")  # ZWJ dropped
    assert _terminal_safe_display_text("a︎b") == "ab"  # FE0E (text VS) dropped

def test_parse_media_quoted_path_with_caption():
    assert parse_media_command('@"с пробелом.png" подпись') == ("с пробелом.png", "подпись")


def test_parse_lang_command():
    assert parse_lang_command("/lang en") == ("set", "en")
    assert parse_lang_command("/lang auto") == ("auto", None)
    assert parse_lang_command("/lang off") == ("off", None)
    assert parse_lang_command("hello") is None
    with pytest.raises(ValueError):
        parse_lang_command("/lang")


def test_parse_tlang_command():
    assert parse_tlang_command("/tlang en") == ("set", "en")
    assert parse_tlang_command("/tlang RU") == ("set", "ru")  # lowercased
    assert parse_tlang_command("/tlang off") == ("off", None)
    assert parse_tlang_command("/lang en") is None  # distinct from outbound /lang
    assert parse_tlang_command("hello") is None
    with pytest.raises(ValueError):
        parse_tlang_command("/tlang")


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
    # #113: title-first — the human-readable title leads, the id is subdued and trailing (#id).
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        item = list(app.query(DialogItem))[0]
        rendered = str(item.query_one(Static).render())  # "Ann [/x  #7" literally
        assert rendered.startswith("Ann")
        assert "#7" in rendered  # id still visible to the user, just subdued


async def test_tui_dialog_item_uses_terminal_safe_title_display():
    class DialogItemProbe(App):
        def compose(self) -> ComposeResult:
            yield ListView(DialogItem(-100200, "7วันปั่นลูกหนัง V4", unread=1, kind="group"))

    app = DialogItemProbe()
    async with app.run_test() as pilot:
        await pilot.pause()
        item = app.query_one(DialogItem)
        rendered = str(item.query_one(Static).render())
        assert "V4" in rendered
        assert "#-100200" in rendered
        # #129: the Thai marks are LETTERS — they must be preserved, not dropped
        assert "\u0e31" in rendered  # mai han-akat preserved
        assert "\u0e48" in rendered  # mai ek tone mark preserved
        assert "7วันปั่นลูกหนัง V4" in rendered  # full title intact


def test_tui_dialog_rows_pin_full_width_css():
    # #160: the dialog rows pin width:1fr so the row background paints past Thai/Indic
    # combining-mark width drift (Rich width 0 vs terminal width >0) — no stray dark patch.
    assert "#dialogs > DialogItem > Static" in MessengerTUI.CSS
    assert "#dialogs > DialogItem {" in MessengerTUI.CSS


class ThaiDialogClient(TuiStubClient):
    """A dialog list whose first entry has a Thai title with combining marks (#160)."""

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        return [Dialog(id=-100200, title="7วันปั่นลูกหนัง V4", kind="group", unread=1)]


async def test_tui_dialog_row_fills_full_row_width_for_thai_title():
    # #160: a Thai title with combining marks (Rich width 0, terminal width >0) must not leave a
    # stray unpainted patch — the row's inner Static spans the full list width so Textual paints
    # the whole row background. The title letters stay intact (see the terminal-safe display test).
    app = MessengerTUI(client=ThaiDialogClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        item = app.query_one(DialogItem)
        inner = item.query_one(Static)
        # the inner Static is pinned to a fraction width (1fr) — the fix that fills the whole row
        assert inner.styles.width is not None
        assert inner.styles.width.unit == Unit.FRACTION
        # and it actually spans the full row content width (no short-by-glyph-drift background fill)
        assert inner.region.width == item.content_region.width
        # title still intact (the fix must not strip Thai marks)
        rendered = str(inner.render())
        assert "ั" in rendered  # mai han-akat preserved
        assert "7วันปั่นลูกหนัง V4" in rendered


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
        assert "(" not in rendered  # no "(N)" badge when unread==0
        assert "#7" in rendered  # id still present


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


THAI_EMOJI_MESSAGE = "\n".join(
    [
        "**⚽️ 777SportsPlus+ ⚽️**",
        "**🏆บอลโลก 2026 มาแล้ว!🏆**",
        "**⚽️NEW โปรแกรมฟุตบอล**",
        "⚽️เปิดทุกคู่ ทุกลีคทั่วโลก ค่ายดังที่คุณวางใจ",
        "**▶️ UFABET     ▶️ BTI",
        "▶️ SBOBET     ▶️ WSSPORT**",
        "🌟**NEW โปรโมชั่น แทงครบ 3-5 บิล",
        "🌟(รับ FREE BET สูงสุด 100 บาท) ทันที!",
        "**⚽️กิจกรรมทายผลฟุตบอล มีทุกวัน!",
        "🚀 เปลี่ยนเพชรเป็นเงินรางวัล ลุ้นได้ทุกวัน!**",
        "https://777sportplus.net/register?m_ref=bh",
        "📲 หรือสอบถามเพิ่มเติม: @7sps",
    ]
)


class ThaiEmojiHistoryClient(TuiStubClient):
    async def history(self, peer, limit=50, offset_id=0):
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        messages = []
        for i in range(2770, 2792):
            score_text = "\n".join(
                [
                    "🇩🇪เยอรมนี 7-1 คูราเซา🇨🇼",
                    f"{i} 🇳🇱เนเธอร์แลนด์ 2-2 ญี่ปุ่น🇯🇵",
                ]
            )
            text = THAI_EMOJI_MESSAGE if i % 4 == 1 else score_text
            messages.append(
                Message(
                    id=i,
                    dialog_id=peer,
                    sender_id=8229443682,
                    out=False,
                    text=text,
                    date=date,
                )
            )
        return messages


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


async def test_tui_messages_pane_has_no_horizontal_overflow_but_keeps_vertical_scroll():
    app = MessengerTUI(client=ThaiEmojiHistoryClient())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._current = -100200
        app._current_kind = "group"
        await app._show_history(-100200)
        await pilot.pause()
        pane = app.query_one("#messages")

        assert pane.max_scroll_x == 0
        assert pane.show_horizontal_scrollbar is False
        assert pane.max_scroll_y > 0
        assert pane.show_vertical_scrollbar is True
        pane.scroll_end(animate=False, force=True)
        await pilot.pause()
        assert pane.scroll_y == pane.max_scroll_y
        assert pane.scrollbars_space == (0, 0)


async def test_tui_thai_emoji_bubbles_keep_right_padding_slack():
    app = MessengerTUI(client=ThaiEmojiHistoryClient())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._current = -100200
        app._current_kind = "group"
        await app._show_history(-100200)
        await pilot.pause()
        pane = app.query_one("#messages").region
        thai_bubble = next(b for b in app.query(MessageBubble) if "777SportsPlus" in str(b.render()))
        rendered = str(thai_bubble.render())

        assert thai_bubble.region.x >= pane.x
        assert thai_bubble.region.right <= pane.right
        assert thai_bubble.styles.padding.left == 1
        assert thai_bubble.styles.padding.right >= 4
        assert "https://777sportplus.net/register?m_ref=bh" in rendered
        # #129: the border fix is the CSS right-padding slack (asserted above), NOT glyph
        # deletion — the emoji and Thai are now PRESERVED; only the FE0F selector is stripped.
        assert "⚽" in rendered
        assert "\ufe0f" not in rendered


async def test_tui_messages_pane_does_not_collapse_on_narrow_terminal():
    # #110 bug #2: a fixed-width sidebar must yield space on a narrow terminal so the chat
    # pane (and composer) don't collapse / slide off-screen.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test(size=(33, 24)) as pilot:
        await pilot.pause()
        msgs = app.query_one("#messages", Vertical)
        assert msgs.region.width >= 5


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


async def test_emoji_picker_escape_does_not_clear_search():
    # #124 regression: the app binds Escape → clear_search. The emoji picker must CONSUME its own
    # Escape (a Binding, not a key_escape method) so cancelling the picker doesn't also bubble up
    # and silently wipe the user's active search filter.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "abc"
        await pilot.pause()
        app.push_screen(EmojiPickerScreen())
        await pilot.pause()
        assert isinstance(app.screen, EmojiPickerScreen)
        await pilot.press("escape")  # cancel the picker
        await pilot.pause()
        assert not isinstance(app.screen, EmojiPickerScreen)  # picker closed
        assert search.value == "abc"  # ...but the search filter survived


async def test_variant_picker_escape_does_not_clear_search():
    # #124 regression (twin of the emoji picker): Escape on the translation-variant picker cancels
    # only the modal, leaving an active search filter intact.
    from tg_messenger.tui.app import VariantPickScreen

    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "abc"
        await pilot.pause()
        app.push_screen(VariantPickScreen(["hola"], "hello"))
        await pilot.pause()
        assert isinstance(app.screen, VariantPickScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, VariantPickScreen)
        assert search.value == "abc"


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


async def test_tui_login_modal_is_centered_and_bordered():
    # #116: the login modal is a centered, bordered card — not a full-width, top-left raw box.
    from tg_messenger.tui.app import LoginScreen

    stub = TuiStubClient()
    stub.authorized = False
    app = MessengerTUI(client=stub, login_session=FakeTuiLoginSession())
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, LoginScreen))
        box = app.screen.query_one("#login-box")
        assert box.region.x > 0  # not flush-left (centered horizontally)
        assert box.region.y > 0  # not flush-top (centered vertically)
        assert box.region.width < app.size.width  # width-capped, not full width
        assert box.styles.border.top[0] != ""  # a border edge is set


async def test_tui_emoji_modal_is_centered_and_bordered():
    # #116: the emoji picker is a centered, bordered card too.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(EmojiPickerScreen())
        await pilot.pause()
        box = app.screen.query_one("#emoji-box")
        assert box.region.x > 0
        assert box.region.width < app.size.width
        assert box.styles.border.top[0] != ""


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
    # #93/#124: up/down move the selection between message bubbles; at the top edge up hands off
    # to the dialog list (no longer a clamp — see the dedicated edge tests below).
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


def _regions_overlap(a, b) -> bool:
    # rectangle intersection over two Textual Region objects (x/y/width/height)
    ix, iy = max(a.x, b.x), max(a.y, b.y)
    ax, ay = min(a.x + a.width, b.x + b.width), min(a.y + a.height, b.y + b.height)
    return (ax - ix) > 0 and (ay - iy) > 0


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


async def test_tui_unread_tab_drops_dialog_that_became_read_on_live_message():
    # #110 bug #4: a live message in the OPEN dialog zeroes its unread; on the "Непрочитанные"
    # tab the now-read dialog must disappear, not linger until the next reload.
    stub = UnreadTouchClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "unread"
        await pilot.pause()
        assert _listed_ids(app) == [8, -100200]
        app._current = 8  # dialog 8 is open → the live message marks it read
        stub.fire.set()
        await pilot.pause()
        assert _listed_ids(app) == [-100200]


class ReadToUnreadClient(TuiStubClient):
    """A live message arrives for a NON-open, initially-read dialog (id=7)."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        dms = [
            Dialog(id=7, title="Ann", username="ann", unread=0, is_contact=True),
            Dialog(id=8, title="Stranger", username="stranger", unread=3, is_contact=False),
        ]
        if dm_only:
            return dms
        return dms

    async def listen_all(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield IncomingEvent(
            dialog_id=7,
            message=Message(id=22, dialog_id=7, sender_id=7, out=False, text="fresh", date=date),
        )
        await asyncio.Event().wait()


async def test_tui_unread_tab_surfaces_dialog_that_became_unread_on_live_message():
    # #110 (Codex re-review): a live message for a NON-open, initially-read dialog must SURFACE on
    # the open "Непрочитанные" tab without a reload — the live touch updates the full snapshot, and
    # the tab projection re-includes it.
    stub = ReadToUnreadClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).active = "unread"
        await pilot.pause()
        assert _listed_ids(app) == [8]  # only Stranger is unread at load time (Ann is read)
        app._current = 8  # a DIFFERENT dialog is open, so the message for 7 increments its unread
        stub.fire.set()
        await pilot.pause()
        # Ann (7) just became unread → it must appear on the unread tab live
        assert set(_listed_ids(app)) == {7, 8}


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


async def test_tui_tab_switch_clears_stale_search_filter():
    # #110 bug #3: a search filter that matched on one tab must not leak onto the next
    # tab and make it look empty. Switching tabs resets #search.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.value = "Ann"  # matches id=7 on "all", nothing among groups
        await app.on_input_changed(Input.Changed(search, "Ann"))
        await pilot.pause()
        assert _listed_ids(app) == [7]
        app.query_one(Tabs).active = "groups"
        await pilot.pause()
        # the stale "Ann" filter must be gone, so the groups tab shows its dialog
        assert app.query_one("#search", Input).value == ""
        assert _listed_ids(app) == [-100200]


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


async def test_tui_tab_switch_clears_search_even_before_startup():
    # #110 (Codex re-review): a tab switch during a slow connect must still clear #search, so when
    # startup finishes it does not render the picked tab with a stale query (the pre-startup path).
    stub = TuiStubClient()
    connect_entered = asyncio.Event()
    stub.connect = hangs_forever(connect_entered)
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await asyncio.wait_for(connect_entered.wait(), 5)
        await pilot.pause()
        app.query_one("#search", Input).value = "zzz"  # user typed during the slow connect
        app.query_one(Tabs).active = "groups"
        await pilot.pause()
        assert app.query_one("#search", Input).value == ""  # cleared even though _started is False
        assert stub.dialogs_calls == 0  # still no network — the worker is gated by _started
        await pilot.press("ctrl+c")
    assert app.return_code == 0


class SlowDialogsClient(TuiStubClient):
    """dialogs() blocks until released, so a tab switch can race the initial load."""

    def __init__(self):
        super().__init__()
        self.dialogs_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        self.dialogs_entered.set()
        await self.release.wait()
        dms = [
            Dialog(id=7, title="Ann", username="ann", unread=0, is_contact=True),
            Dialog(id=8, title="Stranger", username="stranger", unread=2, is_contact=False),
        ]
        if dm_only:
            return dms
        return dms + [Dialog(id=-100200, title="Devs", kind="group", unread=1)]


async def test_tui_pre_startup_switch_to_archive_does_not_render_non_archived():
    # #110 (Codex 3rd pass): if the user switches to Archive while the initial dialogs() is still
    # pending (before _started), the finished load must NOT render the non-archived snapshot on the
    # Archive tab — the load re-runs under the new scope (archive endpoint), not pass-through.
    stub = SlowDialogsClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await asyncio.wait_for(stub.dialogs_entered.wait(), 5)  # _load_dialogs is in dialogs()
        app._tab = "archive"  # user switched tab mid-load (on_tabs_tab_activated under _started gate)
        stub.release.set()  # let the initial non-archive dialogs() return
        await _pause_until(pilot, lambda: app._started)
        await pilot.pause()
        # Archive shows the archived set (10, -100400), NOT the non-archived snapshot (7, 8, -100200)
        assert _listed_ids(app) == [10, -100400]
        await pilot.press("ctrl+c")
    assert app.return_code == 0


class SlowConnectStore(TuiSourceStore):
    """store.connect() blocks until released — the pre-startup window AFTER the initial load."""

    def __init__(self):
        super().__init__()
        self.connect_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def connect(self):
        self.connect_entered.set()
        await self.release.wait()


async def test_tui_pre_startup_switch_during_store_connect_reconciles_tab():
    # #110 (Codex 4th pass): a tab switch during the store.connect() await — AFTER the initial
    # _load_dialogs, still before _started — must be reconciled once startup finishes. Otherwise the
    # archive tab would render the non-archived snapshot forever (no reload is ever scheduled).
    stub = TuiStubClient()
    store = SlowConnectStore()
    app = MessengerTUI(client=stub, store=store)
    async with app.run_test() as pilot:
        await asyncio.wait_for(store.connect_entered.wait(), 5)  # dialogs loaded; stuck in store.connect
        assert app._tab == "all"
        app._tab = "archive"  # user switched tab in the post-load, pre-_started window
        store.release.set()
        await _pause_until(pilot, lambda: app._started)
        await _pause_until(pilot, lambda: _listed_ids(app) == [10, -100400])
        assert _listed_ids(app) == [10, -100400]  # reconciled to the archived set, not the DM snapshot
        await pilot.press("ctrl+c")
    assert app.return_code == 0


async def test_tui_archive_switch_during_store_connect_never_shows_non_archived():
    # #112: while store.connect() is still pending (post-load, pre-_started), a switch to Archive
    # must NEVER expose the non-archived snapshot under the loading spinner. PR #111's reconcile
    # fixes the FINAL state; this asserts the in-flight window too — no non-archive id is rendered
    # while connect is pending, then the final state is the archived set.
    stub = TuiStubClient()
    store = SlowConnectStore()
    app = MessengerTUI(client=stub, store=store)
    non_archive_ids = {7, 8, -100200, -100300, 9}
    async with app.run_test() as pilot:
        await asyncio.wait_for(store.connect_entered.wait(), 5)  # dialogs loaded; stuck in store.connect
        app._tab = "archive"  # switch lands in the connect window
        # while connect is still pending, the list must not show any non-archived ids
        for _ in range(5):
            await pilot.pause()
            assert set(_listed_ids(app)).isdisjoint(non_archive_ids)
            assert app._started is False
        store.release.set()
        await _pause_until(pilot, lambda: app._started)
        await _pause_until(pilot, lambda: _listed_ids(app) == [10, -100400])
        assert _listed_ids(app) == [10, -100400]
        await pilot.press("ctrl+c")
    assert app.return_code == 0


async def test_tui_startup_opens_started_gate_before_reconcile_render():
    # #118 (Codex high, follow-up to #112): the store path used to await a reconcile render WHILE
    # _started was still False, so a switch to Archive in that render window scheduled no reload
    # (the gate was closed) and the non-archived snapshot stayed under Archive. The gate must open
    # BEFORE any reconcile render. Pin the invariant: capture _started at the moment the startup
    # reconcile touches the list — it must already be True.
    stub = TuiStubClient()
    store = SlowConnectStore()
    reconcile_started = []  # _started captured on each RECONCILE render (tab != loaded source)

    class GateProbeTUI(MessengerTUI):
        async def _render_dialogs(self):
            # the initial load renders with tab == loaded source; a reconcile render is the one
            # where they differ (a switch landed during a pre-gate await). Only the latter must
            # run under an open gate.
            if self._tab != self._loaded_tab:
                reconcile_started.append(self._started)
            await super()._render_dialogs()

    app = GateProbeTUI(client=stub, store=store)
    async with app.run_test() as pilot:
        await asyncio.wait_for(store.connect_entered.wait(), 5)
        # same-source switch (all→groups): the OLD code reconciled this with an inline
        # `await _render_dialogs()` BEFORE setting _started=True, so the render ran under a closed
        # gate (the bug). The fix opens the gate first, so this reconcile render sees _started=True.
        app._tab = "groups"
        store.release.set()
        await _pause_until(pilot, lambda: app._started)
        await _pause_until(pilot, lambda: _listed_ids(app) == [-100200])
        assert reconcile_started, "reconcile render never ran"
        assert all(s is True for s in reconcile_started), \
            f"reconcile render ran with gate closed: {reconcile_started}"
        assert _listed_ids(app) == [-100200]  # projected to groups
        await pilot.press("ctrl+c")
    assert app.return_code == 0


async def test_tui_pre_startup_switch_to_same_source_tab_reconciles_projection():
    # #110 (Codex 4th pass): a same-source switch (all→groups) during store.connect must re-project
    # without a refetch when startup finishes.
    stub = TuiStubClient()
    store = SlowConnectStore()
    app = MessengerTUI(client=stub, store=store)
    async with app.run_test() as pilot:
        await asyncio.wait_for(store.connect_entered.wait(), 5)
        calls_after_load = stub.dialogs_calls
        app._tab = "groups"  # same source (non-archive), different projection
        store.release.set()
        await _pause_until(pilot, lambda: app._started)
        await _pause_until(pilot, lambda: _listed_ids(app) == [-100200])
        assert _listed_ids(app) == [-100200]  # projected to groups
        assert stub.dialogs_calls == calls_after_load  # no refetch — same source, just re-projected
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


async def test_tui_group_author_survives_tab_switch_dropping_dialog_from_list():
    # #108 (Codex review): the author line must keep rendering for the OPEN group after a tab
    # switch removed that group from _all_dialogs (the snapshot is the current tab's subset). The
    # kind captured at selection time (_current_kind) drives it, not a fresh _all_dialogs lookup.
    stub = GroupSenderEventClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        # open the group the way on_list_view_selected does: current id + kind captured while present
        app._current = -100200
        app._current_kind = "group"
        # a tab switch reloads _all_dialogs with another tab's subset — the group is now ABSENT,
        # so a fresh _dialog_kind(-100200) would return None and drop the author line
        app._all_dialogs = [d for d in app._all_dialogs if d.id != -100200]
        assert app._dialog_kind(-100200) is None  # confirm the group really left the list
        stub.fire.set()
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
    # the author line is still rendered, driven by the captured _current_kind
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


# --- #113: presentation redesign (dim author/[id], title-first dialog item, framing) ---

def _has_dim_span(content, start: int, end: int) -> bool:
    """True if a span covering [start, end) carries a dim style (textual Content.spans)."""
    spans = getattr(content, "spans", None) or []
    for sp in spans:
        if sp.start == start and sp.end == end and getattr(sp.style, "dim", False):
            return True
    return False


def _any_dim_span_covering(content, index: int) -> bool:
    spans = getattr(content, "spans", None) or []
    return any(sp.start <= index < sp.end and getattr(sp.style, "dim", False) for sp in spans)


async def test_tui_dialog_item_id_is_dim_and_trailing():
    # #113: title leads (prominent), the raw id is subdued (dim) and trailing.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        content = list(app.query(DialogItem))[0].query_one(Static).render()
        plain = str(content)
        assert plain.startswith("Ann")  # title first
        assert plain.rstrip().endswith("#7")  # id trailing
        assert _any_dim_span_covering(content, plain.index("#7"))  # id rendered dim


async def test_tui_bubble_author_line_is_dim():
    # #113: the group author line keeps its content but is dimmed (spans), not removed.
    stub = GroupSenderEventClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100200
        stub.fire.set()
        await pilot.pause()
        content = list(app.query(MessageBubble))[0].render()
    assert str(content) == "9 @bob Bob Lee\n[21] привет"  # content parity (no behavior change)
    assert _has_dim_span(content, 0, len("9 @bob Bob Lee"))  # author line dimmed


async def test_tui_bubble_id_prefix_is_dim():
    # #113: the "[id] " prefix is subdued (dim span) while the body stays literal.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        content = list(app.query(MessageBubble))[0].render()
    plain = str(content)
    assert plain.startswith("[1] ")  # content unchanged
    prefix_len = plain.index("] ") + 2  # length of "[1] "
    assert _has_dim_span(content, 0, prefix_len)


async def test_tui_dm_body_starting_with_bracket_is_not_dimmed_as_author():
    # #118 (Codex): a DM bubble (show_author=False) whose BODY contains a newline followed by
    # "[" must NOT be misparsed as an author line — untrusted message text must not drive the
    # author/[id] metadata styling. Only the real "[id] " prefix of the first body line is dim.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        msgs = app.query_one("#messages")
        # no author passed (DM path): body's own newline+"[" is plain content, not metadata
        bubble = MessageBubble("[1] hi\n[2] forged", out=False, message_id=1, dialog_id=7)
        await msgs.mount(bubble)
        await pilot.pause()
        content = bubble.render()
        plain = str(content)
    assert plain == "[1] hi\n[2] forged"  # content unchanged
    # the only dim span is the genuine "[1] " prefix; the forged second line is NOT an author line
    forged_at = plain.index("[2]")
    assert not _any_dim_span_covering(content, forged_at)  # second "[...]" not dimmed
    assert not _any_dim_span_covering(content, plain.index("\n") - 1)  # first line tail not dim


class TwoMessageHistoryClient(TuiStubClient):
    """history(7) returns an incoming + an outgoing message so in/out framing is testable."""

    async def history(self, peer, limit=50, offset_id=0):
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return [
            Message(id=1, dialog_id=peer, sender_id=peer, out=False, text="hi there", date=date),
            Message(id=2, dialog_id=peer, sender_id=1, out=True, text="hello back", date=date),
        ]


async def test_tui_incoming_outgoing_bubbles_are_aligned_differently():
    # #113: in/out distinction beyond color — the out bubble is offset further right.
    app = MessengerTUI(client=TwoMessageHistoryClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # incoming (id=1) + outgoing (id=2)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        bin_ = next(b for b in bubbles if "in" in b.classes)
        bout = next(b for b in bubbles if "out" in b.classes)
        assert bout.region.x > bin_.region.x


async def test_tui_bubbles_stay_inside_messages_pane_on_narrow_terminal():
    # #118 (Codex high): a fixed 20-col side margin pushed the outgoing bubble off the right
    # edge when #chat shrinks to its min-width. Bubbles must stay inside #messages at any width.
    app = MessengerTUI(client=TwoMessageHistoryClient())
    async with app.run_test(size=(36, 24)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # incoming (id=1) + outgoing (id=2)
        await pilot.pause()
        pane = app.query_one("#messages").region
        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) >= 2
        for b in bubbles:
            assert b.region.x >= pane.x, f"bubble left {b.region.x} < pane {pane.x}"
            assert b.region.right <= pane.right, f"bubble right {b.region.right} > pane {pane.right}"


async def test_tui_bubbles_have_vertical_separation():
    # #113: consecutive bubbles are visually separated (margin + border), not run together.
    app = MessengerTUI(client=TwoMessageHistoryClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) >= 2
        assert bubbles[1].region.y > bubbles[0].region.y + 1  # clear gap between bubbles


async def test_tui_bubble_brackets_render_literally_after_styling():
    # #113 regression: untrusted body with markup-looking text must never be parsed.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one("#messages", Vertical)
        bubble = MessageBubble("[1] [b]not bold[/b]", out=False, message_id=1, dialog_id=7)
        await pane.mount(bubble)
        await pilot.pause()
        assert "[b]not bold[/b]" in str(bubble.render())


async def test_tui_translation_and_reactions_keep_content_after_styling():
    # #113: translation + reactions still compose under the new Text builder (content parity).
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one("#messages", Vertical)
        bubble = MessageBubble("[1] hi", out=False, message_id=1, dialog_id=7)
        await pane.mount(bubble)
        bubble.show_translation("привет")
        bubble.add_reaction("👍")
        await pilot.pause()
        # #129: the reaction line shows the real emoji, not "*"
        assert str(bubble.render()) == "[1] hi\n↳ привет\n👍"


async def test_tui_translation_line_uses_accent_colour():
    # the translation line must NOT be plain white (it merged with the original) — it gets the
    # theme accent so it visibly separates from the body.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one("#messages", Vertical)
        bubble = MessageBubble("[1] hi", out=False, message_id=1, dialog_id=7)
        await pane.mount(bubble)
        bubble.show_translation("привет")
        await pilot.pause()
        accent = app.theme_variables.get("accent")
        assert accent  # the theme exposes an accent colour
        assert bubble._translation_style() == accent
        # the Rich Text span covering the translation carries that accent style (not empty/white)
        text = bubble._build()
        translation_start = str(text).index("↳")
        styles = {
            str(span.style)
            for span in text.spans
            if span.start <= translation_start < span.end and str(span.style)
        }
        assert accent in styles


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


class UnreadTouchClient(TuiStubClient):
    """A live message lands in the OPEN dialog (id=8), zeroing its unread badge."""

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def dialogs(self, dm_only=True):
        self.dialogs_calls += 1
        dms = [Dialog(id=8, title="Stranger", username="stranger", unread=2, is_contact=False)]
        if dm_only:
            return dms
        return dms + [Dialog(id=-100200, title="Devs", kind="group", unread=1)]

    async def listen_all(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield IncomingEvent(
            dialog_id=8,
            message=Message(id=22, dialog_id=8, sender_id=8, out=False, text="fresh", date=date),
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
    assert rendered == ["Bob  (1)  #8", "Ann  #7", "Devs  #-100200"]
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
    assert rendered == ["Bob  #8", "Ann  #7", "Devs  #-100200"]
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
    assert rendered == ["Bob  #8", "Ann  #7", "Devs  #-100200"]
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
    assert rendered == ["Bob  #8", "Ann  #7", "Devs  #-100200"]
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


class OutgoingEchoOnSendClient(TuiStubClient):
    """send_text enqueues the self-echo so listen_outgoing() yields it DURING the
    _touch_dialog_for_message await — reproducing the duplicate-echo race (regression from
    the #160 fix, where _remember_sent ran AFTER that await)."""

    def __init__(self):
        super().__init__()
        self._echo_q: asyncio.Queue = asyncio.Queue()

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append((peer, text, reply_to))
        self.sent_event.set()
        msg = Message(id=99, dialog_id=peer, sender_id=1, out=True, text=text,
                      date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        # the real account echoes our own message back almost immediately
        await self._echo_q.put(OutgoingEvent(dialog_id=peer, message=msg))
        return msg

    async def listen_outgoing(self):
        while True:
            yield await self._echo_q.get()


async def test_tui_real_send_path_not_duplicated_by_fast_outgoing_echo():
    # Regression for the #160 fix: _remember_sent ran AFTER the _touch_dialog_for_message await,
    # so a fast listen_outgoing() echo raced in before the dedup key was armed and _drain_outgoing
    # drew a SECOND bubble. Exercise the REAL path (on_input_submitted → worker), which the
    # direct-_send_text #160 test missed.
    stub = OutgoingEchoOnSendClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _select_dialog(pilot, app, 7)
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, "hi there"))
        # one bubble for our send (echo deduped); robust to the seeded "[1]" history bubble
        await _pause_until(
            pilot,
            lambda: sum("hi there" in str(b.render()) for b in app.query(MessageBubble)) >= 1,
        )
        for _ in range(5):  # let any racing echo bubble mount too, so a dup would show
            await pilot.pause()
        n = sum("hi there" in str(b.render()) for b in app.query(MessageBubble))
        assert n == 1, f"expected exactly one sent bubble, got {n} (duplicate echo)"


class OutgoingFallbackClient(TuiStubClient):
    """listen_outgoing, отдающий эхо нашего отправленного сообщения по сигналу fire.

    Имитирует уход из диалога во время отправки: пузырёк не нарисуется (peer != _current),
    но эхо ДОЛЖНО появиться через _drain_outgoing при возврате — ключ подавления не записан.
    """

    def __init__(self):
        super().__init__()
        self.fire = asyncio.Event()

    async def listen_outgoing(self):
        await self.fire.wait()
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # id=2 — ровно то, что вернёт send_text стаба (см. TuiStubClient)
        yield OutgoingEvent(dialog_id=7, message=Message(
            id=2, dialog_id=7, sender_id=1, out=True, text="hi", date=date))
        await asyncio.Event().wait()


async def test_tui_send_echo_not_suppressed_when_navigated_away_mid_send():
    # #160: a send whose optimistic bubble is SKIPPED (the user switched dialogs during the
    # in-flight send) must end up NOT in _sent_ids — otherwise its listen_outgoing() echo is
    # suppressed forever and the message stays invisible until the chat is reopened. The key is
    # armed before the send await (to dedup a fast echo) and POPPED here because the bubble was
    # not drawn — so the END state is un-armed and _drain_outgoing renders the echo on return.
    stub = OutgoingFallbackClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _select_dialog(pilot, app, 7)
        # simulate "navigated away during the send": _current is 8 by the time the bubble would mount
        app._current = 8
        await app._send_text(7, "hi")  # peer=7 != _current=8 → bubble skipped
        await pilot.pause()
        # the key must NOT be recorded — no bubble was drawn for it
        assert (7, 2) not in app._sent_ids

        # back in dialog 7, the live echo must now be drawn by _drain_outgoing (the fallback)
        await _select_dialog(pilot, app, 7)
        stub.fire.set()
        await _pause_until(
            pilot, lambda: any("hi" in str(b.render()) for b in app.query(MessageBubble))
        )
        assert any("hi" in str(b.render()) for b in app.query(MessageBubble))


class OutgoingEchoWhileAwayClient(TuiStubClient):
    """The dangerous interleaving (Codex local review, PR #161): send_text enqueues the self-echo
    so listen_outgoing() yields it WHILE _touch_dialog_for_message is awaiting AND the user has
    navigated away (peer != _current). The echo arrives while the dedup key is still armed, so
    _drain_outgoing drops it; the send path's else-branch must still pop the key so the message is
    not suppressed forever. A second (resync) echo on return must then render via _drain_outgoing.
    """

    def __init__(self):
        super().__init__()
        self._echo_q: asyncio.Queue = asyncio.Queue()

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append((peer, text, reply_to))
        self.sent_event.set()
        msg = Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                      date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        # echo the just-sent message back immediately — yielded DURING the send's own
        # _touch_dialog_for_message await (which suspends across several loop turns)
        await self._echo_q.put(OutgoingEvent(dialog_id=peer, message=msg))
        return msg

    def resync_echo(self, peer):
        """Simulate Telegram re-echoing the message after a reconnect/resync."""
        self._echo_q.put_nowait(OutgoingEvent(
            dialog_id=peer,
            message=Message(id=2, dialog_id=peer, sender_id=1, out=True, text="hi",
                            date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        ))

    async def listen_outgoing(self):
        while True:
            yield await self._echo_q.get()


async def test_tui_navigated_away_echo_during_await_is_not_permanently_suppressed():
    # Codex local-review regression (PR #161): the echo races in DURING the send await while the
    # user is on another dialog. The armed key dedups that first echo (no spurious bubble in the
    # away view), but the else-branch pop must leave _sent_ids un-armed so the message is NOT
    # suppressed forever — a later resync echo renders it on return. The existing navigated-away
    # test only fires the echo AFTER the pop; this covers the dangerous interleaving.
    stub = OutgoingEchoWhileAwayClient()
    app = MessengerTUI(client=stub)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _select_dialog(pilot, app, 7)
        app._current = 8  # navigated away during the in-flight send
        await app._send_text(7, "hi")  # echo enqueued during the _touch_dialog await
        for _ in range(5):  # let the racing echo be consumed by _drain_outgoing
            await pilot.pause()
        # end state: the key was popped (bubble not drawn), so the echo is recoverable, not lost
        assert (7, 2) not in app._sent_ids
        # no away-view bubble was duplicated for dialog 8
        assert not any("hi" in str(b.render()) for b in app.query(MessageBubble))

        # back in dialog 7, a resync echo must now render via the _drain_outgoing fallback
        await _select_dialog(pilot, app, 7)
        stub.resync_echo(7)
        await _pause_until(
            pilot, lambda: any("hi" in str(b.render()) for b in app.query(MessageBubble))
        )
        assert any("hi" in str(b.render()) for b in app.query(MessageBubble))


class LongHistorySendClient(TuiStubClient):
    """A dialog with enough history that the pane scrolls — so a freshly-sent bubble appended at
    the bottom is below the viewport unless the scroll reaches the recomputed end (#160 r3)."""

    async def history(self, peer, limit=50, offset_id=0):
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return [
            Message(id=i, dialog_id=peer, sender_id=peer, out=(i % 2 == 0),
                    text=f"history message number {i} with some length to fill the row",
                    date=date + timedelta(minutes=i))
            for i in range(1, 29)
        ]

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append((peer, text, reply_to))
        self.sent_event.set()
        return Message(id=216, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, 1, tzinfo=timezone.utc))


async def test_tui_sent_bubble_is_scrolled_into_view_in_a_long_chat():
    # #160 r3 (the REAL "message doesn't show until refresh" bug): the optimistic bubble IS mounted,
    # but _scroll_messages_to_end ran before Textual recomputed the layout for the new widget, so
    # the pane stayed scrolled above it and the message was off-screen until a manual reopen.
    # Assert the new bubble's region is INSIDE the pane viewport — presence in the DOM is not enough.
    stub = LongHistorySendClient()
    app = MessengerTUI(client=stub)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _select_dialog(pilot, app, 7)
        for _ in range(8):
            await pilot.pause()
        pane = app.query_one("#messages", Vertical)
        assert pane.max_scroll_y > 0, "history did not fill the pane — test precondition broken"
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, "MY NEW MESSAGE"))
        # the optimistic bubble mounts at the bottom; the pane MUST scroll to the recomputed end so
        # it is on screen. Presence in the DOM is not enough — assert the scroll reached max.
        await _pause_until(pilot, lambda: pane.scroll_y == pane.max_scroll_y)
        b = next(b for b in app.query(MessageBubble) if "MY NEW MESSAGE" in str(b.render()))
        pr = b.region
        viewport = pane.region
        assert viewport.y <= pr.y < viewport.y + viewport.height, (
            f"sent bubble off-screen: bubble.y={pr.y} viewport={viewport} "
            f"scroll_y={pane.scroll_y} max_scroll_y={pane.max_scroll_y}"
        )


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
    # one bubble (the message); the reaction line shows the real emoji ONCE (deduped), #129.
    assert len(bubbles) == 1
    rendered = str(bubbles[0].render())
    assert "👍" in rendered
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
    # #129: real emoji on the reaction line — ❤️ renders as ❤ (FE0F stripped), 👍 unchanged
    assert rendered.endswith("👍 ❤")
    assert "👍" in rendered
    assert "❤" in rendered
    assert "️" not in rendered  # the only thing still stripped is the variation selector


async def test_tui_reaction_and_translation_coexist_either_order():
    # #106: translation and reactions are separate bubble state — neither clobbers the other.
    # #113: bubbles render a Rich Text, whose render() resolves through the app theme, so mount
    # them in a running app (the only context they ever render in) before asserting.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one("#messages", Vertical)
        bubble = MessageBubble("[1] hi", out=False, message_id=1, dialog_id=7)
        await pane.mount(bubble)
        bubble.show_translation("привет")
        bubble.add_reaction("👍")
        first = str(bubble.render())
        assert "↳ привет" in first and "👍" in first and "[1] hi" in first

        bubble2 = MessageBubble("[2] yo", out=False, message_id=2, dialog_id=7)
        await pane.mount(bubble2)
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
    assert "👍" not in str(bubbles[0].render())  # #129: no reaction landed (emoji not added)


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
    assert str(bubbles[0].render()).endswith("🔥")  # #129: real emoji, not "*"


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
    assert str(bubbles[0].render()).endswith("👍")  # #129: real emoji, not "*"
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
    assert "👍" not in rendered  # #129: the reaction did not land under the other dialog's bubble
    assert app._pending_reactions == {}  # nor was it buffered (the bubble existed, just mismatched)


async def test_tui_stale_bubble_after_switch_cannot_be_reacted_via_mouse():
    # #128: after a dialog switch, the previous dialog's bubbles linger until the async history
    # worker removes them. A mouse focus + action_react on such a STALE bubble must be rejected
    # (older switch generation than the app's), so a reaction can't land in a half-replaced view.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)

    async def pick(screen):
        return "👍"

    app.push_screen_wait = pick  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)  # dialog-7 bubble built under the current generation
        await pilot.pause()
        stale = list(app.query(MessageBubble))[0]
        gen_before = app._switch_gen
        # a real switch to dialog 8: bumps the generation; the dialog-7 bubble is now stale
        # (the async _show_history(8) hasn't run remove_children yet — the render window).
        app._open_dialog(8)
        assert app._switch_gen == gen_before + 1
        assert stale.switch_gen == gen_before  # the lingering bubble carries the OLD generation
        # mouse-click path: focus + action_react on the stale bubble — must be rejected
        stale.action_react()
        await pilot.pause()
    assert stub.reactions == []  # no reaction sent on the stale previous-dialog bubble


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
        app._pending_suggestion_dialog = 7
        app.query_one("#suggestion", Static).update("Suggest: draft for Ann")
        lv = app.query_one("#dialogs", ListView)
        lv.index = 1
        lv.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert app._pending_suggestion is None
        assert app._pending_suggestion_dialog is None  # #158: dialog scope cleared too
        strip = app.query_one("#suggestion", Static)
        assert str(strip.render()) == ""
        # #170: the strip is now bordered, so an EMPTY strip must be hidden — else its empty border
        # floats above the composer.
        assert strip.display is False


async def test_tui_suggestion_line_not_covered_by_composer():
    # #110 bug #1: #suggestion and #composer must not overlap — the "💡 Tab:" hint has to be
    # visible above the composer, not hidden under it.
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.query_one("#suggestion", Static).update("💡 Tab: draft")  # give it a line to render
        await pilot.pause()
        sug = app.query_one("#suggestion", Static)
        comp = app.query_one("#composer", Input)
        assert sug.region.height >= 1 and sug.region.width >= 1
        assert not _regions_overlap(sug.region, comp.region)


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


# --- #114: unified focus navigation (Tab / Shift+Tab cycle panels; accept_suggestion preserved) ---


async def test_tui_tab_cycles_focus_forward_through_panels():
    # #114: with no pending suggestion, Tab falls through to forward focus cycling. From the
    # search box the next focusable panel in DOM order is the tabs strip.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#search", Input).focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is app.query_one(Tabs)  # search → tabs


async def test_tui_shift_tab_cycles_focus_backward():
    # #114: Shift+Tab cycles focus backward (mirror of Tab). From the dialog list the prior
    # focusable panel is the tabs strip.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#dialogs", ListView).focus()
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is app.query_one(Tabs)  # dialogs → (back) tabs


async def test_tui_tab_accepts_pending_suggestion_not_focus():
    # #114: when a suggestion is pending, Tab accepts it into the composer instead of cycling focus.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        app._pending_suggestion = "draft reply"
        app.query_one("#suggestion", Static).update("💡 Tab: draft reply")
        app.query_one("#composer", Input).focus()
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#composer", Input).value == "draft reply"
        assert app._pending_suggestion is None
        assert app.focused is app.query_one("#composer", Input)


async def test_tui_tab_falls_through_when_no_suggestion():
    # #114: with no pending suggestion, Tab must MOVE focus (the accept-fallthrough), not stay put.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not search  # focus advanced


# --- #124: vertical-chain arrow navigation (search ↓ tabs ↓ dialogs ↓ messages ↓ composer) ---


class EmptyHistoryClient(TuiStubClient):
    async def history(self, peer, limit=50, offset_id=0):
        return []


async def test_search_down_focuses_tabs():
    # search ↓ → tabs (the next link in the chain). Up at search is the top: no-op.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one(Tabs)


async def test_search_up_is_noop_at_top():
    # search is the top of the chain — Up stays on search and leaves the (empty) value alone.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is search
        assert search.value == ""


async def test_dialogs_down_at_last_hands_off_to_messages():
    # dialogs (last item) ↓ → the first message bubble (chat pane entry). The last dialog (id 8) is
    # also the open one here, so the handoff is a pure focus move into the already-loaded chat.
    app = MessengerTUI(client=LongHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = len(lv) - 1  # last dialog (id 8)
        await pilot.press("right")  # open it so highlighted == _current (no wrong-recipient switch)
        await _pause_until(pilot, lambda: bool(list(app.query(MessageBubble))))
        lv.focus()
        lv.index = len(lv) - 1
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        assert app.focused is bubbles[0]


async def test_dialogs_down_at_last_no_bubbles_focuses_composer():
    # dialogs (last item) ↓ with no messages loaded → the composer (chain never dead-ends).
    app = MessengerTUI(client=EmptyHistoryClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not list(app.query(MessageBubble))
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = len(lv) - 1
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one("#composer", Input)


async def test_bubble_up_at_first_returns_to_dialogs():
    # first bubble ↑ → back to the dialog list (#124: was a clamp).
    app = MessengerTUI(client=LongHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        bubbles[0].focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


async def test_bubble_down_at_last_focuses_composer():
    # last bubble ↓ → the composer (#124: was a clamp).
    app = MessengerTUI(client=LongHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        bubbles[-1].focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one("#composer", Input)


async def test_composer_up_focuses_last_bubble():
    # composer ↑ → the last message bubble.
    app = MessengerTUI(client=LongHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        app.query_one("#composer", Input).focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        bubbles = list(app.query(MessageBubble))
        assert app.focused is bubbles[-1]


async def test_composer_up_focuses_dialogs_when_no_bubbles():
    # composer ↑ with no messages → the dialog list (chain never dead-ends).
    app = MessengerTUI(client=EmptyHistoryClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not list(app.query(MessageBubble))
        app.query_one("#composer", Input).focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


async def test_composer_down_is_noop_at_bottom():
    # composer is the bottom of the chain — Down stays on the composer.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)
        composer.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is composer


class ThreeMessageHistoryClient(TuiStubClient):
    # a small, fixed history so a full intra-bubble arrow walk is feasible with a few keypresses.
    async def history(self, peer, limit=50, offset_id=0):
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return [
            Message(id=i, dialog_id=peer, sender_id=peer, out=False, text=f"msg {i}", date=date)
            for i in range(1, 4)
        ]


async def test_full_chain_down_then_up():
    # the whole vertical chain with arrows ALONE: search → tabs → dialogs → bubble walk → composer,
    # then symmetric back up. The intra-bubble steps are real keypresses (not .focus()) so the walk
    # itself is exercised. The LAST dialog is the open one so the Down handoff is a pure focus move
    # (no wrong-recipient dialog switch — see test_dialogs_down_handoff_commits_*).
    app = MessengerTUI(client=ThreeMessageHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        tabs = app.query_one(Tabs)
        dialogs = app.query_one("#dialogs", ListView)
        composer = app.query_one("#composer", Input)
        # open the LAST dialog so the cursor and the open chat agree
        dialogs.focus()
        dialogs.index = len(dialogs) - 1
        await pilot.press("right")
        await _pause_until(pilot, lambda: len(list(app.query(MessageBubble))) == 3)
        bubbles = list(app.query(MessageBubble))

        search.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is tabs
        await pilot.press("down")  # tabs → dialogs
        await pilot.pause()
        assert app.focused is dialogs
        dialogs.index = len(dialogs) - 1  # the last dialog (already open) — Down hands off
        await pilot.pause()
        await pilot.press("down")  # dialogs (last) → first bubble
        await pilot.pause()
        assert app.focused is bubbles[0]
        # walk DOWN through the bubbles with arrows alone, then into the composer
        for nxt in bubbles[1:]:
            await pilot.press("down")
            await pilot.pause()
            assert app.focused is nxt
        await pilot.press("down")  # last bubble → composer
        await pilot.pause()
        assert app.focused is composer
        # symmetric path back up: composer → last bubble → ...walk up... → first bubble → dialogs → tabs
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is bubbles[-1]
        for prev in reversed(bubbles[:-1]):
            await pilot.press("up")
            await pilot.pause()
            assert app.focused is prev
        await pilot.press("up")  # first bubble → dialogs
        await pilot.pause()
        assert app.focused is dialogs
        dialogs.index = 0
        await pilot.pause()
        await pilot.press("up")  # dialogs (first) → tabs
        await pilot.pause()
        assert app.focused is tabs


# --- #124: the ?/F1 key-help overlay ---


async def test_f1_opens_and_closes_help():
    from tg_messenger.tui.app import HelpScreen

    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("f1")  # same key toggles it closed (via the modal's own binding)
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_f1_opens_help_from_inside_composer():
    # F1 is non-printable, so a priority app binding fires even while an Input is focused.
    from tg_messenger.tui.app import HelpScreen

    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#composer", Input).focus()
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)


async def test_question_mark_opens_help_outside_inputs():
    # "?" works when focus is NOT on a text input (here: the tab strip).
    from tg_messenger.tui.app import HelpScreen

    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")  # Escape also closes the overlay
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_f1_over_another_modal_is_noop():
    # #124 cleanup: F1 is a priority binding so it fires even over an open modal. It must NOT stack
    # HelpScreen on top of that modal (which owns the screen) — pressing F1 there is a no-op.
    from tg_messenger.tui.app import AccountsScreen, HelpScreen

    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")  # open account settings (a different modal)
        await pilot.pause()
        assert isinstance(app.screen, AccountsScreen)
        await pilot.press("f1")  # must not bury the modal under HelpScreen
        await pilot.pause()
        assert isinstance(app.screen, AccountsScreen)
        assert not isinstance(app.screen, HelpScreen)


# --- #124: text editing in the Inputs is unaffected by the new up/down bindings ---


async def test_composer_typing_and_cursor_unaffected():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)
        composer.focus()
        await pilot.pause()
        await pilot.press("a", "b", "c", "left", "x")
        await pilot.pause()
        assert composer.value == "abxc"  # left moved the cursor; up/down bindings didn't interfere


async def test_search_typing_filters_and_cursor_unaffected():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#search", Input)
        search.focus()
        await pilot.pause()
        await pilot.press("A", "n", "left", "n")  # type into the search box
        await pilot.pause()
        assert search.value == "Ann"  # left moved the cursor between n's
        # live filter still works: only the "Ann" DM (id=7) matches the query
        assert [item.dialog_id for item in app.query(DialogItem)] == [7]


# --- #124-r2: tabs↑→search, dialogs →/space, composer ←, bubble space/x ---


async def test_tabs_up_focuses_search():
    # ↑ on the tab strip returns focus to the search box (symmetric to down/enter → dialogs).
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is app.query_one("#search", Input)


async def test_dialogs_right_opens_dialog_and_focuses_composer():
    # → from the dialog list opens the highlighted dialog and drops the cursor into the composer.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0  # first dialog (id=7)
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert app._current == 7
        assert app.focused is app.query_one("#composer", Input)


async def test_dialogs_space_jumps_to_last_then_first():
    # space toggles the cursor between the last and the first dialog.
    app = MessengerTUI(client=TwoDmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0
        await pilot.pause()
        await pilot.press("space")  # first → last
        await pilot.pause()
        assert lv.index == len(lv) - 1
        await pilot.press("space")  # last → first
        await pilot.pause()
        assert lv.index == 0


async def test_composer_left_empty_focuses_dialogs():
    # ← on an EMPTY composer leaves the chat (back to the dialog list).
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)
        assert composer.value == ""
        composer.focus()
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert app.focused is app.query_one("#dialogs", ListView)


async def test_composer_left_nonempty_moves_cursor():
    # ← with text is a normal cursor move — focus stays, value unchanged, cursor steps left.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", Input)
        composer.focus()
        await pilot.pause()
        await pilot.press("a", "b")  # value "ab", cursor at end (pos 2)
        await pilot.pause()
        assert composer.value == "ab" and composer.cursor_position == 2
        await pilot.press("left")
        await pilot.pause()
        assert app.focused is composer  # did NOT leave the composer
        assert composer.value == "ab" and composer.cursor_position == 1


async def test_bubble_space_jumps_to_last_then_first():
    # space on a message toggles focus between the last and the first bubble.
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
        await pilot.press("space")  # first → last
        await pilot.pause()
        assert app.focused is bubbles[-1]
        await pilot.press("space")  # last → first
        await pilot.pause()
        assert app.focused is bubbles[0]


async def test_bubble_x_opens_reaction_picker():
    # "x" is a synonym for "r": focus a bubble, press "x", pick → the reaction is sent.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)

    async def pick(screen):
        return "🔥"

    app.push_screen_wait = pick  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        bubble = list(app.query(MessageBubble))[0]
        bubble.focus()
        await pilot.press("x")
        await _pause_until(pilot, lambda: bool(stub.reactions))
    assert stub.reactions == [(7, 1, "🔥")]


async def test_dialogs_down_handoff_commits_highlighted_dialog():
    # #124 regression (wrong-recipient send): open dialog 7, then arrow the cursor to a DIFFERENT
    # last dialog WITHOUT selecting it, then Down to hand off into the chat pane. The Down must
    # commit the highlighted dialog first, so _current is the dialog the cursor sits on — not 7
    # (the previously-open chat). Otherwise a reply would silently go to the wrong recipient.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0  # open dialog 7 (first item on the "Все" tab)
        await pilot.press("right")
        await _pause_until(pilot, lambda: app._current == 7)
        # move the cursor to the last dialog WITHOUT opening it (cursor move = Highlighted only)
        lv.focus()
        lv.index = len(lv) - 1
        await pilot.pause()
        last_id = lv.highlighted_child.dialog_id
        assert last_id != 7 and app._current == 7  # diverged before the handoff
        await pilot.press("down")  # hand off into the chat pane — must commit the highlighted dialog
        await _pause_until(pilot, lambda: app._current == last_id)
        assert app._current == last_id  # the handoff opened the highlighted dialog, not the stale one


async def test_dialogs_down_handoff_then_send_targets_highlighted_dialog():
    # #124 regression (the privacy bug end to end): after the Down handoff above, typing a message
    # and submitting must send to the highlighted dialog, never the previously-open one (7).
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0  # open dialog 7
        await pilot.press("right")
        await _pause_until(pilot, lambda: app._current == 7)
        lv.focus()
        lv.index = len(lv) - 1  # cursor on the last dialog, still showing dialog 7's chat
        await pilot.pause()
        last_id = lv.highlighted_child.dialog_id
        assert last_id != 7
        await pilot.press("down")  # handoff → opens the highlighted dialog
        await _pause_until(pilot, lambda: app._current == last_id)
        composer = app.query_one("#composer", Input)
        composer.focus()
        await pilot.press("s", "e", "c", "r", "e", "t", "enter")
        await stub.wait_sent_count(1)
    assert stub.sent == [(last_id, "secret", None)]  # sent to the highlighted dialog, not the stale 7


async def test_dialogs_down_handoff_commits_current_synchronously():
    # #124 regression (Codex): the Down handoff must commit _current SYNCHRONOUSLY, before focus
    # moves into the composer — not via a posted ListView.Selected the pump drains later. Otherwise
    # a same-tick Enter / queued paste in the composer would snapshot the stale _current and send to
    # the previously-open dialog (a wrong-recipient private send). Assert _current flips the instant
    # the handoff action runs, with NO intervening pause (the pump never gets a turn).
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0
        await pilot.press("right")
        await _pause_until(pilot, lambda: app._current == 7)
        lv.focus()
        lv.index = len(lv) - 1  # cursor on the last dialog WITHOUT opening it
        await pilot.pause()
        last_id = lv.highlighted_child.dialog_id
        assert last_id != 7 and app._current == 7  # diverged before the handoff
        lv.action_cursor_down_or_messages()  # the handoff — runs synchronously, no pump turn
        assert app._current == last_id  # committed IMMEDIATELY, not deferred to the pump
        # and a submit in the very same window now targets the highlighted dialog
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, "secret"))
        await stub.wait_sent_count(1)
    assert stub.sent == [(last_id, "secret", None)]  # never the stale 7


async def test_dialogs_right_open_commits_current_synchronously():
    # #124 regression (Codex): the Right-open path has the same race as Down — it focuses the
    # composer right after action_select_cursor, which only posts Selected. Commit _current
    # synchronously so a same-tick submit can't leak text to the previously-open dialog.
    stub = TuiStubClient()
    app = MessengerTUI(client=stub)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0
        await pilot.press("right")
        await _pause_until(pilot, lambda: app._current == 7)
        lv.focus()
        lv.index = len(lv) - 1  # cursor on the last dialog WITHOUT opening it
        await pilot.pause()
        last_id = lv.highlighted_child.dialog_id
        assert last_id != 7 and app._current == 7  # diverged before the open
        lv.action_open_dialog()  # Right — runs synchronously, no pump turn
        assert app._current == last_id  # committed IMMEDIATELY, not deferred to the pump
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, "secret"))
        await stub.wait_sent_count(1)
    assert stub.sent == [(last_id, "secret", None)]  # never the stale 7


async def test_dialogs_down_handoff_switch_does_not_focus_stale_bubble():
    # #124 regression (wrong-conversation action): open dialog 7 (its bubble mounts), then arrow the
    # cursor to a DIFFERENT writable dialog (8) WITHOUT selecting it and press Down. The switch only
    # POSTS ListView.Selected — _current and the history render happen later (on_list_view_selected →
    # _show_history removes and re-mounts bubbles in a worker) — so the bubbles still mounted at the
    # handoff moment belong to dialog 7. Focusing one would land on a STALE bubble, and
    # MessageBubble.action_react acts on the bubble's OWN dialog id (not _current), so a fast r/x there
    # would react on the previous conversation. The handoff must land on the composer, never a stale
    # dialog-7 bubble — and must stay off stale bubbles once the new history finishes loading.
    class TwoWritableDmClient(TuiStubClient):
        async def dialogs(self, dm_only=True):
            self.dialogs_calls += 1
            return [
                Dialog(id=7, title="Ann", username="ann", is_contact=True),
                Dialog(id=8, title="Bob", username="bob", is_contact=False),
            ]

    app = MessengerTUI(client=TwoWritableDmClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = 0  # open dialog 7
        await pilot.press("right")
        await _pause_until(pilot, lambda: app._current == 7 and bool(list(app.query(MessageBubble))))
        assert all(b.dialog_id == 7 for b in app.query(MessageBubble))  # mounted bubbles are dialog 7's
        lv.focus()
        lv.index = len(lv) - 1  # cursor on the LAST dialog (8), still showing dialog 7's chat
        await pilot.pause()
        assert lv.highlighted_child.dialog_id == 8 and app._current == 7  # diverged before the handoff
        await pilot.press("down")  # hand off into a DIFFERENT dialog
        focused = app.focused
        # the instant of the switch must not focus a stale dialog-7 bubble
        assert not (isinstance(focused, MessageBubble) and focused.dialog_id == 7)
        assert focused is app.query_one("#composer", Input)  # lands on the safe composer
        # after the new history renders, focus is still off any stale dialog-7 bubble
        await _pause_until(pilot, lambda: app._current == 8)
        focused = app.focused
        assert not (isinstance(focused, MessageBubble) and focused.dialog_id == 7)


async def test_composer_up_during_switch_cannot_focus_stale_bubble():
    # #124 regression (Codex cycle-2, wrong-conversation reaction): the text-send race is closed by
    # committing _current synchronously, but old MessageBubble nodes linger in the DOM until the
    # async history worker (_show_history) runs remove_children. In that window pressing Up from the
    # composer (ComposerInput.action_focus_messages) must NOT re-enter a stale previous-dialog bubble
    # — else a following r/x would react on the PREVIOUS conversation (MessageBubble.action_react acts
    # on the bubble's OWN dialog_id). We materialise that exact window deterministically: mount dialog
    # 7's bubbles, then flip _current to 8 with the bubbles still present (the state between the
    # synchronous switch and the worker's first remove_children).
    from tg_messenger.tui.app import _focus_first_bubble_or_composer, _navigable_bubbles

    app = MessengerTUI(client=LongHistoryClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)          # mount dialog 7's bubbles
        await pilot.pause()
        stale = list(app.query(MessageBubble))
        assert stale and all(b.dialog_id == 7 for b in stale)
        app._current = 8                    # the switch committed _current; bubbles not yet removed
        # the stale dialog-7 bubbles must be UNREACHABLE by navigation now
        assert _navigable_bubbles(app.screen) == []
        # Up from the composer must fall back to the dialog list, never a dialog-7 bubble
        composer = app.query_one("#composer", Input)
        composer.action_focus_messages()
        await pilot.pause()
        focused = app.focused
        assert not (isinstance(focused, MessageBubble) and focused.dialog_id == 7)
        assert focused is app.query_one("#dialogs", ListView)
        # entering the pane from the dialog list must also skip the stale bubbles (→ composer here,
        # since dialog 8 has no bubbles mounted yet)
        _focus_first_bubble_or_composer(app.screen)
        await pilot.pause()
        focused = app.focused
        assert not (isinstance(focused, MessageBubble) and focused.dialog_id == 7)
        assert focused is composer


async def test_dialogs_right_on_readonly_channel_keeps_focus_alive():
    # #124 regression: Right on a read-only channel disables the composer (async, via
    # _apply_composer_writable). Focusing a soon-to-be-disabled composer would leave focus on
    # nothing (Textual releases focus from a disabled widget) and kill arrow navigation. The Right
    # handler must keep focus on the dialog list there instead of diving into the dead composer.
    stub = TuiStubClient()
    stub.channel_can_send = False
    app = MessengerTUI(client=stub)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        # the default "Все" tab already lists the read-only channel (-100300)
        await _pause_until(pilot, lambda: any(
            i.dialog_id == -100300 for i in app.query(DialogItem)))
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = next(i for i, it in enumerate(lv.children)
                        if getattr(it, "dialog_id", None) == -100300)
        await pilot.pause()
        await pilot.press("right")  # open the read-only channel
        await _pause_until(pilot, lambda: app._current == -100300)
        assert app.query_one("#composer", Input).disabled  # read-only → composer disabled
        assert app.focused is not None  # focus is NOT lost into nothing
        assert app.focused is lv  # stays on the dialog list, so arrow navigation still works


async def test_dialogs_down_handoff_to_readonly_channel_keeps_focus_alive():
    # #124 regression (cycle 2): the Down handoff must apply the SAME read-only guard as Right.
    # The read-only channel (-100300) is the LAST dialog here, so Down from it commits the channel,
    # which disables the composer async — focusing it would lose focus into nothing. Down must keep
    # focus on the dialog list instead, exactly like the Right path.
    class ReadOnlyLastClient(TuiStubClient):
        async def dialogs(self, dm_only=True):
            self.dialogs_calls += 1
            # a single read-only channel as the only (hence last) dialog
            return [Dialog(id=-100300, title="News", kind="channel", can_send=False)]

    app = MessengerTUI(client=ReadOnlyLastClient())
    async with app.run_test(size=(80, 20)) as pilot:
        await _pause_until(pilot, lambda: any(
            i.dialog_id == -100300 for i in app.query(DialogItem)))
        lv = app.query_one("#dialogs", ListView)
        lv.focus()
        lv.index = len(lv) - 1  # the read-only channel is the last (and only) item
        await pilot.pause()
        await pilot.press("down")  # handoff into the read-only chat pane
        await _pause_until(pilot, lambda: app._current == -100300)
        assert app.query_one("#composer", Input).disabled  # read-only → composer disabled
        assert app.focused is not None  # focus is NOT lost into nothing
        assert app.focused is lv  # stays on the dialog list, so arrow navigation still works


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
        self.auto_translate = False  # #126: _startup assigns self._auto_translate from this


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


# --- #115: accounts settings screen (add / list / remove a profile; no in-session switch) ---


class FakeSessionStore:
    """In-memory SessionStore stand-in: list/save/delete profiles, no disk, no network.

    Models the real store's filename sanitization (#121): names are keyed by
    ``sanitize_profile_name`` and ``list_profiles`` returns the canonical stems, so an
    unsafe/colliding raw name maps onto an existing file exactly as on disk.
    """

    def __init__(self, profiles=()):
        from tg_messenger.core.names import sanitize_profile_name

        self._sanitize = sanitize_profile_name
        self._profiles = [self._sanitize(p) for p in profiles]
        self.saved = []

    def list_profiles(self):
        return sorted(self._profiles)

    def save(self, name, session_string):
        canon = self._sanitize(name)
        if canon not in self._profiles:
            self._profiles.append(canon)
        self.saved.append((canon, session_string))

    def delete(self, name):
        canon = self._sanitize(name)
        if canon in self._profiles:
            self._profiles.remove(canon)
            return True
        return False

    def is_valid_profile(self, name):
        return self._sanitize(name) in self._profiles


class SavingStubClient(TuiStubClient):
    """A new-profile client whose save_session() persists into a FakeSessionStore.

    Lets the add-account test exercise the production path (client.save_session()) while the
    store records the save — no real network/disk.
    """

    def __init__(self, name, store):
        super().__init__()
        self._name = name
        self._store = store

    def save_session(self):
        super().save_session()
        self._store.save(self._name, "session-string-stub")


async def test_tui_open_settings_lists_profiles_with_active_marked():
    store = FakeSessionStore(["alice", "bob"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _pause_until(pilot, lambda: isinstance(app.screen, AccountsScreen))
        items = list(app.screen.query(AccountItem))
        assert [it.profile for it in items] == ["alice", "bob"]
        alice_row = next(str(it.query_one(Static).render()) for it in items if it.profile == "alice")
        bob_row = next(str(it.query_one(Static).render()) for it in items if it.profile == "bob")
        assert "(текущий)" in alice_row  # active profile marked
        assert "(текущий)" not in bob_row


async def test_tui_settings_add_profile_runs_wizard_and_saves(caplog):
    store = FakeSessionStore(["alice"])
    sess = FakeTuiLoginSession()
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store,
            account_client_factory=lambda name: SavingStubClient(name, store),
            login_session=sess,
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-profile", Input).value = "bob"
        with caplog.at_level("INFO"):
            await pilot.press("a")  # add_account → pushes LoginScreen
            await _pause_until(pilot, lambda: app.screen.query("#login-input"))
            # drive the wizard: phone then code
            app.screen.query_one("#login-input", Input).value = "+10000000000"
            await pilot.press("enter")
            await pilot.pause()
            app.screen.query_one("#login-input", Input).value = "12345"
            await pilot.press("enter")
            await _pause_until(pilot, lambda: "bob" in store.list_profiles())
        assert "bob" in store.list_profiles()
        assert sess.phones == ["+10000000000"] and sess.codes == ["12345"]
        # the new profile now shows in the list
        await _pause_until(
            pilot, lambda: "bob" in [it.profile for it in app.screen.query(AccountItem)]
        )
        # no secrets (phone/code) reached the logs
        for rec in caplog.records:
            msg = rec.getMessage()
            assert "+10000000000" not in msg and "12345" not in msg


async def test_tui_settings_remove_non_active_profile():
    store = FakeSessionStore(["alice", "bob"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(profiles=store.list_profiles(), active="alice", store=store)
        app.push_screen(screen)
        await pilot.pause()
        lv = screen.query_one("#accounts", ListView)
        lv.index = 1  # "bob"
        screen.action_remove_account()
        # #121: removal now asks for confirmation — confirm it
        await _pause_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _pause_until(pilot, lambda: store.list_profiles() == ["alice"])
        assert store.list_profiles() == ["alice"]
        assert [it.profile for it in screen.query(AccountItem)] == ["alice"]


async def test_tui_settings_remove_asks_confirmation_and_cancel_keeps_profile():
    # #121: deletion is a destructive action — it must confirm (parity with CLI `profiles
    # remove`), and cancelling leaves the profile intact.
    store = FakeSessionStore(["alice", "bob"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(profiles=store.list_profiles(), active="alice", store=store)
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#accounts", ListView).index = 1  # "bob"
        screen.action_remove_account()
        await _pause_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("escape")  # cancel
        await pilot.pause()
        assert store.list_profiles() == ["alice", "bob"]  # nothing deleted


async def test_tui_settings_add_unsafe_name_is_rejected():
    # #121: a raw name that sanitizes to a DIFFERENT file (so it would overwrite another
    # account's session) is rejected before any client/login — nothing is saved.
    store = FakeSessionStore(["alice"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        built = []
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store,
            account_client_factory=lambda name: built.append(name) or SavingStubClient(name, store),
            login_session=FakeTuiLoginSession(),
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-profile", Input).value = "../alice"  # → sanitizes to "alice"
        await pilot.press("a")
        await pilot.pause()
        assert app.screen is screen  # no LoginScreen pushed
        assert built == []  # client never built for an unsafe name
        assert store.list_profiles() == ["alice"]  # alice's session not overwritten


async def test_tui_settings_add_duplicate_canonical_name_is_rejected():
    # #121: a name whose canonical form already exists is rejected (no silent overwrite).
    store = FakeSessionStore(["work_personal"])
    app = MessengerTUI(client=TuiStubClient(), session_name="work_personal", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        built = []
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="work_personal", store=store,
            account_client_factory=lambda name: built.append(name) or SavingStubClient(name, store),
            login_session=FakeTuiLoginSession(),
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-profile", Input).value = "work_personal"  # already present
        await pilot.press("a")
        await pilot.pause()
        assert app.screen is screen
        assert built == []
        assert store.list_profiles() == ["work_personal"]


async def test_tui_settings_active_marked_and_protected_under_sanitization():
    # #121: the active profile's raw session name may sanitize differently than the listed
    # (canonical) stems. The marker AND the delete guard must compare canonical forms, so the
    # active row is still marked "(текущий)" and cannot be deleted.
    store = FakeSessionStore(["work_personal", "bob"])
    # active raw name "work/personal" → canonical "work_personal" (the listed stem)
    app = MessengerTUI(client=TuiStubClient(), session_name="work/personal", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="work/personal", store=store
        )
        app.push_screen(screen)
        await pilot.pause()
        items = list(screen.query(AccountItem))
        active_row = next(
            str(it.query_one(Static).render()) for it in items if it.profile == "work_personal"
        )
        assert "(текущий)" in active_row  # marked despite raw≠canonical
        # try to delete the active (canonical) row — must be refused, no confirm dialog
        screen.query_one("#accounts", ListView).index = next(
            i for i, it in enumerate(items) if it.profile == "work_personal"
        )
        screen.action_remove_account()
        await pilot.pause()
        assert app.screen is screen  # no ConfirmScreen pushed
        assert "work_personal" in store.list_profiles()  # active protected


async def test_tui_settings_cannot_remove_active_profile():
    store = FakeSessionStore(["alice", "bob"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(profiles=store.list_profiles(), active="alice", store=store)
        app.push_screen(screen)
        await pilot.pause()
        lv = screen.query_one("#accounts", ListView)
        lv.index = 0  # "alice" — the active profile
        screen.action_remove_account()
        await pilot.pause()
        assert app.screen is screen  # no ConfirmScreen pushed (active is protected)
        assert "alice" in store.list_profiles()  # active profile is protected


async def test_tui_settings_add_empty_name_is_noop():
    store = FakeSessionStore(["alice"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store,
            login_session=FakeTuiLoginSession(),
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-profile", Input).value = "   "  # whitespace only
        await pilot.press("a")
        await pilot.pause()
        assert app.screen is screen  # no LoginScreen pushed (still on AccountsScreen)
        assert store.list_profiles() == ["alice"]  # nothing added


# --- #126: inbound translation toggle (t) + on-demand translate (Ctrl+T) -----------------


class FakeKVStorage:
    """Minimal SQLite-KV stand-in recording set_value calls for persistence assertions."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.sets = []

    async def get_value(self, key):
        return self.values.get(key)

    async def set_value(self, key, value):
        self.values[key] = value
        self.sets.append((key, value))


class FakeTranslator:
    """In-memory Translator double — no LLM. Translates incoming text to f"{text}!{lang}"."""

    def __init__(self, *, target_lang="ru", auto=None, storage=None):
        self._target = target_lang
        self._auto = auto
        self.storage = storage or FakeKVStorage()
        self.history_calls = []
        self.set_lang_calls = []

    async def target_lang(self):
        return self._target

    async def set_target_lang(self, code):
        self.set_lang_calls.append(code)
        self._target = code

    async def auto_enabled(self):
        return self._auto

    async def set_auto_enabled(self, enabled):
        self._auto = enabled
        await self.storage.set_value("translate_auto", "1" if enabled else "0")

    async def max_messages(self):
        # the whole-chat (Ctrl+T) path reloads up to this many messages before translating
        return 100

    async def translate_history(self, dialog_id, messages):
        self.history_calls.append((dialog_id, list(messages)))
        if not self._target:
            return list(messages)
        out = []
        for m in messages:
            if not m.out and m.text:
                out.append(m.model_copy(update={"translated_text": f"{m.text}!{self._target}"}))
            else:
                out.append(m)
        return out

    async def translate_message(self, message):
        return (await self.translate_history(message.dialog_id, [message]))[0]


def _has_translation(app) -> bool:
    return any("↳" in str(b.render()) for b in app.query(MessageBubble))


async def test_tui_auto_translate_off_by_default_no_translation():
    tr = FakeTranslator()
    app = MessengerTUI(client=TuiStubClient(), translator=tr)  # auto_translate defaults False
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._auto_translate is False
        await app._show_history(7)
        await pilot.pause()
        await pilot.pause()
        assert tr.history_calls == []  # the LLM path was never kicked — no tokens spent
        assert not _has_translation(app)


async def test_tui_toggle_t_flips_notifies_persists():
    storage = FakeKVStorage()
    tr = FakeTranslator(storage=storage)
    app = MessengerTUI(client=TuiStubClient(), translator=tr)
    notes = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        app.set_focus(app.query_one("#dialogs", ListView))  # `t` is printable: not in an Input
        await pilot.press("t")
        await _pause_until(pilot, lambda: app._auto_translate is True)
        await _pause_until(pilot, lambda: ("translate_auto", "1") in storage.sets)
        await pilot.press("t")
        await _pause_until(pilot, lambda: app._auto_translate is False)
        await _pause_until(pilot, lambda: ("translate_auto", "0") in storage.sets)
    assert any("включ" in n for n in notes)
    assert any("выключ" in n for n in notes)


async def test_tui_t_swallowed_in_composer_but_ctrl_t_reaches_handler():
    tr = FakeTranslator()
    app = MessengerTUI(client=TuiStubClient(), translator=tr)
    notes = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7  # writable DM
        notes.clear()
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        composer = app.query_one("#composer", Input)
        composer.disabled = False
        composer.focus()
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert "t" in composer.value  # printable `t` typed into the Input, not a toggle
        assert app._auto_translate is False
        # Ctrl+T is non-printable + priority → reaches the handler even from inside the composer:
        # with a chat open it runs the whole-chat translate pass (observed via history_calls).
        await pilot.press("ctrl+t")
        await _pause_until(pilot, lambda: bool(tr.history_calls))


async def test_tui_toggle_on_translates_open_chat():
    tr = FakeTranslator(target_lang="ru")
    app = MessengerTUI(client=TuiStubClient(), translator=tr)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        assert not _has_translation(app)  # auto off → nothing yet
        app.action_toggle_auto_translate()  # turning ON translates the open chat
        await _pause_until(pilot, lambda: _has_translation(app))
        assert app._auto_translate is True


async def test_tui_ctrl_t_translates_regardless_of_flag():
    tr = FakeTranslator(target_lang="ru")
    app = MessengerTUI(client=TuiStubClient(), translator=tr)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _select_dialog(pilot, app, 7)  # loads history; focus ends on composer
        await _pause_until(pilot, lambda: bool(app.query(MessageBubble)))  # history mounted
        assert app._auto_translate is False
        assert not _has_translation(app)
        await pilot.press("ctrl+t")  # on-demand, ignores the auto flag
        await _pause_until(pilot, lambda: _has_translation(app))
        assert tr.history_calls  # the translate path ran despite auto being off


async def test_tui_ctrl_t_no_open_chat_notifies():
    tr = FakeTranslator()
    app = MessengerTUI(client=TuiStubClient(), translator=tr)
    notes = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        await pilot.press("ctrl+t")  # no dialog open
        await pilot.pause()
    assert any("Нет открытого диалога" in n for n in notes)
    assert tr.history_calls == []


async def test_tui_translate_prompts_for_language_when_unset():
    tr = FakeTranslator(target_lang=None)  # no reading language configured
    app = MessengerTUI(client=TuiStubClient(), translator=tr)
    seen = []

    async def fake_psw(screen):
        seen.append(type(screen).__name__)
        return "ru"  # user enters a code in the modal

    app.push_screen_wait = fake_psw  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        await pilot.press("ctrl+t")
        await _pause_until(pilot, lambda: _has_translation(app))
    assert "ReadLangScreen" in seen  # prompted instead of silently doing nothing
    assert tr.set_lang_calls == ["ru"]


async def test_tui_translate_not_configured_warns():
    app = MessengerTUI(client=TuiStubClient(), translator=None)  # agent extra/model absent
    notes = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        await pilot.press("ctrl+t")
        await pilot.pause()
        app.set_focus(app.query_one("#dialogs", ListView))
        await pilot.press("t")
        await pilot.pause()
    # both the whole-chat Ctrl+T and the `t` toggle warn when no translator is wired
    assert sum("Переводчик не настроен" in n for n in notes) >= 2


async def test_tui_auto_translate_covers_channels():
    tr = FakeTranslator(target_lang="ru")
    app = MessengerTUI(client=TuiStubClient(), translator=tr, auto_translate=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = -100300  # a broadcast channel
        await app._show_history(-100300)
        await _pause_until(pilot, lambda: _has_translation(app))
        assert tr.history_calls  # channel messages flow through the same translate path


async def test_tui_auto_translate_pref_loaded_at_startup():
    tr = FakeTranslator(auto=True)  # persisted KV says ON
    # constructor seed is False (env default) — the persisted pref must win
    app = MessengerTUI(client=TuiStubClient(), translator=tr, auto_translate=False)
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._auto_translate is True)


async def test_tui_live_incoming_translated_when_auto_on():
    stub = GroupEventClient()
    tr = FakeTranslator(target_lang="ru")
    app = MessengerTUI(client=stub, translator=tr, auto_translate=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7  # the DM the live event targets
        stub.fire.set()
        await _pause_until(pilot, lambda: _has_translation(app))


async def test_tui_live_incoming_not_translated_when_auto_off():
    stub = GroupEventClient()
    tr = FakeTranslator(target_lang="ru")
    app = MessengerTUI(client=stub, translator=tr)  # auto off
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        stub.fire.set()
        await _pause_until(pilot, lambda: bool(app.query(MessageBubble)))  # bubble mounted
        await pilot.pause()
        assert not _has_translation(app)  # ...but never translated
        assert tr.history_calls == []


async def test_tui_tlang_command_sets_reading_language():
    tr = FakeTranslator(target_lang=None)
    app = MessengerTUI(client=TuiStubClient(), translator=tr)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7  # writable DM — composer commands are allowed
        composer = app.query_one("#composer", Input)
        await app.on_input_submitted(Input.Submitted(composer, "/tlang en"))
        await _pause_until(pilot, lambda: tr.set_lang_calls == ["en"])


async def test_tui_read_lang_screen_submit_and_cancel():
    app = MessengerTUI(client=TuiStubClient())
    results = []
    async with app.run_test() as pilot:
        await pilot.pause()
        # Enter on the input dismisses with the entered code...
        app.push_screen(ReadLangScreen(), callback=results.append)
        await pilot.pause()
        app.screen.query_one("#readlang-input", Input).value = "de"
        await pilot.press("enter")
        await _pause_until(pilot, lambda: results == ["de"])
        # ...and Esc dismisses with None.
        app.push_screen(ReadLangScreen(), callback=results.append)
        await pilot.pause()
        await pilot.press("escape")
        await _pause_until(pilot, lambda: results == ["de", None])


# --- Footer visibility + inbound-translation settings (#127-followup) --------------------


class StubTranslator:
    """Minimal Translator stand-in for AccountsScreen tests (no Storage, no LLM)."""

    _DEFAULTS = {
        "mode": "off", "target": None, "known": [], "unknown": [],
        "model": None, "max_messages": 100,
    }

    def __init__(self, settings=None, *, history=None):
        merged = dict(self._DEFAULTS)
        merged.update(settings or {})
        self._settings = merged
        self.saved = []
        self._history = list(history or [])

    async def get_settings(self):
        return dict(self._settings)

    async def set_settings(self, *, mode, target=None, known=None, unknown=None,
                           model=None, max_messages=None):
        self.saved.append({
            "mode": mode, "target": target, "known": known, "unknown": unknown,
            "model": model, "max_messages": max_messages,
        })

    async def max_messages(self):
        return self._settings.get("max_messages") or 100

    async def target_lang(self):
        # the whole-chat (Ctrl+T) path checks this before translating; honour the configured target
        return self._settings.get("target")

    async def set_target_lang(self, code):
        self._settings["target"] = code

    async def auto_enabled(self):
        return None  # never-persisted; the app keeps its constructor/env default

    async def set_auto_enabled(self, enabled):
        pass

    async def translate_history(self, dialog_id, messages):
        return list(self._history) if self._history else list(messages)


async def test_tui_footer_is_present_and_shows_help_and_settings():
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        # the Footer exists (was missing — the reported "не вижу настроек/?")
        assert app.query(Footer)
        # exactly one visible "Справка" binding (no F1/? duplicate) and a "Настройки" binding
        labels = [b.description for b in app._bindings.shown_keys]
        assert labels.count("Справка") == 1
        assert "Настройки" in labels
        # the Footer only renders bindings ACTIVE in the current focus (search Input on startup).
        # The help hint must be the F1 binding (priority → active inside an Input); a "?"-only hint
        # would be filtered out exactly when the user first looks — the original complaint.
        active = [
            ab.binding for ab in app.active_bindings.values() if ab.binding.show
        ]
        active_help = [b for b in active if b.description == "Справка"]
        assert len(active_help) == 1 and active_help[0].key == "f1"
        assert any(b.description == "Настройки" for b in active)


async def test_tui_settings_sections_are_card_widgets():
    # #163: each settings section is its OWN composable card widget (compose + handlers + guard),
    # not inline screen markup; AccountsScreen only orchestrates. The profiles card is always
    # present; translate/suggest cards are gated on their wired dependency.
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "off", "target": "", "known": [], "unknown": []})
    suggester = StubSuggesterTUI()
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store,
            translator=translator, suggester=suggester,
        )
        app.push_screen(screen)
        await pilot.pause()
        profiles_card = screen.query_one(ProfileListCard)
        translate_card = screen.query_one(TranslateSettingsCard)
        suggest_card = screen.query_one(SuggestSettingsCard)
        # the load-echo guards now live on the cards, not the screen
        assert hasattr(translate_card, "_applied_mode")
        assert hasattr(suggest_card, "_applied_suggest_enabled")
        # the section ids stay reachable from the screen (query searches the whole DOM subtree)
        assert screen.query("#translate-section")
        assert screen.query("#suggest-section")
        # the preserved widget ids resolve to children of their owning card
        assert profiles_card.query("#accounts")
        assert translate_card.query("#translate-mode")
        assert suggest_card.query("#suggest-enabled")


async def test_tui_settings_shows_translate_section_when_translator_wired():
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "skip_known", "target": "ru", "known": ["ru", "en"], "unknown": ["ja"]})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, translator=translator,
        )
        app.push_screen(screen)
        await pilot.pause()
        assert screen.query("#translate-section")
        # stored mode selected; each of the three fields prefilled from its own setting
        assert screen.query_one("#mode-skip_known").value is True
        assert screen.query_one("#target-lang", Input).value == "ru"
        assert screen.query_one("#known-langs", Input).value == "ru, en"
        assert screen.query_one("#unknown-langs", Input).value == "ja"


async def test_tui_settings_hides_translate_section_without_translator():
    store = FakeSessionStore(["alice"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(profiles=store.list_profiles(), active="alice", store=store)
        app.push_screen(screen)
        await pilot.pause()
        assert not screen.query("#translate-section")


class StubSuggesterTUI:
    """Minimal Suggester stand-in for AccountsScreen tests (no Storage, no LLM)."""

    def __init__(self, settings=None):
        self._settings = settings or {"enabled": True, "history": 30, "model": None}
        self.saved = []

    async def get_settings(self):
        return dict(self._settings)

    async def save_settings(self, *, enabled, history, model):
        if history < 1:
            raise ValueError("history must be positive")
        self._settings = {"enabled": enabled, "history": history, "model": model}
        self.saved.append(dict(self._settings))


async def test_tui_settings_shows_suggest_section_when_suggester_wired():
    store = FakeSessionStore(["alice"])
    suggester = StubSuggesterTUI({"enabled": True, "history": 25, "model": "openai:gpt-4o"})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, suggester=suggester,
        )
        app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        assert screen.query("#suggest-section")
        from textual.widgets import Switch
        assert screen.query_one("#suggest-enabled", Switch).value is True
        assert screen.query_one("#suggest-history", Input).value == "25"
        assert screen.query_one("#suggest-model", Input).value == "openai:gpt-4o"


async def test_tui_settings_disabled_load_does_not_spurious_save():
    # #143 review: loading a stored enabled=False flips the Switch (compose default True),
    # whose Changed fires ASYNC — must NOT be mistaken for a user toggle and auto-save.
    store = FakeSessionStore(["alice"])
    suggester = StubSuggesterTUI({"enabled": False, "history": 30, "model": None})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, suggester=suggester,
        )
        app.push_screen(screen)
        # several pumps so the async Switch.Changed echo is delivered
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        from textual.widgets import Switch
        assert screen.query_one("#suggest-enabled", Switch).value is False
        assert suggester.saved == []  # load alone never persists


async def test_tui_settings_hides_suggest_section_without_suggester():
    store = FakeSessionStore(["alice"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(profiles=store.list_profiles(), active="alice", store=store)
        app.push_screen(screen)
        await pilot.pause()
        assert not screen.query("#suggest-section")


async def test_tui_settings_saves_suggest_fields():
    store = FakeSessionStore(["alice"])
    suggester = StubSuggesterTUI()
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, suggester=suggester,
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#suggest-history", Input).value = "12"
        screen.query_one("#suggest-model", Input).value = "openai:gpt-4o"
        await screen._save_suggest_settings()
        assert suggester.saved[-1] == {"enabled": True, "history": 12, "model": "openai:gpt-4o"}


async def test_tui_settings_saves_all_three_fields_independently():
    # the three fields each write their own setting — editing one never clobbers another
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "skip_known", "target": "ru", "known": ["en"], "unknown": ["ja"]})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, translator=translator,
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#target-lang", Input).value = "ru"
        screen.query_one("#known-langs", Input).value = "ru, en"
        screen.query_one("#unknown-langs", Input).value = "ja, ko"
        await screen._save_translate_settings()
        # the model is applied via _apply_model_change (validate-then-commit), NOT through
        # set_settings, so it isn't part of this save; max 100 was prefilled on load.
        assert translator.saved[-1] == {
            "mode": "skip_known", "target": "ru", "known": ["ru", "en"], "unknown": ["ja", "ko"],
            "model": None, "max_messages": 100,
        }


async def test_tui_settings_rejects_bad_lang_code_without_saving():
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "skip_known", "target": "ru", "known": [], "unknown": []})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, translator=translator,
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#known-langs", Input).value = "ru, fr"  # fr unsupported
        await screen._save_translate_settings()
        assert translator.saved == []  # nothing persisted on a bad code


async def test_tui_settings_all_three_fields_tab_reachable_in_every_mode():
    # regression (#133): a disabled field drops out of Textual's focus_chain. None of the three
    # translation fields may ever be disabled — Tab must reach each of them in every mode.
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "all_unknown", "target": "ru", "known": ["ru"], "unknown": []})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, translator=translator,
        )
        app.push_screen(screen)
        await pilot.pause()
        for mode in ("off", "all_unknown", "skip_known", "only_unknown"):
            screen.query_one(f"#mode-{mode}").value = True
            await pilot.pause()
            chain_ids = [getattr(w, "id", None) for w in screen.focus_chain]
            for fid in ("target-lang", "known-langs", "unknown-langs"):
                field = screen.query_one(f"#{fid}", Input)
                assert field.disabled is False, f"{fid} disabled in mode {mode}"
                assert fid in chain_ids, f"{fid} not Tab-reachable in mode {mode}"


async def test_tui_settings_fields_have_persistent_border_titles():
    # each field carries a PERSISTENT, fixed border_title caption (visible even with a value typed),
    # not a placeholder that vanishes on input — the reported "can't tell which field is which".
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "skip_known", "target": "ru", "known": ["en"], "unknown": []})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, translator=translator,
        )
        app.push_screen(screen)
        await pilot.pause()
        captions = {
            "target-lang": "Мой язык (на что переводить)",
            "known-langs": "Не переводить",
            "unknown-langs": "Переводить (пусто = всё переводить)",
        }
        for fid, caption in captions.items():
            field = screen.query_one(f"#{fid}", Input)
            # styled legible (accent + bold), not the default grey blurred border
            assert field.styles.border_title_color is not None
            assert "bold" in str(field.styles.border_title_style)
            assert str(field.border_title) == caption
        # caption survives a typed value (the core of the fix)
        target = screen.query_one("#target-lang", Input)
        target.value = "ru"
        await pilot.pause()
        assert str(target.border_title) == "Мой язык (на что переводить)"


async def test_tui_settings_model_and_max_fields_present_and_loaded():
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({
        "mode": "all_unknown", "target": "ru", "model": "openai:glm-5.1", "max_messages": 250,
    })
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, translator=translator,
        )
        app.push_screen(screen)
        await pilot.pause()
        model_field = screen.query_one("#translate-model", Input)
        max_field = screen.query_one("#translate-max", Input)
        assert model_field.value == "openai:glm-5.1"
        assert max_field.value == "250"
        assert str(model_field.border_title) == "Модель для перевода"
        assert str(max_field.border_title) == "Сколько переводить за раз (Ctrl+T)"
        # both Tab-reachable (never disabled — #133 discipline)
        chain_ids = [getattr(w, "id", None) for w in screen.focus_chain]
        assert "translate-model" in chain_ids and "translate-max" in chain_ids


async def test_tui_settings_rejects_bad_max_without_saving():
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "all_unknown", "target": "ru"})
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AccountsScreen(
            profiles=store.list_profiles(), active="alice", store=store, translator=translator,
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#translate-max", Input).value = "0"  # < 1
        await screen._save_translate_settings()
        assert translator.saved == []  # nothing persisted
        screen.query_one("#translate-max", Input).value = "abc"  # not a number
        await screen._save_translate_settings()
        assert translator.saved == []


async def test_tui_translate_all_without_dialog_notifies():
    store = FakeSessionStore(["alice"])
    translator = StubTranslator({"mode": "all_unknown", "target": "ru"})
    app = MessengerTUI(
        client=TuiStubClient(), session_name="alice", session_store=store, translator=translator,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda msg, **kw: notes.append((msg, kw.get("severity")))
        app._current = None
        app.action_translate_all()
        await pilot.pause()
        assert any("диалог" in m.lower() for m, _ in notes)


async def test_tui_translate_all_without_translator_notifies():
    store = FakeSessionStore(["alice"])
    app = MessengerTUI(client=TuiStubClient(), session_name="alice", session_store=store)
    async with app.run_test() as pilot:
        await pilot.pause()
        notes = []
        app.notify = lambda msg, **kw: notes.append((msg, kw.get("severity")))
        app._current = 123
        app._translator = None
        app.action_translate_all()
        await pilot.pause()
        assert any("переводчик" in m.lower() for m, _ in notes)


class _BlockingTranslator(StubTranslator):
    """Holds translate_history until released, so a test can observe the in-progress status line."""

    def __init__(self, settings=None):
        super().__init__(settings)
        self.gate = asyncio.Event()

    async def translate_history(self, dialog_id, messages):
        await self.gate.wait()
        # mark every inbound message translated so the pass counts as a success
        return [
            m.model_copy(update={"translated_text": "ПЕРЕВОД"}) if not m.out else m
            for m in messages
        ]


async def test_tui_translate_all_shows_status_then_clears():
    store = FakeSessionStore(["alice"])
    translator = _BlockingTranslator({"mode": "all_unknown", "target": "ru", "max_messages": 100})
    app = MessengerTUI(
        client=TuiStubClient(), session_name="alice", session_store=store, translator=translator,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        app.action_translate_all()
        await pilot.pause()
        # while translate_history is blocked, the status container is mounted with BOTH a labelled
        # caption and the animated LoadingIndicator (the blinking dots, like history loading)
        status = app.query_one("#translate-status")
        label = status.query_one(Label)
        assert "Идёт перевод" in str(label.render())
        assert status.query(LoadingIndicator), "animated loading dots not shown during translation"
        # release the translator → status clears, bubbles appear WITH the translation rendered
        translator.gate.set()
        await pilot.pause()
        await pilot.pause()
        assert not app.query("#messages .translate-status")
        bubbles = app.query("MessageBubble")
        assert bubbles
        # regression: the re-mounted snapshot must show the TRANSLATED text, not the originals
        rendered = "\n".join(str(b.render()) for b in bubbles)
        assert "ПЕРЕВОД" in rendered, "Ctrl+T mounted untranslated bubbles"


async def test_tui_translate_all_aborts_when_dialog_already_switched():
    # regression: the Ctrl+T worker is scheduled, not inline. If the user opens another dialog
    # before the worker body runs, it must bail BEFORE touching the pane — not wipe the new
    # dialog's history nor mount a stale #translate-status into the wrong chat.
    store = FakeSessionStore(["alice"])
    translator = _BlockingTranslator({"mode": "all_unknown", "target": "ru", "max_messages": 100})
    app = MessengerTUI(
        client=TuiStubClient(), session_name="alice", session_store=store, translator=translator,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        bubbles_before = len(app.query("MessageBubble"))
        assert bubbles_before  # dialog 7 history is on screen
        # the user has switched to another dialog by the time the stale worker body runs
        app._current = 8
        await app._translate_whole_dialog(7)
        await pilot.pause()
        # the worker bailed early: no spinner mounted, dialog 7's pane untouched
        assert not app.query("#translate-status")
        assert len(app.query("MessageBubble")) == bubbles_before


async def test_tui_translate_all_aborts_when_dialog_switches_during_first_await():
    # regression: even past the entry guard, the dialog can switch DURING `await remove_children()`.
    # The post-await re-check must then abort before mounting a stale spinner.
    store = FakeSessionStore(["alice"])
    translator = _BlockingTranslator({"mode": "all_unknown", "target": "ru", "max_messages": 100})
    app = MessengerTUI(
        client=TuiStubClient(), session_name="alice", session_store=store, translator=translator,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._current = 7
        await app._show_history(7)
        await pilot.pause()
        pane = app.query_one("#messages", Vertical)
        original_remove = pane.remove_children

        async def remove_then_switch(*args, **kwargs):
            # simulate the user opening another dialog WHILE remove_children() is awaited
            app._current = 8
            return await original_remove(*args, **kwargs)

        pane.remove_children = remove_then_switch
        await app._translate_whole_dialog(7)
        await pilot.pause()
        # the post-remove_children guard fired: no stale spinner mounted for the old dialog
        assert not app.query("#translate-status")


async def test_tui_failed_model_change_does_not_persist_model_or_clear_cache(monkeypatch, tmp_path):
    # regression: a bad model must NOT be written to kv (and the cache must NOT be cleared) before
    # the probe/build succeeds — validate-then-commit.
    pytest.importorskip("deepagents")
    from tg_messenger.agent import factory as factory_mod
    from tg_messenger.agent.translate import (
        Translator,
        get_translate_model,
        get_user_lang,
        set_translate_model,
    )
    from tg_messenger.core.message_store import register_message_store_migrations
    from tg_messenger.core.storage import Storage

    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    await storage.connect()
    cleared = {"count": 0}
    try:
        await set_translate_model(storage, "openai:good")  # the working model already persisted

        async def fake_translate_fn(batch, lang, skip=(), only=()):
            return {}

        translator = Translator(storage=storage, translate_fn=fake_translate_fn, env={})

        # building the candidate model raises (bad name / missing key)
        async def boom(_storage, _model):
            raise RuntimeError("bad model")

        monkeypatch.setattr(factory_mod, "build_translator_with_probe", boom)

        # count cache-clears across the whole save path (every setter calls clear_all_translations)
        async def counting_clear(_s):
            cleared["count"] += 1

        monkeypatch.setattr(
            "tg_messenger.agent.translate.clear_all_translations", counting_clear
        )

        store = FakeSessionStore(["alice"])
        app = MessengerTUI(
            client=TuiStubClient(), session_name="alice", session_store=store, translator=translator,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = AccountsScreen(
                profiles=store.list_profiles(), active="alice", store=store, translator=translator,
            )
            app.push_screen(screen)
            await pilot.pause()
            # fill the other settings too — they must NOT be written when the new model is bad
            screen.query_one("#target-lang", Input).value = "en"
            screen.query_one("#known-langs", Input).value = "de"
            screen.query_one("#translate-model", Input).value = "openai:bad"
            # the REAL UI save path: a bad changed model must abort BEFORE set_settings, so neither
            # the cache nor any setting is touched (atomic validate-then-commit).
            await screen._save_translate_settings()
            assert await get_translate_model(storage, {}) == "openai:good"  # working model survives
            assert cleared["count"] == 0  # cache never cleared
            assert await get_user_lang(storage, {}) is None  # target NOT written
            # _validate_model is validation-only and returns None on a bad model (no side effects)
            assert await screen._validate_model("openai:bad") is None
            assert cleared["count"] == 0  # still no cache clear from the validation call
    finally:
        await storage.close()


async def test_tui_successful_model_change_commits_model_and_settings(monkeypatch, tmp_path):
    # happy path: a valid new model persists the model AND the other settings, then dismisses with
    # the rebuilt translator (atomic commit after validation).
    pytest.importorskip("deepagents")
    from tg_messenger.agent import factory as factory_mod
    from tg_messenger.agent.translate import (
        Translator,
        get_translate_model,
        get_user_lang,
    )
    from tg_messenger.core.message_store import register_message_store_migrations
    from tg_messenger.core.storage import Storage

    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    await storage.connect()
    try:
        async def fake_translate_fn(batch, lang, skip=(), only=()):
            return {}

        translator = Translator(storage=storage, translate_fn=fake_translate_fn, env={})
        new_translator = Translator(storage=storage, translate_fn=fake_translate_fn, env={})

        async def ok_build(_storage, _model):
            return new_translator

        monkeypatch.setattr(factory_mod, "build_translator_with_probe", ok_build)

        store = FakeSessionStore(["alice"])
        app = MessengerTUI(
            client=TuiStubClient(), session_name="alice", session_store=store, translator=translator,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            dismissed = {}
            screen = AccountsScreen(
                profiles=store.list_profiles(), active="alice", store=store, translator=translator,
            )
            screen.dismiss = lambda result=None: dismissed.__setitem__("result", result)
            app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#target-lang", Input).value = "en"
            screen.query_one("#translate-model", Input).value = "openai:new"
            await screen._save_translate_settings()
            # both the model and the other settings are persisted; dismissed with the new translator
            assert await get_translate_model(storage, {}) == "openai:new"
            assert await get_user_lang(storage, {}) == "en"
            assert dismissed.get("result") is new_translator
    finally:
        await storage.close()


# --- #144: TUI notifies once, with the reason, when the suggester is disabled ---


async def test_tui_notifies_when_suggester_disabled(monkeypatch):
    monkeypatch.setattr(
        "tg_messenger.agent.suggest.suggester_disabled_reason",
        lambda env=None: "TG_AGENT_MODEL is not set — expected 'provider:model'",
    )
    app = MessengerTUI(client=TuiStubClient())  # suggester=None by default
    notes: list[str] = []
    app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
    assert any("Суфлёр" in m and "TG_AGENT_MODEL" in m for m in notes)


async def test_tui_no_disabled_notice_when_suggester_wired(monkeypatch):
    monkeypatch.setattr(
        "tg_messenger.agent.suggest.suggester_disabled_reason",
        lambda env=None: "should-not-be-called",
    )

    class _Sugg:
        async def suggest(self, dialog_id):
            return ""

    app = MessengerTUI(client=TuiStubClient(), suggester=_Sugg())
    notes: list[str] = []
    app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
    assert not any("Суфлёр" in m for m in notes)


# --- #147: the 💡 hint renders with a suggester wired, and stays empty without one ---


async def test_tui_suggest_hint_renders_with_suggester():
    from tg_messenger.tui.app import SUGGEST_PREFIX

    class FixedSuggester:
        async def suggest(self, dialog_id):
            return "Привет! Как дела?"

    app = MessengerTUI(client=TuiStubClient(), suggester=FixedSuggester())
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7  # Ann is a DM (kind="dm")
        app._maybe_suggest(7)
        await _pause_until(
            pilot, lambda: SUGGEST_PREFIX in str(app.query_one("#suggestion", Static).render())
        )
        rendered = str(app.query_one("#suggestion", Static).render())
        assert rendered == f"{SUGGEST_PREFIX}Привет! Как дела?"
        assert app._pending_suggestion == "Привет! Как дела?"
        # #170: a draft is non-empty → the strip is shown.
        assert app.query_one("#suggestion", Static).display is True


async def test_tui_multiline_suggestion_renders_as_one_framed_block():
    # #170: a multi-line draft must read as ONE bordered block, not N prefix-less "bubbles".
    # We keep the newlines (the user chose the framed look) but verify the strip is bordered and
    # holds the full draft so it can never degrade back to stacked, unframed rows.
    from tg_messenger.tui.app import SUGGEST_PREFIX

    draft = "line one\nline two\nline three"

    class MultiLineSuggester:
        async def suggest(self, dialog_id):
            return draft

    app = MessengerTUI(client=TuiStubClient(), suggester=MultiLineSuggester())
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7  # Ann is a DM (kind="dm")
        app._maybe_suggest(7)
        await _pause_until(
            pilot, lambda: SUGGEST_PREFIX in str(app.query_one("#suggestion", Static).render())
        )
        strip = app.query_one("#suggestion", Static)
        rendered = str(strip.render())
        # the full multi-line draft is preserved (all three lines present)
        assert rendered == f"{SUGGEST_PREFIX}{draft}"
        assert "line two" in rendered and "line three" in rendered
        # shown, and bordered so the multi-row hint is one framed block (not N bare bubbles)
        assert strip.display is True
        assert strip.styles.border.top[0] != ""


async def test_tui_suggest_strip_empty_without_suggester():
    app = MessengerTUI(client=TuiStubClient())  # suggester=None
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7
        # pin the `_suggester is None` gate specifically: no suggest worker is even started
        # (not merely that the draft happens to be empty). Spy on run_worker for group "suggest".
        suggest_workers = []
        real_run_worker = app.run_worker

        def spy_run_worker(work, *a, **kw):
            if kw.get("group") == "suggest":
                suggest_workers.append(work)
            return real_run_worker(work, *a, **kw)

        app.run_worker = spy_run_worker  # type: ignore[method-assign]
        app._maybe_suggest(7)  # no-op without a suggester
        await pilot.pause()
        assert suggest_workers == []  # the gate returned BEFORE scheduling any work
        assert str(app.query_one("#suggestion", Static).render()) == ""
        assert app._pending_suggestion is None


# --- #155: Ctrl+G suggests a reply for the OPEN dialog on demand (history, no new incoming) ---


async def test_tui_ctrl_g_suggests_for_open_dm():
    from tg_messenger.tui.app import SUGGEST_PREFIX

    class FixedSuggester:
        async def suggest(self, dialog_id):
            return "Конечно, давай завтра!"

    app = MessengerTUI(client=TuiStubClient(), suggester=FixedSuggester())
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7  # Ann is a DM (kind="dm"); opened from history, no live incoming
        app.action_suggest_reply()
        await _pause_until(
            pilot, lambda: SUGGEST_PREFIX in str(app.query_one("#suggestion", Static).render())
        )
        assert app._pending_suggestion == "Конечно, давай завтра!"


# --- #158: thinking indicator, instant Ctrl+G reuse, dialog scope ---


async def test_tui_ctrl_g_shows_thinking_indicator_while_blocked():
    from tg_messenger.tui.app import SUGGEST_PREFIX, SUGGEST_THINKING

    gate = asyncio.Event()

    class BlockingSuggester:
        async def suggest(self, dialog_id):
            await gate.wait()  # hold the "LLM" until the test releases it
            return "готовый ответ"

    app = MessengerTUI(client=TuiStubClient(), suggester=BlockingSuggester())
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7
        app.action_suggest_reply()
        # the indicator is shown synchronously BEFORE the await resolves
        await _pause_until(
            pilot, lambda: str(app.query_one("#suggestion", Static).render()) == SUGGEST_THINKING
        )
        gate.set()  # release the LLM call
        await _pause_until(
            pilot, lambda: SUGGEST_PREFIX in str(app.query_one("#suggestion", Static).render())
        )
        assert app._pending_suggestion == "готовый ответ"


async def test_tui_ctrl_g_instant_when_pending_for_current_dialog():
    from tg_messenger.tui.app import SUGGEST_PREFIX

    class RecordingSuggester:
        def __init__(self):
            self.calls = []

        async def suggest(self, dialog_id):
            self.calls.append(dialog_id)
            return "fresh"

    suggester = RecordingSuggester()
    app = MessengerTUI(client=TuiStubClient(), suggester=suggester)
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7
        # a draft was pre-generated for dialog 7 (e.g. by the auto-path on an incoming message)
        app._pending_suggestion = "уже готов"
        app._pending_suggestion_dialog = 7
        suggest_workers = []
        real_run_worker = app.run_worker

        def spy_run_worker(work, *a, **kw):
            if kw.get("group") == "suggest":
                suggest_workers.append(work)
            return real_run_worker(work, *a, **kw)

        app.run_worker = spy_run_worker  # type: ignore[method-assign]
        app.action_suggest_reply()
        await pilot.pause()
        assert suggest_workers == []  # no LLM worker scheduled — instant
        assert suggester.calls == []  # the suggester was never asked again
        assert str(app.query_one("#suggestion", Static).render()) == f"{SUGGEST_PREFIX}уже готов"


async def test_tui_ctrl_g_cross_dialog_pending_not_shown_runs_worker():
    class RecordingSuggester:
        def __init__(self):
            self.calls = []

        async def suggest(self, dialog_id):
            self.calls.append(dialog_id)
            return "fresh for current"

    suggester = RecordingSuggester()
    app = MessengerTUI(client=TwoDmClient(), suggester=suggester)
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 8  # a different DM than the pending draft's dialog
        app._pending_suggestion = "draft for dialog 7"
        app._pending_suggestion_dialog = 7
        suggest_workers = []
        real_run_worker = app.run_worker

        def spy_run_worker(work, *a, **kw):
            if kw.get("group") == "suggest":
                suggest_workers.append(work)
            return real_run_worker(work, *a, **kw)

        app.run_worker = spy_run_worker  # type: ignore[method-assign]
        app.action_suggest_reply()
        await pilot.pause()
        assert len(suggest_workers) == 1  # stale cross-dialog draft NOT reused; a fresh call runs


async def test_tui_auto_path_does_not_show_thinking_indicator():
    from tg_messenger.tui.app import SUGGEST_THINKING

    gate = asyncio.Event()

    class BlockingSuggester:
        async def suggest(self, dialog_id):
            await gate.wait()
            return "draft"

    app = MessengerTUI(client=TuiStubClient(), suggester=BlockingSuggester())
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7
        app._maybe_suggest(7)  # the auto-path (incoming message), notify_empty/show_thinking False
        await pilot.pause()
        # the auto-path never flashes "⏳" — the strip stays empty until the draft lands
        assert str(app.query_one("#suggestion", Static).render()) != SUGGEST_THINKING
        gate.set()


async def test_tui_ctrl_g_blocked_by_nonempty_composer_points_at_escape():
    # #155 follow-up: a non-empty composer blocks Ctrl+G (on_input_changed would wipe the hint and
    # clobbering typed text is worse). The toast tells the user to clear it with Escape; no draft
    # is generated and the typed text is left intact.
    class RecordingSuggester:
        def __init__(self):
            self.calls = []

        async def suggest(self, dialog_id):
            self.calls.append(dialog_id)
            return "draft"

    suggester = RecordingSuggester()
    app = MessengerTUI(client=TuiStubClient(), suggester=suggester)
    notes: list[str] = []
    app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7  # Ann, a DM
        app.query_one("#composer", Input).value = "недописанное"
        app.action_suggest_reply()
        await pilot.pause()
        assert app.query_one("#composer", Input).value == "недописанное"  # text untouched
    assert suggester.calls == []  # blocked before the suggester is asked
    assert any("Esc" in m for m in notes)  # the toast points at Escape


async def test_tui_escape_clears_composer_when_composer_focused():
    # #156: Escape clears the composer when the composer is focused, so Ctrl+G has a clean field.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7
        composer = app.query_one("#composer", Input)
        composer.value = "черновик"
        composer.focus()
        await pilot.pause()
        app.action_clear_search()
        await pilot.pause()
        assert composer.value == ""


async def test_tui_escape_clearing_search_preserves_composer_draft():
    # #156 regression (Codex): Escape pressed to clear the SEARCH filter must NOT wipe a reply
    # typed in the composer — that draft is persisted and unrecoverable on a dialog switch.
    app = MessengerTUI(client=TuiStubClient())
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7
        search = app.query_one("#search", Input)
        composer = app.query_one("#composer", Input)
        composer.value = "важный черновик"
        search.value = "ann"
        search.focus()  # focus is on the SEARCH box, not the composer
        await pilot.pause()
        app.action_clear_search()
        await pilot.pause()
        assert search.value == ""  # search cleared
        assert composer.value == "важный черновик"  # draft preserved — no data loss


async def test_tui_ctrl_g_notifies_without_suggester():
    app = MessengerTUI(client=TuiStubClient())  # suggester=None
    notes: list[str] = []
    app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
    suggest_workers = []
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7
        real_run_worker = app.run_worker

        def spy_run_worker(work, *a, **kw):
            if kw.get("group") == "suggest":
                suggest_workers.append(work)
            return real_run_worker(work, *a, **kw)

        app.run_worker = spy_run_worker  # type: ignore[method-assign]
        app.action_suggest_reply()
        await pilot.pause()
    assert suggest_workers == []  # no work scheduled
    assert any("Суфлёр" in m for m in notes)  # the user got feedback, not silence


async def test_tui_ctrl_g_dm_only_notifies_in_group():
    class RecordingSuggester:
        def __init__(self):
            self.calls = []

        async def suggest(self, dialog_id):
            self.calls.append(dialog_id)
            return "draft"

    suggester = RecordingSuggester()
    app = MessengerTUI(client=TuiStubClient(), suggester=suggester)
    notes: list[str] = []
    app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        # -100200 ("Devs") is a real group in the stub dialog list, so the REAL DM-detection
        # (_kind_for_rendering → "group") must reject it — no monkeypatch, faithful end-to-end check.
        app._current = -100200
        app.action_suggest_reply()
        await pilot.pause()
    assert suggester.calls == []  # never asked for a draft in a group
    assert any("личных сообщениях" in m for m in notes)


async def test_tui_ctrl_g_notifies_on_empty_draft():
    # an explicit Ctrl+G that yields no draft must give feedback (silence reads as "key did
    # nothing"); the auto-path stays a silent no-op (notify_empty defaults to False).
    class EmptySuggester:
        async def suggest(self, dialog_id):
            return ""

    app = MessengerTUI(client=TuiStubClient(), suggester=EmptySuggester())
    notes: list[str] = []
    app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _pause_until(pilot, lambda: app._started)
        app._current = 7  # Ann, a DM
        app.action_suggest_reply()
        await _pause_until(pilot, lambda: any("не предложил" in m for m in notes))
        assert app._pending_suggestion is None  # nothing pending on an empty draft
        assert str(app.query_one("#suggestion", Static).render()) == ""
