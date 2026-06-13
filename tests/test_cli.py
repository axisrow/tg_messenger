import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from tests.conftest import make_sent_code
from tg_messenger.cli import main as cli_main
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.models import (
    Dialog,
    IncomingEvent,
    MediaRef,
    Message,
    MessagesDeletedEvent,
    OutgoingEvent,
    ReactionEvent,
    User,
)


class StubClient:
    def __init__(self, **kw):
        self.sent = []
        self.forwarded = []
        self.edited = []
        self.deleted_calls = []
        self.read_acks = []
        self.reactions = []
        self.downloaded = []
        self.searched = []
        self.history_items = None
        self.connected = False
        self.authorized = True
        self.logged_out = False
        self.listen_interrupt = True  # emulate Ctrl+C after the first event

    async def is_authorized(self):
        return self.authorized

    async def log_out(self):
        self.logged_out = True
        return True

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def listen(self):
        yield IncomingEvent(
            dialog_id=7,
            message=Message(id=10, dialog_id=7, sender_id=7, out=False, text="ping",
                            date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        )
        if self.listen_interrupt:
            raise KeyboardInterrupt
        await asyncio.Event().wait()

    async def listen_reads(self):
        from tg_messenger.core.models import MessageReadEvent

        yield MessageReadEvent(dialog_id=7, max_id=10, outbox=True)
        await asyncio.Event().wait()

    async def dialogs(self, dm_only=True):
        dms = [Dialog(id=7, title="Ann", username="ann", unread=2)]
        if dm_only:
            return dms
        # повторяет контракт core: dm_only=False — все диалоги с kind и marked id
        return dms + [
            Dialog(id=-100200, title="Devs", kind="group"),
            Dialog(id=-100123, title="News", kind="channel"),
            Dialog(id=9, title="HelperBot", kind="bot"),
        ]

    async def group_dialogs(self):
        return [d for d in await self.dialogs(dm_only=False) if d.kind != "dm"]

    async def history(self, peer, limit=50, offset_id=0):
        if self.history_items is not None:
            return self.history_items
        return [
            Message(id=1, dialog_id=peer, sender_id=peer, out=False, text="hi",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        ]

    async def search_messages(self, peer, query, limit=20):
        self.searched.append((peer, query, limit))
        return [Message(id=5, dialog_id=peer, sender_id=peer, out=False, text="found-it",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc))]

    async def download_message_media(self, peer, message_id, dest):
        self.downloaded.append((message_id, str(dest)))
        return str(dest)

    async def send_text(self, peer, text, reply_to=None, schedule=None):
        self.sent.append((peer, text, reply_to, schedule))
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def forward(self, from_peer, message_ids, to_peer):
        self.forwarded.append((from_peer, list(message_ids), to_peer))
        return [Message(id=m, dialog_id=to_peer, sender_id=1, out=True, text="fwd",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc)) for m in message_ids]

    async def edit_text(self, peer, message_id, text):
        self.edited.append((peer, message_id, text))
        return Message(id=message_id, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def delete_messages(self, peer, message_ids, revoke=True):
        self.deleted_calls.append((peer, list(message_ids), revoke))

    async def mark_read(self, peer, max_id=None):
        self.read_acks.append((peer, max_id))

    async def send_media(self, peer, file_path, *, caption=None, voice_note=False,
                         video_note=False, force_document=False):
        self.sent.append((peer, "file", str(file_path), caption))
        self.media_kwargs = {"voice_note": voice_note, "video_note": video_note,
                             "force_document": force_document}
        return Message(id=3, dialog_id=peer, sender_id=1, out=True, text=caption,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def send_reaction(self, peer, message_id, emoticon):
        self.reactions.append((peer, message_id, emoticon))

    async def get_me(self):
        return User(id=1, first_name="Me")

    async def entity_title(self, peer):
        return "My Group"

    # username (#22): override .occupied to mark names taken
    occupied: set = frozenset()
    set_username_to = None
    cleared = False

    async def check_username(self, username):
        return username not in self.occupied

    async def set_username(self, username):
        if username in self.occupied:
            raise ValueError(f"username already taken: {username}")
        self.set_username_to = username

    async def clear_username(self):
        self.cleared = True

    async def listen_outgoing(self):
        yield OutgoingEvent(
            dialog_id=-100123,
            message=Message(id=10, dialog_id=-100123, sender_id=1, out=True,
                            text="удалят меня", date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        )
        await asyncio.Event().wait()

    async def listen_reactions(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    admin = True  # default: we can moderate every chat

    async def is_admin(self, peer):
        return self.admin

    async def moderation_rights(self, peer):
        return {"delete_messages": self.admin, "ban_users": self.admin}

    async def listen_all(self):
        if False:  # pragma: no cover — empty async generator, then idle
            yield None
        raise KeyboardInterrupt  # эмуляция Ctrl+C (паттерн listen_interrupt)

    async def listen_chat_actions(self):
        if False:  # pragma: no cover
            yield None
        await asyncio.Event().wait()

    async def listen_deleted(self):
        for _ in range(10):
            await asyncio.sleep(0)  # дать outgoing-потоку закэшировать сообщение
        yield MessagesDeletedEvent(chat_id=-100123, message_ids=[10])
        for _ in range(10):
            await asyncio.sleep(0)  # дать watcher'у отправить уведомление
        raise KeyboardInterrupt  # эмуляция Ctrl+C (паттерн listen_interrupt)


class DummyMessageStore:
    def __init__(self, client):
        self.client = client
        self.closed = False

    async def connect(self):
        pass

    async def close(self):
        self.closed = True

    async def history(self, peer, limit=50):
        return await self.client.history(peer, limit=limit)

    async def run(self):
        await asyncio.Event().wait()


def _patch_message_store(monkeypatch):
    stores = []

    def fake_make_message_store(client, **kw):
        store = DummyMessageStore(client)
        stores.append((store, kw))
        return store, object()

    monkeypatch.setattr(cli_main, "make_message_store", fake_make_message_store)
    monkeypatch.setattr(cli_main, "make_optional_translator", lambda storage: None)
    monkeypatch.setattr(cli_main, "make_optional_outbound", lambda store, storage: None)
    return stores


@pytest.fixture
def runner(monkeypatch):
    stub = StubClient()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    _patch_message_store(monkeypatch)
    return CliRunner(), stub


def test_dialogs_lists_dms(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code == 0
    assert "Ann" in result.output
    assert "7" in result.output


def test_dialogs_prints_id_next_to_title(runner):
    # цикл 63: id виден в выводе рядом с заголовком (id<TAB>title)
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code == 0
    assert "7\tAnn" in result.output


def test_dialogs_groups_flag_lists_non_dm(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs", "--groups"])
    assert result.exit_code == 0
    assert "Devs" in result.output
    assert "-100200" in result.output  # marked id — пригоден для read/send
    for kind in ("[group]", "[channel]", "[bot]"):
        assert kind in result.output
    assert "Ann" not in result.output  # DM не смешиваются с группами


# --- цикл 65: поиск в CLI (dialogs --find, команда search) ---


def test_dialogs_find_filters_by_query(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs", "--find", "ann"])
    assert result.exit_code == 0
    assert "Ann" in result.output


def test_dialogs_find_no_match_is_empty(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs", "--find", "zzznope"])
    assert result.exit_code == 0
    assert "Ann" not in result.output


def test_dialogs_find_works_with_groups(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs", "--groups", "--find", "Devs"])
    assert result.exit_code == 0
    assert "Devs" in result.output
    assert "News" not in result.output


def test_search_command_calls_search_messages(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["search", "7", "hi"])
    assert result.exit_code == 0, result.output
    assert stub.searched == [(7, "hi", 20)]
    assert "found-it" in result.output


def test_search_command_passes_limit(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["search", "7", "hi", "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert stub.searched == [(7, "hi", 3)]


def test_read_prints_history(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["read", "7"])
    assert result.exit_code == 0
    assert "hi" in result.output


def test_send_calls_client(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["send", "7", "hello"])
    assert result.exit_code == 0
    assert stub.sent == [(7, "hello", None, None)]


def test_send_file_uses_send_media(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "caption", "--file", str(f)])
    assert result.exit_code == 0
    assert stub.sent[-1] == (7, "file", str(f), "caption")


def test_send_file_caption_option(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "--file", str(f), "--caption", "cap"])
    assert result.exit_code == 0, result.output
    assert stub.sent[-1] == (7, "file", str(f), "cap")


def test_send_file_voice_flag(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "note.ogg"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "--file", str(f), "--voice"])
    assert result.exit_code == 0, result.output
    assert stub.media_kwargs == {"voice_note": True, "video_note": False,
                                 "force_document": False}


def test_send_file_video_note_flag(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "--file", str(f), "--video-note"])
    assert result.exit_code == 0, result.output
    assert stub.media_kwargs["video_note"] is True


def test_send_file_as_file_flag(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "--file", str(f), "--as-file"])
    assert result.exit_code == 0, result.output
    assert stub.media_kwargs["force_document"] is True


def test_send_file_conflicting_flags_error(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "--file", str(f), "--voice", "--as-file"])
    assert result.exit_code != 0
    assert stub.sent == []


def test_react_command_calls_client(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["react", "7", "10", "👍"])
    assert result.exit_code == 0, result.output
    assert stub.reactions == [(7, 10, "👍")]
    assert "reacted." in result.output


# --- цикл 80: reply/forward/edit/delete/read команды ---


def test_send_reply_to_passed(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["send", "7", "re", "--reply-to", "42"])
    assert result.exit_code == 0, result.output
    assert stub.sent == [(7, "re", 42, None)]


def test_forward_command_calls_client(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["forward", "7", "1,2", "8"])
    assert result.exit_code == 0, result.output
    assert stub.forwarded == [(7, [1, 2], 8)]


def test_forward_command_rejects_bad_ids(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["forward", "7", "1,x", "8"])
    assert result.exit_code != 0
    assert "id" in result.output.lower()


def test_edit_command_calls_client(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["edit", "7", "5", "fixed"])
    assert result.exit_code == 0, result.output
    assert stub.edited == [(7, 5, "fixed")]


def test_delete_command_revokes_by_default(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["delete", "7", "1,2"])
    assert result.exit_code == 0, result.output
    assert stub.deleted_calls == [(7, [1, 2], True)]


def test_delete_command_for_me_keeps_revoke_false(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["delete", "7", "1", "--for-me"])
    assert result.exit_code == 0, result.output
    assert stub.deleted_calls == [(7, [1], False)]


def test_delete_command_for_me_rejects_channel_marked_id(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["delete", "--for-me", "--", "-1000000000123", "1"])
    assert result.exit_code != 0
    assert "--for-me is not supported" in result.output
    assert stub.deleted_calls == []


def test_delete_command_rejects_bad_ids(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["delete", "7", "nope"])
    assert result.exit_code != 0
    assert "id" in result.output.lower()


def test_mark_read_command_marks_read(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["mark-read", "7"])
    assert result.exit_code == 0, result.output
    assert stub.read_acks == [(7, None)]


def test_flood_wait_friendly_message(runner, monkeypatch):
    r, stub = runner

    async def boom(peer, text, reply_to=None):
        raise HandledFloodWaitError("send_text", 9999)

    monkeypatch.setattr(stub, "send_text", boom)
    result = r.invoke(cli_main.cli, ["send", "7", "hi"])
    assert result.exit_code != 0 or "flood" in result.output.lower()


@pytest.fixture
def serve_spy(monkeypatch):
    calls = {"uvicorn": [], "build": []}
    client = object()
    suggester = object()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    monkeypatch.setattr(cli_main, "make_optional_suggester", lambda c, **kw: suggester)
    _patch_message_store(monkeypatch)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls["uvicorn"].append(kw))
    monkeypatch.setattr(
        "tg_messenger.web.app.build_app",
        lambda **kw: calls["build"].append(kw) or object(),
    )
    return calls


def test_serve_defaults_to_8090(serve_spy, monkeypatch):
    monkeypatch.delenv("TG_WEB_PORT", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0
    assert serve_spy["uvicorn"][0]["port"] == 8090


def test_serve_reads_env_port(serve_spy, monkeypatch):
    monkeypatch.setenv("TG_WEB_PORT", "9099")
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0
    assert serve_spy["uvicorn"][0]["port"] == 9099


def test_serve_flag_overrides_env(serve_spy, monkeypatch):
    monkeypatch.setenv("TG_WEB_PORT", "9099")
    result = CliRunner().invoke(cli_main.cli, ["serve", "--port", "1234"])
    assert result.exit_code == 0
    assert serve_spy["uvicorn"][0]["port"] == 1234


def test_read_download_saves_media(runner, tmp_path):
    r, stub = runner
    stub.history_items = [
        Message(id=1, dialog_id=7, sender_id=7, out=False, text="hi",
                date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        Message(id=2, dialog_id=7, sender_id=7, out=False, text=None,
                date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                media=MediaRef(kind="other", downloadable=True)),
    ]
    result = r.invoke(cli_main.cli, ["read", "7", "--download", str(tmp_path)])
    assert result.exit_code == 0
    # only the media message (id=2) is downloaded
    assert [mid for mid, _ in stub.downloaded] == [2]


def test_read_download_creates_directory(runner, tmp_path):
    r, stub = runner
    stub.history_items = [
        Message(id=2, dialog_id=7, sender_id=7, out=False, text=None,
                date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                media=MediaRef(kind="other", downloadable=True)),
    ]
    target = tmp_path / "dl" / "nested"
    result = r.invoke(cli_main.cli, ["read", "7", "--download", str(target)])
    assert result.exit_code == 0
    assert target.is_dir()
    assert [mid for mid, _ in stub.downloaded] == [2]


def test_listen_stops_and_disconnects_on_ctrl_c(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["listen"])
    assert result.exit_code == 0
    assert "ping" in result.output
    assert stub.connected is False


def test_chat_sends_and_disconnects_on_eof(runner):
    r, stub = runner
    stub.listen_interrupt = False  # printer just idles; EOF on stdin ends the REPL
    result = r.invoke(cli_main.cli, ["chat", "7"], input="hello\n")
    assert result.exit_code == 0
    assert (7, "hello", None, None) in stub.sent
    assert stub.connected is False


def test_chat_react_command_does_not_send_text(runner):
    r, stub = runner
    stub.listen_interrupt = False
    result = r.invoke(cli_main.cli, ["chat", "7"], input="/react 10 👍\n")
    assert result.exit_code == 0, result.output
    assert stub.reactions == [(7, 10, "👍")]
    assert stub.sent == []


def test_chat_outbound_variant_sends_pick(runner, monkeypatch):
    r, stub = runner
    stub.listen_interrupt = False

    class FakeStorage:
        async def get_value(self, key):
            return "ru" if key == "user_lang" else None

    class FakeStore(DummyMessageStore):
        def __init__(self, client):
            super().__init__(client)
            self.storage = FakeStorage()
            self.recorded = []

        async def record_outgoing(self, dialog_id, message, *, source_text, source_lang):
            self.recorded.append((dialog_id, message.text, source_text, source_lang))

    class FakeOutbound:
        async def applies(self, dialog_id, text, *, telegram_lang_code=None):
            return "en"

        async def variants(self, dialog_id, text, target_lang):
            return ["hi", "hello", "hey"]

    store = FakeStore(stub)
    monkeypatch.setattr(cli_main, "make_message_store", lambda client, **kw: (store, store.storage))
    monkeypatch.setattr(cli_main, "make_optional_outbound", lambda s, storage: FakeOutbound())
    result = r.invoke(cli_main.cli, ["chat", "7"], input="привет\n2\n")
    assert result.exit_code == 0, result.output
    assert (7, "hello", None, None) in stub.sent
    assert store.recorded == [(7, "hello", "привет", "ru")]
    assert "↳ привет" in result.output


def test_chat_lang_command_is_handled_before_outbound(runner, monkeypatch):
    r, stub = runner
    stub.listen_interrupt = False

    class FakeStorage:
        def __init__(self):
            self.values = []
            self.executed = []

        async def get_value(self, key):
            return None

        async def set_value(self, key, value):
            self.values.append((key, value))

        async def execute(self, sql, params=()):
            self.executed.append((sql, params))

    class FakeStore(DummyMessageStore):
        def __init__(self, client):
            super().__init__(client)
            self.storage = FakeStorage()

    class FakeOutbound:
        def __init__(self, storage):
            self.storage = storage
            self.applies_calls = []

        async def applies(self, dialog_id, text, *, telegram_lang_code=None):
            self.applies_calls.append((dialog_id, text))
            return "en"

        async def variants(self, dialog_id, text, target_lang):
            return ["translated"]

    store = FakeStore(stub)
    outbound = FakeOutbound(store.storage)
    monkeypatch.setattr(cli_main, "make_message_store", lambda client, **kw: (store, store.storage))
    monkeypatch.setattr(cli_main, "make_optional_outbound", lambda s, storage: outbound)
    result = r.invoke(cli_main.cli, ["chat", "7"], input="/lang en\n")
    assert result.exit_code == 0, result.output
    assert stub.sent == []
    assert outbound.applies_calls == []
    assert store.storage.values == [
        ("dialog_lang_7", {"lang": "en", "source": "manual"}),
    ]
    assert "language setting saved" in result.output


def test_chat_outbound_timeout_sends_original(runner, monkeypatch):
    r, stub = runner
    stub.listen_interrupt = False

    class FakeOutbound:
        async def applies(self, dialog_id, text, *, telegram_lang_code=None):
            raise TimeoutError

    monkeypatch.setattr(cli_main, "make_optional_outbound", lambda s, storage: FakeOutbound())
    result = r.invoke(cli_main.cli, ["chat", "7"], input="привет\n")
    assert result.exit_code == 0, result.output
    assert (7, "привет", None, None) in stub.sent
    assert "translation timed out" in result.output


def test_dialog_lang_show_set_and_off(monkeypatch, tmp_path):
    from tg_messenger.core.storage import Storage

    def fake_make_storage(profile="default"):
        return Storage(tmp_path / f"{profile}.db")

    monkeypatch.setattr(cli_main, "make_storage", fake_make_storage)
    runner = CliRunner()
    set_result = runner.invoke(cli_main.cli, ["dialog-lang", "7", "en", "--off"])
    show_result = runner.invoke(cli_main.cli, ["dialog-lang", "7"])
    auto_result = runner.invoke(cli_main.cli, ["dialog-lang", "7", "--auto", "--on"])
    assert set_result.exit_code == 0, set_result.output
    assert "lang=en" in set_result.output and "outbound=off" in set_result.output
    assert show_result.exit_code == 0, show_result.output
    assert "source=manual" in show_result.output
    assert auto_result.exit_code == 0, auto_result.output
    assert "lang=unset" in auto_result.output and "outbound=on" in auto_result.output


def test_lang_rejects_unsupported_code(monkeypatch, tmp_path):
    from tg_messenger.core.storage import Storage

    def fake_make_storage(profile="default"):
        return Storage(tmp_path / f"{profile}.db")

    monkeypatch.setattr(cli_main, "make_storage", fake_make_storage)
    runner = CliRunner()

    result = runner.invoke(cli_main.cli, ["lang", "fr"])
    show_result = runner.invoke(cli_main.cli, ["lang"])

    assert result.exit_code != 0
    assert "invalid language code" in result.output
    assert "fr" not in result.output
    assert show_result.exit_code == 0, show_result.output
    assert "unset" in show_result.output


def test_dialog_lang_rejects_unsupported_code(monkeypatch, tmp_path):
    from tg_messenger.core.storage import Storage

    def fake_make_storage(profile="default"):
        return Storage(tmp_path / f"{profile}.db")

    monkeypatch.setattr(cli_main, "make_storage", fake_make_storage)
    runner = CliRunner()

    result = runner.invoke(cli_main.cli, ["dialog-lang", "7", "fr"])
    show_result = runner.invoke(cli_main.cli, ["dialog-lang", "7"])

    assert result.exit_code != 0
    assert "invalid language code" in result.output
    assert "fr" not in result.output
    assert show_result.exit_code == 0, show_result.output
    assert "lang=unset" in show_result.output


def test_chat_prints_own_message_sent_from_another_device(runner, monkeypatch):
    """chat shows our OWN message (out) sent elsewhere for the open dialog only."""
    r, stub = runner
    stub.listen_interrupt = False

    async def outgoing():
        # one for the open dialog (id=7) — must print; one for another (id=9) — must not
        date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        yield OutgoingEvent(dialog_id=7, message=Message(
            id=50, dialog_id=7, sender_id=1, out=True, text="с телефона", date=date))
        yield OutgoingEvent(dialog_id=9, message=Message(
            id=51, dialog_id=9, sender_id=1, out=True, text="в другой чат", date=date))
        await asyncio.Event().wait()

    monkeypatch.setattr(stub, "listen_outgoing", outgoing)
    result = r.invoke(cli_main.cli, ["chat", "7"], input="")  # EOF immediately; just watch
    assert result.exit_code == 0
    assert "→ с телефона" in result.output
    assert "в другой чат" not in result.output  # другой диалог


def test_chat_prints_reactions_for_open_dialog(runner, monkeypatch):
    r, stub = runner
    stub.listen_interrupt = False

    async def reactions():
        yield ReactionEvent(dialog_id=7, message_id=10, emoticon="👍")
        yield ReactionEvent(dialog_id=9, message_id=11, emoticon="❤️")
        yield ReactionEvent(dialog_id=7, message_id=12, emoticon=None)
        await asyncio.Event().wait()

    monkeypatch.setattr(stub, "listen_reactions", reactions)
    result = r.invoke(cli_main.cli, ["chat", "7"], input="")
    assert result.exit_code == 0, result.output
    assert "* reaction [10]: 👍" in result.output
    assert "* reaction [12]: <custom>" in result.output
    assert "❤️" not in result.output


def test_chat_does_not_echo_back_our_own_input(runner, monkeypatch):
    """A line we type isn't printed back when its outgoing echo arrives (dedup by id)."""
    r, stub = runner
    stub.listen_interrupt = False
    sent_gate = asyncio.Event()

    async def send_text(peer, text, reply_to=None, schedule=None):
        stub.sent.append((peer, text, reply_to, schedule))
        sent_gate.set()  # the echo may only arrive AFTER we've recorded the id
        return Message(id=99, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def outgoing():
        # echo the same id=99 send_text returns — but only once it has been sent
        await sent_gate.wait()
        yield OutgoingEvent(dialog_id=7, message=Message(
            id=99, dialog_id=7, sender_id=1, out=True, text="hello",
            date=datetime(2024, 1, 1, tzinfo=timezone.utc)))
        await asyncio.Event().wait()

    monkeypatch.setattr(stub, "listen_outgoing", outgoing)
    monkeypatch.setattr(stub, "send_text", send_text)
    result = r.invoke(cli_main.cli, ["chat", "7"], input="hello\n")
    assert result.exit_code == 0
    # our own line must NOT be echoed back via the outgoing printer
    assert "→ hello" not in result.output


class FakeInnerLoginClient:
    """Stands in for the raw Telethon client used by LoginFlow."""

    def __init__(self, sign_in_error=None, send_code_error=None,
                 with_next_type=True, resend_error=None):
        self.sign_in_error = sign_in_error
        self.send_code_error = send_code_error
        self.resend_error = resend_error
        self.with_next_type = with_next_type  # Telegram offered a fallback channel?
        self.signed_in = []
        self.code_requests = []
        self.resends = 0

    async def send_code_request(self, phone):
        self.code_requests.append(phone)
        if self.send_code_error is not None:
            raise self.send_code_error
        return make_sent_code("App", "h", next_kind="Sms" if self.with_next_type else None)

    async def __call__(self, request):
        if self.resend_error is not None:
            raise self.resend_error
        self.resends += 1
        return make_sent_code("Sms", "h2")

    async def sign_in(self, phone=None, code=None, password=None, **kw):
        if code is not None and self.sign_in_error is not None:
            raise self.sign_in_error
        self.signed_in.append({"code": code, "password": password})
        return object()


class LoginStubClient:
    def __init__(self, inner):
        self._client = inner
        self.connected = False
        self.saved = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def save_session(self):
        self.saved = True


def test_login_wrong_code_fails_without_2fa_prompt(monkeypatch):
    from telethon.errors import PhoneCodeInvalidError

    inner = FakeInnerLoginClient(sign_in_error=PhoneCodeInvalidError(None))
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+10000000000\n123\n")
    assert result.exit_code != 0
    assert "2FA" not in result.output
    assert stub.saved is False


def test_login_says_where_code_was_sent(monkeypatch):
    inner = FakeInnerLoginClient()
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+10000000000\n123\n")
    assert result.exit_code == 0
    assert "Telegram app" in result.output  # код ушёл в приложение, не по SMS


def test_login_invalid_phone_friendly_error(monkeypatch):
    from telethon.errors.rpcerrorlist import PhoneNumberInvalidError

    inner = FakeInnerLoginClient(send_code_error=PhoneNumberInvalidError(None))
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+999\n")
    assert result.exit_code != 0
    assert "Could not send code" in result.output
    assert "Traceback" not in result.output
    assert stub.connected is False


def test_login_empty_code_resends_via_next_channel(monkeypatch):
    inner = FakeInnerLoginClient()
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    # phone, empty code (= resend), then the real code
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+10000000000\n\n123\n")
    assert result.exit_code == 0
    assert inner.resends == 1
    assert "SMS" in result.output  # resent code went via SMS
    assert inner.signed_in[-1]["code"] == "123"
    assert stub.saved is True


def test_login_empty_code_without_fallback_resends_same_channel(monkeypatch):
    # Telegram offered no next_type (e.g. +86 numbers): empty Enter must do a
    # fresh send_code (same channel) — mirroring tg_content_factory's web
    # "Отправить код повторно" — and must NOT call ResendCodeRequest.
    inner = FakeInnerLoginClient(with_next_type=False)
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+10000000000\n\n123\n")
    assert result.exit_code == 0
    assert inner.resends == 0
    assert len(inner.code_requests) == 2
    assert inner.signed_in[-1]["code"] == "123"
    assert stub.saved is True


def test_login_app_without_fallback_mentions_service_chat(monkeypatch):
    inner = FakeInnerLoginClient(with_next_type=False)
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+10000000000\n123\n")
    assert result.exit_code == 0
    assert "service chat" in result.output
    assert "777000" in result.output


def test_login_resend_failure_does_not_abort(monkeypatch):
    from telethon.errors import SendCodeUnavailableError

    inner = FakeInnerLoginClient(resend_error=SendCodeUnavailableError(None))
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    # empty Enter -> resend fails -> login keeps waiting for the original code
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+10000000000\n\n123\n")
    assert result.exit_code == 0
    # a short human message instead of the raw telethon paragraph
    assert "previous code is still valid" in result.output
    assert "flash-call" not in result.output  # telethon's verbose text stays out
    assert "Traceback" not in result.output
    assert inner.signed_in[-1]["code"] == "123"
    assert stub.saved is True


def test_login_other_resend_errors_show_short_reason(monkeypatch):
    from telethon.errors.rpcerrorlist import PhoneCodeExpiredError

    inner = FakeInnerLoginClient(resend_error=PhoneCodeExpiredError(None))
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["login"], input="+10000000000\n\n123\n")
    assert result.exit_code == 0
    assert "Could not resend code" in result.output
    assert stub.saved is True


def test_login_2fa_prompts_password(monkeypatch):
    from telethon.errors import SessionPasswordNeededError

    inner = FakeInnerLoginClient(sign_in_error=SessionPasswordNeededError(None))
    stub = LoginStubClient(inner)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(
        cli_main.cli, ["login"], input="+10000000000\n123\nhunter2\n"
    )
    assert result.exit_code == 0
    assert inner.signed_in[-1]["password"] == "hunter2"
    assert stub.saved is True


def test_dialogs_without_login_gives_hint_not_traceback(runner):
    r, stub = runner
    stub.authorized = False
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code != 0
    assert "tg-messenger login" in result.output
    assert "Traceback" not in result.output
    assert stub.connected is False  # client got disconnected


def test_listen_without_login_gives_hint(runner):
    r, stub = runner
    stub.authorized = False
    result = r.invoke(cli_main.cli, ["listen"])
    assert result.exit_code != 0
    assert "tg-messenger login" in result.output
    assert stub.connected is False


# --- Цикл 30: команда watch (бэкап удалённых сообщений) ---


def test_watch_notifies_saved_messages(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["watch"])
    assert result.exit_code == 0
    assert "Watching" in result.output
    (peer, text, _reply, _schedule), = stub.sent
    assert peer == 1  # Saved Messages = собственный id
    assert "удалят меня" in text
    assert "My Group" in text
    assert "stopped." in result.output
    assert stub.connected is False  # disconnect в finally


def test_watch_without_login_gives_hint(runner):
    r, stub = runner
    stub.authorized = False
    result = r.invoke(cli_main.cli, ["watch"])
    assert result.exit_code != 0
    assert "tg-messenger login" in result.output
    assert stub.connected is False


# --- Цикл 89: команда moderate + moderate-rules ---


@pytest.fixture
def mod_runner(monkeypatch, tmp_path):
    """CLI runner whose moderation storage is a fresh tmp SQLite db."""
    from tg_messenger.core.moderation import register_moderation_migrations
    from tg_messenger.core.storage import Storage

    stub = StubClient()
    seen: dict[str, list[str]] = {"clients": [], "storages": []}

    def _make_client(**kw):
        seen["clients"].append(kw.get("session_name", "default"))
        return stub

    def _make_storage(profile="default"):
        seen["storages"].append(profile)
        return Storage(tmp_path / f"{profile}.db")

    monkeypatch.setattr(cli_main, "make_client", _make_client)
    # register_moderation_migrations is called by the CLI; bare Storage is fine
    monkeypatch.setattr(cli_main, "make_storage", _make_storage)
    return CliRunner(), stub, tmp_path, register_moderation_migrations, seen


_RULE_JSON = """{
  "chat_id": -100200,
  "name": "no-spam",
  "conditions": {"pattern": "spam"},
  "actions": {"delete": true}
}"""


def test_moderate_rules_add_list_remove(mod_runner, tmp_path):
    r, stub, _tp, _, _seen = mod_runner
    rule_file = tmp_path / "rule.json"
    rule_file.write_text(_RULE_JSON, encoding="utf-8")

    add = r.invoke(cli_main.cli, ["moderate-rules", "add", str(rule_file)])
    assert add.exit_code == 0, add.output
    assert "no-spam" in add.output

    lst = r.invoke(cli_main.cli, ["moderate-rules", "list"])
    assert lst.exit_code == 0
    assert "no-spam" in lst.output and "-100200" in lst.output

    rm = r.invoke(cli_main.cli, ["moderate-rules", "remove", "--", "-100200", "no-spam"])
    assert rm.exit_code == 0

    lst2 = r.invoke(cli_main.cli, ["moderate-rules", "list"])
    assert "No rules." in lst2.output


def test_moderate_rules_remove_missing_errors(mod_runner):
    r, _stub, _tp, _, _seen = mod_runner
    result = r.invoke(cli_main.cli, ["moderate-rules", "remove", "--", "-100200", "missing"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_moderate_rules_add_rejects_bad_json(mod_runner, tmp_path):
    r, _stub, _tp, _, _seen = mod_runner
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = r.invoke(cli_main.cli, ["moderate-rules", "add", str(bad)])
    assert result.exit_code != 0
    assert "invalid rule JSON" in result.output


def test_moderate_runs_and_stops_on_ctrl_c(mod_runner):
    r, stub, _tp, _, _seen = mod_runner
    result = r.invoke(cli_main.cli, ["moderate"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert "stopped." in result.output
    assert stub.connected is False


def test_moderate_enforce_flag_shown(mod_runner):
    r, _stub, _tp, _, _seen = mod_runner
    result = r.invoke(cli_main.cli, ["moderate", "--enforce"])
    assert result.exit_code == 0
    assert "ENFORCING" in result.output


def test_moderate_uses_global_profile(mod_runner):
    r, _stub, _tp, _, seen = mod_runner
    result = r.invoke(cli_main.cli, ["--profile", "work", "moderate"])
    assert result.exit_code == 0
    assert seen["clients"][-1] == "work"
    assert seen["storages"][-1] == "work"


def test_moderate_without_admin_warns(mod_runner, tmp_path):
    r, stub, _tp, _, _seen = mod_runner
    stub.admin = False  # no rights anywhere
    rule_file = tmp_path / "rule.json"
    rule_file.write_text(_RULE_JSON, encoding="utf-8")
    r.invoke(cli_main.cli, ["moderate-rules", "add", str(rule_file)])
    result = r.invoke(cli_main.cli, ["moderate"])
    assert result.exit_code == 0
    assert "no admin rights" in result.output


def test_moderate_without_login_gives_hint(mod_runner):
    r, stub, _tp, _, _seen = mod_runner
    stub.authorized = False
    result = r.invoke(cli_main.cli, ["moderate"])
    assert result.exit_code != 0
    assert "tg-messenger login" in result.output


def test_moderate_rules_use_global_profile(mod_runner, tmp_path):
    r, _stub, _tp, _, seen = mod_runner
    rule_file = tmp_path / "rule.json"
    rule_file.write_text(_RULE_JSON, encoding="utf-8")

    add = r.invoke(cli_main.cli, ["--profile", "work", "moderate-rules", "add", str(rule_file)])
    assert add.exit_code == 0, add.output
    assert seen["storages"][-1] == "work"

    default_list = r.invoke(cli_main.cli, ["moderate-rules", "list"])
    assert default_list.exit_code == 0
    assert "No rules." in default_list.output

    work_list = r.invoke(cli_main.cli, ["--profile", "work", "moderate-rules", "list"])
    assert work_list.exit_code == 0
    assert "no-spam" in work_list.output


def test_revoked_session_mid_command_gives_hint(runner, monkeypatch):
    from telethon.errors.rpcerrorlist import AuthKeyUnregisteredError

    r, stub = runner

    async def boom(dm_only=True):
        raise AuthKeyUnregisteredError(None)

    monkeypatch.setattr(stub, "dialogs", boom)
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code != 0
    assert "tg-messenger login" in result.output
    assert "Traceback" not in result.output


# --- Цикл F (#18): команды ghostwrite + ghostwrite-dialogs ---


class FakeSuggesterCli:
    """Stub Suggester for the ghostwrite CLI: never touches an LLM."""

    async def suggest(self, dialog_id):
        return "auto reply"


@pytest.fixture
def gw_runner(monkeypatch, tmp_path):
    """CLI runner whose ghostwrite storage is a fresh tmp SQLite db; no LLM."""
    from tg_messenger.core.storage import Storage

    stub = StubClient()
    seen = {"clients": [], "storages": []}

    def fake_make_client(**kw):
        seen["clients"].append(kw.get("session_name"))
        return stub

    def fake_make_storage(profile="default"):
        seen["storages"].append(profile)
        return Storage(tmp_path / f"{profile}.db")

    monkeypatch.setattr(cli_main, "make_client", fake_make_client)
    monkeypatch.setattr(cli_main, "make_storage", fake_make_storage)
    monkeypatch.setattr(cli_main, "make_suggester", lambda client, storage=None: FakeSuggesterCli())
    return CliRunner(), stub, tmp_path, seen


def test_ghostwrite_dialogs_enable_list_disable(gw_runner):
    r, _stub, _tp, _seen = gw_runner
    en = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "enable", "7"])
    assert en.exit_code == 0, en.output

    lst = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "list"])
    assert lst.exit_code == 0
    assert "7" in lst.output

    dis = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "disable", "7"])
    assert dis.exit_code == 0

    lst2 = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "list"])
    assert "No dialogs" in lst2.output or "7" not in lst2.output


def test_ghostwrite_enable_star_is_rejected(gw_runner):
    r, _stub, _tp, _seen = gw_runner
    result = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "enable", "*"])
    assert result.exit_code != 0
    assert "*" in result.output


def test_ghostwrite_pause_all_and_resume(gw_runner):
    r, _stub, _tp, _seen = gw_runner
    r.invoke(cli_main.cli, ["ghostwrite-dialogs", "enable", "7"])
    pa = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "pause-all"])
    assert pa.exit_code == 0, pa.output
    res = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "resume", "7"])
    assert res.exit_code == 0, res.output


def test_ghostwrite_runs_and_stops_on_ctrl_c(gw_runner):
    r, stub, _tp, _seen = gw_runner
    r.invoke(cli_main.cli, ["ghostwrite-dialogs", "enable", "7"])
    result = r.invoke(cli_main.cli, ["ghostwrite"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "stopped." in result.output
    assert stub.connected is False
    # dry-run: nothing was actually sent
    assert stub.sent == []


def test_ghostwrite_watches_read_receipts(gw_runner, monkeypatch):
    # цикл 98 (#17): фиксация last_read из listen_reads живёт в долгоживущем
    # ghostwrite-цикле — иначе сигнал никогда не пишется
    r, stub, _tp, _seen = gw_runner
    calls = []

    async def spy(client, storage):
        calls.append(client)

    monkeypatch.setattr("tg_messenger.agent.suggest.watch_read_receipts", spy)
    r.invoke(cli_main.cli, ["ghostwrite-dialogs", "enable", "7"])
    result = r.invoke(cli_main.cli, ["ghostwrite"])
    assert result.exit_code == 0, result.output
    assert calls and calls[0] is stub


def test_ghostwrite_enforce_flag_shown(gw_runner):
    r, stub, _tp, _seen = gw_runner
    r.invoke(cli_main.cli, ["ghostwrite-dialogs", "enable", "7"])
    result = r.invoke(cli_main.cli, ["ghostwrite", "--enforce"])
    assert result.exit_code == 0, result.output
    assert "ENFORCING" in result.output


def test_ghostwrite_without_login_gives_hint(gw_runner):
    r, stub, _tp, _seen = gw_runner
    stub.authorized = False
    result = r.invoke(cli_main.cli, ["ghostwrite"])
    assert result.exit_code != 0
    assert "tg-messenger login" in result.output


def test_ghostwrite_uses_global_profile(gw_runner):
    r, _stub, _tp, seen = gw_runner
    enabled = r.invoke(cli_main.cli, ["--profile", "work", "ghostwrite-dialogs", "enable", "7"])
    assert enabled.exit_code == 0, enabled.output

    result = r.invoke(cli_main.cli, ["--profile", "work", "ghostwrite"])

    assert result.exit_code == 0, result.output
    assert seen["clients"][-1] == "work"
    assert seen["storages"][-1] == "work"


def test_ghostwrite_dialogs_use_global_profile(gw_runner):
    r, _stub, _tp, seen = gw_runner
    enabled = r.invoke(cli_main.cli, ["--profile", "work", "ghostwrite-dialogs", "enable", "7"])
    assert enabled.exit_code == 0, enabled.output
    assert seen["storages"][-1] == "work"

    default_list = r.invoke(cli_main.cli, ["ghostwrite-dialogs", "list"])
    assert default_list.exit_code == 0, default_list.output
    assert "No dialogs" in default_list.output

    work_list = r.invoke(cli_main.cli, ["--profile", "work", "ghostwrite-dialogs", "list"])
    assert work_list.exit_code == 0, work_list.output
    assert "7" in work_list.output
    assert seen["storages"][-1] == "work"


# --- Цикл 104 (#19): команды heartbeat + heartbeat plan/list/remove ---


@pytest.fixture
def hb_runner(monkeypatch, tmp_path):
    """CLI runner whose heartbeat storage is a fresh tmp SQLite db; no LLM."""
    from tg_messenger.core.storage import Storage

    stub = StubClient()
    seen = {"clients": [], "storages": []}

    def fake_make_client(**kw):
        seen["clients"].append(kw.get("session_name"))
        return stub

    def fake_make_storage(profile="default"):
        seen["storages"].append(profile)
        return Storage(tmp_path / f"{profile}.db")

    monkeypatch.setattr(cli_main, "make_client", fake_make_client)
    monkeypatch.setattr(cli_main, "make_storage", fake_make_storage)
    return CliRunner(), stub, tmp_path, seen


def test_heartbeat_plan_add_list_remove(hb_runner):
    r, _stub, _tp, _seen = hb_runner
    add = r.invoke(cli_main.cli, ["heartbeat", "plan", "7",
                                  "--interval", "24", "--template", "ping",
                                  "--template", "yo"])
    assert add.exit_code == 0, add.output

    lst = r.invoke(cli_main.cli, ["heartbeat", "list"])
    assert lst.exit_code == 0
    assert "7" in lst.output

    rm = r.invoke(cli_main.cli, ["heartbeat", "remove", "7"])
    assert rm.exit_code == 0

    lst2 = r.invoke(cli_main.cli, ["heartbeat", "list"])
    assert "No plans" in lst2.output


def test_heartbeat_plan_at_sends_scheduled_one_shot(hb_runner):
    r, stub, _tp, _seen = hb_runner
    result = r.invoke(cli_main.cli, ["heartbeat", "plan", "7",
                                     "--at", "18:00", "--template", "evening ping"])
    assert result.exit_code == 0, result.output
    # one-shot native schedule: a single send_text with a non-None schedule
    assert len(stub.sent) == 1
    peer, text, _reply, schedule = stub.sent[0]
    assert peer == 7
    assert text == "evening ping"
    assert schedule is not None
    # and it did NOT create a recurring stored plan
    lst = r.invoke(cli_main.cli, ["heartbeat", "list"])
    assert "No plans" in lst.output


def test_heartbeat_plan_requires_at_or_interval(hb_runner):
    r, _stub, _tp, _seen = hb_runner
    result = r.invoke(cli_main.cli, ["heartbeat", "plan", "7", "--template", "x"])
    assert result.exit_code != 0
    assert "--at" in result.output or "--interval" in result.output


def test_heartbeat_run_stops_on_ctrl_c(hb_runner, monkeypatch):
    r, stub, _tp, _seen = hb_runner
    # enable a plan so the tick has work; history raises Ctrl+C to break the loop
    r.invoke(cli_main.cli, ["heartbeat", "plan", "7", "--interval", "24", "--template", "ping"])

    async def boom(peer, limit=1):
        raise KeyboardInterrupt

    monkeypatch.setattr(stub, "history", boom)
    result = r.invoke(cli_main.cli, ["heartbeat", "run"])
    assert result.exit_code == 0, result.output
    assert "stopped." in result.output
    assert stub.connected is False


def test_heartbeat_run_watches_read_receipts(hb_runner, monkeypatch):
    # сигнал «прочитал и молчит» (#17/#19) пишется и из heartbeat run
    r, stub, _tp, _seen = hb_runner
    calls = []

    async def spy(client, storage):
        calls.append(client)

    monkeypatch.setattr("tg_messenger.agent.suggest.watch_read_receipts", spy)
    r.invoke(cli_main.cli, ["heartbeat", "plan", "7", "--interval", "24", "--template", "ping"])

    async def boom(peer, limit=1):
        raise KeyboardInterrupt

    monkeypatch.setattr(stub, "history", boom)
    result = r.invoke(cli_main.cli, ["heartbeat", "run"])
    assert result.exit_code == 0, result.output
    assert calls and calls[0] is stub


def test_heartbeat_run_without_login_gives_hint(hb_runner):
    r, stub, _tp, _seen = hb_runner
    stub.authorized = False
    result = r.invoke(cli_main.cli, ["heartbeat", "run"])
    assert result.exit_code != 0
    assert "tg-messenger login" in result.output


def test_heartbeat_plan_list_remove_use_global_profile(hb_runner):
    r, _stub, _tp, seen = hb_runner
    add = r.invoke(
        cli_main.cli,
        ["--profile", "work", "heartbeat", "plan", "7", "--interval", "24", "--template", "ping"],
    )
    assert add.exit_code == 0, add.output
    assert seen["storages"][-1] == "work"

    default_list = r.invoke(cli_main.cli, ["heartbeat", "list"])
    assert default_list.exit_code == 0, default_list.output
    assert "No plans" in default_list.output

    work_list = r.invoke(cli_main.cli, ["--profile", "work", "heartbeat", "list"])
    assert work_list.exit_code == 0, work_list.output
    assert "7" in work_list.output
    assert seen["storages"][-1] == "work"

    removed = r.invoke(cli_main.cli, ["--profile", "work", "heartbeat", "remove", "7"])
    assert removed.exit_code == 0, removed.output
    assert seen["storages"][-1] == "work"


def test_heartbeat_run_uses_global_profile(hb_runner, monkeypatch):
    r, stub, _tp, seen = hb_runner
    r.invoke(cli_main.cli, ["--profile", "work", "heartbeat", "plan", "7", "--interval", "24", "--template", "ping"])

    async def boom(peer, limit=1):
        raise KeyboardInterrupt

    monkeypatch.setattr(stub, "history", boom)
    result = r.invoke(cli_main.cli, ["--profile", "work", "heartbeat", "run"])
    assert result.exit_code == 0, result.output
    assert seen["clients"][-1] == "work"
    assert seen["storages"][-1] == "work"


def test_dotenv_autoloaded_for_commands(runner, tmp_path, monkeypatch):
    # isolate os.environ so the test can't leak TG_API_ID into the session
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()
                                        if k not in ("TG_API_ID", "TG_API_HASH")})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text('TG_API_ID=42\nTG_API_HASH="abc"\n# comment\n', encoding="utf-8")
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code == 0
    assert os.environ["TG_API_ID"] == "42"
    assert os.environ["TG_API_HASH"] == "abc"  # quotes stripped


def test_dotenv_does_not_override_real_env(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.chdir(tmp_path)
    os.environ["TG_API_ID"] = "111"
    (tmp_path / ".env").write_text("TG_API_ID=42\n", encoding="utf-8")
    r, _ = runner
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code == 0
    assert os.environ["TG_API_ID"] == "111"


def test_unexpected_error_hint_instead_of_traceback(runner, monkeypatch):
    r, stub = runner

    async def boom(dm_only=True):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(stub, "dialogs", boom)
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code != 0
    assert "Unexpected error" in result.output
    assert "kaboom" in result.output
    assert "Traceback" not in result.output


def test_unexpected_error_traceback_lands_in_log_file(runner, monkeypatch):
    from pathlib import Path

    r, stub = runner

    async def boom(dm_only=True):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(stub, "dialogs", boom)
    result = r.invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code != 0
    log_file = Path(os.environ["TG_LOG_DIR"]) / "tg_messenger.log"
    content = log_file.read_text(encoding="utf-8")
    assert "kaboom" in content
    assert "Traceback" in content


def test_flood_wait_is_logged_to_file(runner, monkeypatch):
    from pathlib import Path

    r, stub = runner

    async def boom(peer, text, reply_to=None):
        raise HandledFloodWaitError("send_text", 9999)

    monkeypatch.setattr(stub, "send_text", boom)
    result = r.invoke(cli_main.cli, ["send", "7", "hi"])
    assert result.exit_code != 0
    log_file = Path(os.environ["TG_LOG_DIR"]) / "tg_messenger.log"
    assert "flood wait" in log_file.read_text(encoding="utf-8")


def test_chat_listener_failure_is_reported(runner, monkeypatch, caplog):
    r, stub = runner

    async def broken_listen():
        raise RuntimeError("listener blew up")
        yield  # pragma: no cover

    monkeypatch.setattr(stub, "listen", broken_listen)
    with caplog.at_level("ERROR", logger="tg_messenger.cli.main"):
        result = r.invoke(cli_main.cli, ["chat", "7"], input="hello\n")
    assert result.exit_code == 0  # the REPL itself still worked
    assert (7, "hello", None, None) in stub.sent
    assert "listener failed" in result.output
    errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


def test_serve_unifies_uvicorn_logging(serve_spy):
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0
    assert serve_spy["uvicorn"][0]["log_config"] is None


def test_serve_announces_url(serve_spy, monkeypatch):
    # uvicorn's own startup banner goes to the file now — the CLI must say the URL
    monkeypatch.delenv("TG_WEB_PORT", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0
    assert "http://127.0.0.1:8090" in result.output


def test_verbose_flag_sets_debug_level(runner):
    import logging

    r, _ = runner
    result = r.invoke(cli_main.cli, ["-v", "dialogs"])
    assert result.exit_code == 0
    assert logging.getLogger().level == logging.DEBUG


def test_help_lists_commands():
    result = CliRunner().invoke(cli_main.cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("login", "dialogs", "read", "send", "listen", "watch", "serve", "tui"):
        assert cmd in result.output


# --- цикл 48: понятные ошибки без установленного extra ---

def _block_imports(monkeypatch, *blocked: str):
    """Make `import <name>` raise ImportError for the given packages.

    Also evicts the already-imported interface modules from ``sys.modules`` so the
    command's lazy import actually re-executes and hits the block — other tests may
    have cached ``tg_messenger.tui``/``textual`` etc. already.
    """
    import builtins

    real_import = builtins.__import__

    def matches(name: str) -> bool:
        return name in blocked or any(name.startswith(b + ".") for b in blocked)

    for mod in [m for m in sys.modules if matches(m)]:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    def fake_import(name, *args, **kwargs):
        if matches(name):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_serve_without_web_extra_hints_install(monkeypatch):
    # simulate `pip install tg-messenger` without the [web] extra
    _block_imports(monkeypatch, "uvicorn", "fastapi", "tg_messenger.web")
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code != 0
    assert "tg-messenger[web]" in result.output


def test_tui_without_tui_extra_hints_install(monkeypatch):
    _block_imports(monkeypatch, "textual", "tg_messenger.tui")
    result = CliRunner().invoke(cli_main.cli, ["tui"])
    assert result.exit_code != 0
    assert "tg-messenger[tui]" in result.output


# --- цикл 55: export/import session string (SSO) ---

class ExportStubClient:
    def __init__(self, session_string="EXPORTED-SESSION", authorized=True):
        self._session_string = session_string
        self._authorized = authorized
        self.connected = False
        self.imported = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_authorized(self):
        return self._authorized

    def export_session_string(self):
        return self._session_string

    def import_session_string(self, s):
        self.imported.append(s)


def test_login_export_session_prints_string_and_warning(monkeypatch):
    stub = ExportStubClient(session_string="MY-SECRET-SESSION")
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["login", "--export-session"])
    assert result.exit_code == 0
    assert "MY-SECRET-SESSION" in result.output
    assert "full access" in result.output.lower() or "полный доступ" in result.output.lower()


def test_login_import_session_saves_valid_string(monkeypatch):
    saved = []

    class Store:
        def save(self, session, raw):
            saved.append((session, raw))

    monkeypatch.setattr(cli_main, "_session_store", lambda: Store())
    valid = _valid_session_for_import()
    result = CliRunner().invoke(cli_main.cli, ["login", "--import-session"], input=valid + "\n")
    assert result.exit_code == 0, result.output
    assert saved == [("default", valid)]


def test_login_import_session_reads_piped_stdin_without_prompt(monkeypatch):
    saved = []

    class Store:
        def save(self, session, raw):
            saved.append((session, raw))

    def prompt_must_not_run(*args, **kwargs):
        raise AssertionError("piped import must read stdin directly")

    monkeypatch.setattr(cli_main, "_session_store", lambda: Store())
    monkeypatch.setattr(cli_main.click, "prompt", prompt_must_not_run)
    valid = _valid_session_for_import()
    result = CliRunner().invoke(cli_main.cli, ["login", "--import-session"], input=valid + "\n")
    assert result.exit_code == 0, result.output
    assert saved == [("default", valid)]


def test_login_import_session_rejects_garbage(monkeypatch):
    saved = []

    class Store:
        def save(self, session, raw):
            saved.append((session, raw))

    # garbage must be rejected before it ever reaches the store
    monkeypatch.setattr(cli_main, "_session_store", lambda: Store())
    result = CliRunner().invoke(cli_main.cli, ["login", "--import-session"], input="not-a-session\n")
    assert result.exit_code != 0
    assert "invalid StringSession" in result.output
    assert saved == []


def test_login_import_session_rejects_empty_input(monkeypatch):
    saved = []

    class Store:
        def save(self, session, raw):
            saved.append((session, raw))

    monkeypatch.setattr(cli_main, "_session_store", lambda: Store())
    result = CliRunner().invoke(cli_main.cli, ["login", "--import-session"], input=" \n")
    assert result.exit_code != 0
    assert "invalid StringSession" in result.output
    assert saved == []


def test_login_import_session_replaces_unreadable_existing_file(monkeypatch, session_dir):
    from tg_messenger.core.auth import SessionStore

    store = SessionStore(session_dir)
    store.session_dir.mkdir(parents=True, exist_ok=True)
    store.path_for("default").write_text("not-a-valid-session", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_session_store", lambda: store)

    valid = _valid_session_for_import()
    result = CliRunner().invoke(cli_main.cli, ["login", "--import-session"], input=valid + "\n")

    assert result.exit_code == 0, result.output
    assert store.load("default") == valid


def test_export_session_not_in_log(monkeypatch, tmp_path):
    import logging

    stub = ExportStubClient(session_string="SHOULD-NOT-BE-LOGGED")
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    monkeypatch.setenv("TG_LOG_DIR", str(tmp_path))
    CliRunner().invoke(cli_main.cli, ["-v", "login", "--export-session"])
    logging.shutdown()
    logs = "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "SHOULD-NOT-BE-LOGGED" not in logs


def _valid_session_for_import():
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    s = StringSession()
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(b"\x00" * 256)
    return s.save()


# --- циклы 58–59: мультилогин --profile + меню ---

class ProfileSpyClient(StubClient):
    """StubClient that records the session_name it was built with."""

    last_kwargs = {}


@pytest.fixture
def profile_spy(monkeypatch):
    captured = {}

    def fake_make_client(**kw):
        captured.update(kw)
        return StubClient()

    monkeypatch.setattr(cli_main, "make_client", fake_make_client)
    return captured


def test_global_profile_sets_session_name(profile_spy):
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "dialogs"])
    assert result.exit_code == 0, result.output
    assert profile_spy.get("session_name") == "work"


def test_make_client_uses_tg_session_dir(monkeypatch, tmp_path):
    captured = {}

    class FakeStandaloneTelegramClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    # make_client делегирует в core.client_from_env — сема подменяется там же,
    # как в зеркальных тестах web/tui (_make_real_client)
    monkeypatch.setattr(
        "tg_messenger.core.client.StandaloneTelegramClient", FakeStandaloneTelegramClient
    )

    cli_main.make_client(session_name="work")

    assert captured["session_name"] == "work"
    assert captured["session_dir"] == str(tmp_path)


@pytest.mark.parametrize(
    ("args", "input_text"),
    [
        (["listen"], None),
        (["watch"], None),
        (["chat", "7"], ""),
        (["agent"], None),
    ],
)
def test_global_profile_reaches_direct_client_commands(
    profile_spy, monkeypatch, args, input_text
):
    class FakeAgentRunner:
        async def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        cli_main,
        "make_agent_runner",
        lambda client, *, notify_errors=False: FakeAgentRunner(),
    )

    result = CliRunner().invoke(
        cli_main.cli,
        ["--profile", "work", *args],
        input=input_text,
    )

    assert result.exit_code == 0, result.output
    assert profile_spy.get("session_name") == "work"


# --- #50: safe default send rate + loud WARNING only when explicitly off ---


def _run_agent_with_interrupt(monkeypatch):
    """Invoke the `agent` command with a stub client/runner that exits via Ctrl+C."""
    class FakeAgentRunner:
        async def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    monkeypatch.setattr(
        cli_main, "make_agent_runner",
        lambda client, *, notify_errors=False: FakeAgentRunner(),
    )
    return CliRunner().invoke(cli_main.cli, ["agent"])


def test_sender_command_no_warning_when_send_rate_unset(monkeypatch, caplog):
    monkeypatch.delenv("TG_SEND_RATE", raising=False)
    with caplog.at_level("WARNING", logger="tg_messenger.cli.main"):
        result = _run_agent_with_interrupt(monkeypatch)
    assert result.exit_code == 0, result.output
    assert not any("TG_SEND_RATE" in r.message for r in caplog.records)


def test_sender_command_warns_when_send_rate_explicitly_off(monkeypatch, caplog):
    monkeypatch.setenv("TG_SEND_RATE", "0")
    with caplog.at_level("WARNING", logger="tg_messenger.cli.main"):
        result = _run_agent_with_interrupt(monkeypatch)
    assert result.exit_code == 0, result.output
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("TG_SEND_RATE=0" in r.message for r in warnings), [r.message for r in warnings]


def test_sender_command_no_warning_when_send_rate_set(monkeypatch, caplog):
    monkeypatch.setenv("TG_SEND_RATE", "20")
    with caplog.at_level("WARNING", logger="tg_messenger.cli.main"):
        result = _run_agent_with_interrupt(monkeypatch)
    assert result.exit_code == 0, result.output
    assert not any("TG_SEND_RATE" in r.message for r in caplog.records)


def test_sender_command_warns_on_invalid_send_rate(monkeypatch, caplog):
    # A non-numeric value is surfaced as a distinct "cannot be parsed" warning, not folded
    # into "off" — client_from_env would raise on it, so "limit is off" would mislead.
    monkeypatch.setenv("TG_SEND_RATE", "abc")
    with caplog.at_level("WARNING", logger="tg_messenger.cli.main"):
        result = _run_agent_with_interrupt(monkeypatch)
    assert result.exit_code == 0, result.output
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("not a number" in m for m in warnings), warnings


def test_ghostwrite_command_warns_when_send_rate_explicitly_off(monkeypatch, caplog):
    # Guards a SECOND call site (besides `agent`): if _warn_if_send_rate_off() were dropped
    # from ghostwrite, this catches it. The helper runs before _do(), so making make_client
    # raise KeyboardInterrupt ends the command cleanly without the engine/LLM/network stack.
    monkeypatch.setenv("TG_SEND_RATE", "0")

    def _interrupt(**kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "make_client", _interrupt)
    with caplog.at_level("WARNING", logger="tg_messenger.cli.main"):
        result = CliRunner().invoke(cli_main.cli, ["ghostwrite"])
    assert result.exit_code == 0, result.output
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("TG_SEND_RATE=0" in m for m in warnings), warnings


def test_profiles_command_lists_saved(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    store = SessionStore(tmp_path)
    store.save("alice", _valid_session_for_import())
    store.save("bob", _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    result = CliRunner().invoke(cli_main.cli, ["profiles"])
    assert result.exit_code == 0, result.output
    # #52: valid profiles carry the ✓ marker
    assert "alice ✓" in result.output
    assert "bob ✓" in result.output


def test_profiles_command_marks_corrupt_profile(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    store = SessionStore(tmp_path)
    store.save("good", _valid_session_for_import())
    store.path_for("broken").write_text("garbage", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    result = CliRunner().invoke(cli_main.cli, ["profiles"])
    assert result.exit_code == 0, result.output
    assert "good ✓" in result.output
    assert "broken ✗" in result.output


def test_profiles_command_empty_hint_uses_global_profile_position(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))

    result = CliRunner().invoke(cli_main.cli, ["profiles"])

    assert result.exit_code == 0, result.output
    assert "tg-messenger --profile NAME login" in result.output


def test_multiple_profiles_non_interactive_errors(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    store = SessionStore(tmp_path)
    store.save("alice", _valid_session_for_import())
    store.save("bob", _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    # CliRunner is non-interactive (stdin not a tty) → ambiguous profile must error
    result = CliRunner().invoke(cli_main.cli, ["dialogs"])
    assert result.exit_code != 0
    assert "--profile" in result.output


# --- #11 (комментарий): logout + profiles remove ---


def _profile_store(monkeypatch, tmp_path, *names):
    from tg_messenger.core.auth import SessionStore

    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    store = SessionStore(tmp_path)
    for n in names:
        store.save(n, _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    return store


def test_logout_logs_out_and_removes_session(monkeypatch, tmp_path):
    store = _profile_store(monkeypatch, tmp_path, "work")
    stub = StubClient()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "logout", "--yes"])
    assert result.exit_code == 0, result.output
    assert stub.logged_out is True
    assert store.list_profiles() == []


def test_logout_deletes_file_even_if_telegram_fails(monkeypatch, tmp_path):
    # best-effort: мёртвая/отозванная сессия не должна мешать удалению файла
    store = _profile_store(monkeypatch, tmp_path, "work")
    stub = StubClient()

    async def boom():
        raise RuntimeError("AUTH_KEY_UNREGISTERED")

    stub.log_out = boom
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "logout", "--yes"])
    assert result.exit_code == 0, result.output
    assert store.list_profiles() == []


def test_logout_asks_confirmation(monkeypatch, tmp_path):
    store = _profile_store(monkeypatch, tmp_path, "work")
    stub = StubClient()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "logout"], input="n\n")
    assert result.exit_code != 0
    assert store.list_profiles() == ["work"]
    assert stub.logged_out is False


def test_logout_missing_profile_errors(monkeypatch, tmp_path):
    _profile_store(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main.cli, ["--profile", "ghost", "logout", "--yes"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_profiles_remove_deletes_file_without_network(monkeypatch, tmp_path):
    # remove — для мёртвых сессий: только файл, клиент не строится вовсе
    store = _profile_store(monkeypatch, tmp_path, "dead", "live")
    network = []
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: network.append(kw))
    result = CliRunner().invoke(cli_main.cli, ["profiles", "remove", "dead", "--yes"])
    assert result.exit_code == 0, result.output
    assert store.list_profiles() == ["live"]
    assert network == []


def test_profiles_remove_missing_errors(monkeypatch, tmp_path):
    _profile_store(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main.cli, ["profiles", "remove", "ghost", "--yes"])
    assert result.exit_code != 0


def test_explicit_default_session_skips_profile_picker(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    captured = {}

    def fake_make_client(**kw):
        captured.update(kw)
        return StubClient()

    store = SessionStore(tmp_path)
    store.save("alice", _valid_session_for_import())
    store.save("bob", _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    monkeypatch.setattr(cli_main, "make_client", fake_make_client)

    result = CliRunner().invoke(cli_main.cli, ["dialogs", "--session", "default"])

    assert result.exit_code == 0, result.output
    assert captured.get("session_name") == "default"


def test_profile_menu_picks_second(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    captured = {}

    def fake_make_client(**kw):
        captured.update(kw)
        return StubClient()

    store = SessionStore(tmp_path)
    for name in ("alice", "bob", "carol"):
        store.save(name, _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    monkeypatch.setattr(cli_main, "make_client", fake_make_client)
    # force interactive so the menu shows; feed "2" to pick the second profile
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    result = CliRunner().invoke(cli_main.cli, ["dialogs"], input="2\n")
    assert result.exit_code == 0, result.output
    assert captured.get("session_name") == "bob"  # sorted: alice, bob, carol → #2


def test_profile_menu_reprompts_on_out_of_range(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    captured = {}

    def fake_make_client(**kw):
        captured.update(kw)
        return StubClient()

    store = SessionStore(tmp_path)
    for name in ("alice", "bob", "carol"):
        store.save(name, _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    monkeypatch.setattr(cli_main, "make_client", fake_make_client)
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    # 9 is out of range → re-prompt; then 2 picks bob
    result = CliRunner().invoke(cli_main.cli, ["dialogs"], input="9\n2\n")
    assert result.exit_code == 0, result.output
    assert "out of range" in result.output


def test_interactive_menu_reinits_log_for_chosen_profile(monkeypatch, tmp_path):
    # #52 point 3: a menu-chosen profile re-runs setup_logging so the log file is
    # isolated (tg_messenger_<profile>.log), not just for an explicit --profile.
    from tg_messenger.core.auth import SessionStore

    store = SessionStore(tmp_path)
    for name in ("alice", "bob", "carol"):
        store.save(name, _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)

    profiles_logged = []
    real_setup = cli_main.setup_logging

    def spy_setup_logging(*args, **kw):
        profiles_logged.append(kw.get("profile"))
        return real_setup(*args, **kw)

    monkeypatch.setattr(cli_main, "setup_logging", spy_setup_logging)
    result = CliRunner().invoke(cli_main.cli, ["dialogs"], input="2\n")
    assert result.exit_code == 0, result.output
    # the LAST setup_logging call must target the menu-chosen profile (sorted #2 = bob)
    assert profiles_logged[-1] == "bob", profiles_logged


# --- цикл 61: serve/tui учитывают глобальный --profile ---

def test_serve_uses_global_profile_as_session(monkeypatch):
    captured = {}
    client = object()
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    monkeypatch.setattr(cli_main, "make_optional_suggester", lambda c, **kw: object())
    _patch_message_store(monkeypatch)
    monkeypatch.setattr(
        "tg_messenger.web.app.build_app",
        lambda **kw: captured.update(kw) or object(),
    )
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "serve"])
    assert result.exit_code == 0, result.output
    assert captured.get("session_name") == "work"


def test_serve_wires_suggester(monkeypatch):
    captured = {}
    client = object()
    suggester = object()
    optional_kwargs = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    _patch_message_store(monkeypatch)

    def fake_make_optional_suggester(c, **kw):
        optional_kwargs.update(kw)
        return suggester

    monkeypatch.setattr(cli_main, "make_optional_suggester", fake_make_optional_suggester)
    monkeypatch.setattr(
        "tg_messenger.web.app.build_app",
        lambda **kw: captured.update(kw) or object(),
    )

    result = CliRunner().invoke(cli_main.cli, ["serve"])

    assert result.exit_code == 0, result.output
    assert captured["client"] is client
    assert captured["suggester"] is suggester
    assert optional_kwargs == {"session": "default"}


def test_tui_uses_global_profile_as_session(monkeypatch):
    captured = {}
    client = object()
    suggester = object()
    optional_kwargs = {}
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    _patch_message_store(monkeypatch)

    def fake_make_optional_suggester(c, **kw):
        optional_kwargs.update(kw)
        return suggester

    monkeypatch.setattr(cli_main, "make_optional_suggester", fake_make_optional_suggester)

    class FakeTUI:
        def __init__(
            self, *, client=None, session_name="default", suggester=None,
            store=None, translator=None, outbound=None,
        ):
            captured["client"] = client
            captured["session_name"] = session_name
            captured["suggester"] = suggester
            captured["store"] = store
            captured["translator"] = translator
            captured["outbound"] = outbound

        def run(self):
            pass

    monkeypatch.setattr("tg_messenger.tui.app.MessengerTUI", FakeTUI)
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "tui"])
    assert result.exit_code == 0, result.output
    assert captured.get("session_name") == "work"
    assert captured["client"] is client
    assert captured["suggester"] is suggester
    assert optional_kwargs == {"session": "work"}


# --- #52 point 2: ProfileScreen reachable from the `tui` entrypoint ---


class _FakeTUIAllKwargs:
    """Records every constructor kwarg so a test can assert eager vs deferred wiring."""

    captured: dict = {}

    def __init__(self, **kw):
        type(self).captured = dict(kw)

    def run(self):
        pass


def _patch_tui(monkeypatch):
    monkeypatch.setattr("tg_messenger.tui.app.MessengerTUI", _FakeTUIAllKwargs)
    _FakeTUIAllKwargs.captured = {}


def test_tui_eager_when_single_profile(monkeypatch, tmp_path):
    # 0/1 profile: resolve silently, build deps eagerly, pass a ready client (no defer).
    from tg_messenger.core.auth import SessionStore

    store = SessionStore(tmp_path)
    store.save("solo", _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    deps_calls = []
    monkeypatch.setattr(
        cli_main, "make_tui_deps",
        lambda profile, **kw: (deps_calls.append(profile) or cli_main.TuiDeps(
            client=StubClient(), session_name=profile, suggester=None,
            store=None, translator=None, outbound=None,
        )),
    )
    _patch_tui(monkeypatch)
    result = CliRunner().invoke(cli_main.cli, ["tui"])
    assert result.exit_code == 0, result.output
    assert deps_calls == ["solo"]
    cap = _FakeTUIAllKwargs.captured
    assert cap.get("client") is not None  # ready client, eager
    assert cap.get("deps_factory") is None
    assert cap.get("session_name") == "solo"


def test_tui_eager_when_explicit_profile(monkeypatch, tmp_path):
    # --profile wins: no menu, no defer, deps built for the named profile even with >1 saved.
    from tg_messenger.core.auth import SessionStore

    store = SessionStore(tmp_path)
    for name in ("alice", "bob"):
        store.save(name, _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    deps_calls = []
    monkeypatch.setattr(
        cli_main, "make_tui_deps",
        lambda profile, **kw: (deps_calls.append(profile) or cli_main.TuiDeps(
            client=StubClient(), session_name=profile, suggester=None,
            store=None, translator=None, outbound=None,
        )),
    )
    _patch_tui(monkeypatch)
    result = CliRunner().invoke(cli_main.cli, ["--profile", "alice", "tui"])
    assert result.exit_code == 0, result.output
    assert deps_calls == ["alice"]
    assert _FakeTUIAllKwargs.captured.get("deps_factory") is None


def test_tui_defers_to_screen_when_multi_profile_interactive(monkeypatch, tmp_path):
    # >1 profiles + interactive: do NOT resolve up front — pass profiles + a deps_factory
    # so the in-app ProfileScreen picks, then builds deps lazily.
    from tg_messenger.core.auth import SessionStore

    store = SessionStore(tmp_path)
    for name in ("alice", "bob"):
        store.save(name, _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    deps_calls = []
    monkeypatch.setattr(
        cli_main, "make_tui_deps", lambda profile, **kw: deps_calls.append(profile)
    )
    _patch_tui(monkeypatch)
    result = CliRunner().invoke(cli_main.cli, ["tui"])
    assert result.exit_code == 0, result.output
    assert deps_calls == []  # built lazily inside the TUI, not up front
    cap = _FakeTUIAllKwargs.captured
    assert cap.get("client") is None
    assert sorted(cap.get("profiles")) == ["alice", "bob"]
    assert cap.get("deps_factory") is not None
    # the factory routes to make_tui_deps for the chosen profile
    cap["deps_factory"]("bob")
    assert deps_calls == ["bob"]


def test_tui_errors_when_multi_profile_non_interactive(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    store = SessionStore(tmp_path)
    for name in ("alice", "bob"):
        store.save(name, _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    _patch_tui(monkeypatch)
    result = CliRunner().invoke(cli_main.cli, ["tui"])
    assert result.exit_code != 0
    assert "--profile" in result.output


def test_make_tui_deps_calls_setup_logging_and_threads_storage(monkeypatch):
    # make_tui_deps re-inits per-profile logging (point 3) and threads the SAME storage
    # object from make_message_store into both the translator and outbound builders.
    log_calls = []
    monkeypatch.setattr(
        cli_main, "setup_logging", lambda **kw: log_calls.append(kw)
    )
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    monkeypatch.setattr(cli_main, "make_optional_suggester", lambda c, **kw: "SUG")
    sentinel_storage = object()
    monkeypatch.setattr(
        cli_main, "make_message_store", lambda client, **kw: ("STORE", sentinel_storage)
    )
    translator_storage = {}
    outbound_args = {}

    def fake_translator(storage):
        translator_storage["storage"] = storage
        return "TR"

    def fake_outbound(store, storage):
        outbound_args.update(store=store, storage=storage)
        return "OUT"

    monkeypatch.setattr(cli_main, "make_optional_translator", fake_translator)
    monkeypatch.setattr(cli_main, "make_optional_outbound", fake_outbound)
    deps = cli_main.make_tui_deps("myprofile", log_kwargs={"verbose": False, "console": False})
    assert deps.session_name == "myprofile"
    assert deps.suggester == "SUG"
    assert deps.store == "STORE"
    assert deps.translator == "TR"
    assert deps.outbound == "OUT"
    # per-profile log re-init with console=False
    assert any(c.get("profile") == "myprofile" and c.get("console") is False for c in log_calls)
    # same storage threaded everywhere
    assert translator_storage["storage"] is sentinel_storage
    assert outbound_args["storage"] is sentinel_storage
    assert outbound_args["store"] == "STORE"


# --- Цикл 122: username suggest / set / clear ---


def test_username_suggest_prints_available(runner, monkeypatch):
    cli, stub = runner
    # generate a deterministic candidate list and mark some occupied
    import random

    from tg_messenger.core.usernames import generate_candidates

    cands = generate_candidates("Ann", count=20, rng=random.Random(0))
    stub.occupied = set(cands[:2])
    result = cli.invoke(cli_main.cli, ["username", "suggest", "Ann", "--limit", "5"])
    assert result.exit_code == 0, result.output
    lines = [ln.strip() for ln in result.output.splitlines() if ln.strip()]
    assert lines, "expected at least one suggested username"
    for ln in lines:
        # every line carries an availability marker: ✓ (verified free) or ? (unchecked)
        assert ln.endswith("✓") or ln.endswith("?"), ln
        name = ln[:-1].strip()
        if ln.endswith("✓"):
            # verified-free names are genuinely not occupied
            assert name not in stub.occupied


def test_username_suggest_marks_unchecked_with_question(runner, monkeypatch):
    cli, stub = runner
    # nothing occupied → the first `limit` candidates verify free (✓), the rest of the
    # generated pool is never checked and must be printed with the `?` marker (issue #53).
    # The CLI uses the global rng (no seed injected here), so we assert on the marker
    # structure, not on the concrete names.
    stub.occupied = set()
    result = cli.invoke(cli_main.cli, ["username", "suggest", "Ann", "--limit", "3"])
    assert result.exit_code == 0, result.output
    lines = [ln.strip() for ln in result.output.splitlines() if ln.strip()]
    checked = [ln[:-1].strip() for ln in lines if ln.endswith("✓")]
    unchecked = [ln[:-1].strip() for ln in lines if ln.endswith("?")]
    assert len(checked) == 3, lines  # stopped at the limit
    assert unchecked, "expected unchecked candidates past the limit to be marked ?"
    # the ✓ block comes entirely before the ? block
    assert lines[3].endswith("?"), lines
    assert all(lines[i].endswith("✓") for i in range(3)), lines
    # the two markers partition the printed names, no name appears in both
    assert set(checked).isdisjoint(unchecked)


def test_username_set_confirms(runner):
    cli, stub = runner
    result = cli.invoke(cli_main.cli, ["username", "set", "mynewhandle"])
    assert result.exit_code == 0, result.output
    assert stub.set_username_to == "mynewhandle"
    assert "mynewhandle" in result.output


def test_username_set_occupied_errors(runner):
    cli, stub = runner
    stub.occupied = {"takenname"}
    result = cli.invoke(cli_main.cli, ["username", "set", "takenname"])
    assert result.exit_code != 0
    assert stub.set_username_to is None


def test_username_clear_confirms(runner):
    cli, stub = runner
    result = cli.invoke(cli_main.cli, ["username", "clear"])
    assert result.exit_code == 0, result.output
    assert stub.cleared is True


# --- Цикл 125: режимы запуска serve (web-auth #24) ---


@pytest.fixture
def serve_capture(monkeypatch):
    """Like serve_spy but captures build_app kwargs too."""
    client = object()
    suggester = object()
    calls = {"uvicorn": [], "build_app": [], "optional_suggester": []}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls["uvicorn"].append(kw))
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    _patch_message_store(monkeypatch)
    monkeypatch.setattr(
        cli_main,
        "make_optional_suggester",
        lambda c, **kw: calls["optional_suggester"].append((c, kw)) or suggester,
    )
    monkeypatch.setattr(
        "tg_messenger.web.app.build_app",
        lambda **kw: calls["build_app"].append(kw) or object(),
    )
    return calls


def test_serve_public_host_without_pass_refuses(serve_capture, monkeypatch):
    monkeypatch.delenv("TG_WEB_PASS", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "TG_WEB_PASS" in result.output
    assert not serve_capture["uvicorn"]  # never started


def test_serve_public_host_insecure_starts_with_warning(serve_capture, monkeypatch, caplog):
    import logging

    monkeypatch.delenv("TG_WEB_PASS", raising=False)
    with caplog.at_level(logging.WARNING, logger="tg_messenger.cli.main"):
        result = CliRunner().invoke(cli_main.cli, ["serve", "--host", "0.0.0.0", "--insecure"])
    assert result.exit_code == 0, result.output
    assert serve_capture["uvicorn"]  # started
    assert any("insecure" in rec.message.lower() or "without" in rec.message.lower()
               for rec in caplog.records)


def test_serve_localhost_without_pass_starts(serve_capture, monkeypatch):
    monkeypatch.delenv("TG_WEB_PASS", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0, result.output
    assert serve_capture["uvicorn"]
    assert serve_capture["build_app"][0].get("web_pass") is None


def test_serve_passes_web_pass_from_env(serve_capture, monkeypatch):
    monkeypatch.setenv("TG_WEB_PASS", "hunter2")
    result = CliRunner().invoke(cli_main.cli, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert serve_capture["build_app"][0].get("web_pass") == "hunter2"
