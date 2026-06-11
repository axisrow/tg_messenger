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
    User,
)


class StubClient:
    def __init__(self, **kw):
        self.sent = []
        self.forwarded = []
        self.edited = []
        self.deleted_calls = []
        self.read_acks = []
        self.downloaded = []
        self.searched = []
        self.history_items = None
        self.connected = False
        self.authorized = True
        self.listen_interrupt = True  # emulate Ctrl+C after the first event

    async def is_authorized(self):
        return self.authorized

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

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append((peer, text, reply_to))
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

    async def mark_read(self, peer):
        self.read_acks.append(peer)

    async def send_media(self, peer, file_path, caption=None):
        self.sent.append((peer, "file", str(file_path), caption))
        return Message(id=3, dialog_id=peer, sender_id=1, out=True, text=caption,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def get_me(self):
        return User(id=1, first_name="Me")

    async def entity_title(self, peer):
        return "My Group"

    async def listen_outgoing(self):
        yield OutgoingEvent(
            dialog_id=-100123,
            message=Message(id=10, dialog_id=-100123, sender_id=1, out=True,
                            text="удалят меня", date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        )
        await asyncio.Event().wait()

    async def listen_deleted(self):
        for _ in range(10):
            await asyncio.sleep(0)  # дать outgoing-потоку закэшировать сообщение
        yield MessagesDeletedEvent(chat_id=-100123, message_ids=[10])
        for _ in range(10):
            await asyncio.sleep(0)  # дать watcher'у отправить уведомление
        raise KeyboardInterrupt  # эмуляция Ctrl+C (паттерн listen_interrupt)


@pytest.fixture
def runner(monkeypatch):
    stub = StubClient()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)
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
    assert stub.sent == [(7, "hello", None)]


def test_send_file_uses_send_media(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "caption", "--file", str(f)])
    assert result.exit_code == 0
    assert stub.sent[-1] == (7, "file", str(f), "caption")


# --- цикл 80: reply/forward/edit/delete/read команды ---


def test_send_reply_to_passed(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["send", "7", "re", "--reply-to", "42"])
    assert result.exit_code == 0, result.output
    assert stub.sent == [(7, "re", 42)]


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
    assert stub.read_acks == [7]


def test_flood_wait_friendly_message(runner, monkeypatch):
    r, stub = runner

    async def boom(peer, text, reply_to=None):
        raise HandledFloodWaitError("send_text", 9999)

    monkeypatch.setattr(stub, "send_text", boom)
    result = r.invoke(cli_main.cli, ["send", "7", "hi"])
    assert result.exit_code != 0 or "flood" in result.output.lower()


@pytest.fixture
def serve_spy(monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr("tg_messenger.web.app.build_app", lambda **kw: object())
    return calls


def test_serve_defaults_to_8090(serve_spy, monkeypatch):
    monkeypatch.delenv("TG_WEB_PORT", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0
    assert serve_spy[0]["port"] == 8090


def test_serve_reads_env_port(serve_spy, monkeypatch):
    monkeypatch.setenv("TG_WEB_PORT", "9099")
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0
    assert serve_spy[0]["port"] == 9099


def test_serve_flag_overrides_env(serve_spy, monkeypatch):
    monkeypatch.setenv("TG_WEB_PORT", "9099")
    result = CliRunner().invoke(cli_main.cli, ["serve", "--port", "1234"])
    assert result.exit_code == 0
    assert serve_spy[0]["port"] == 1234


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
    assert (7, "hello", None) in stub.sent
    assert stub.connected is False


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
    (peer, text, _reply), = stub.sent
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
    assert (7, "hello", None) in stub.sent
    assert "listener failed" in result.output
    errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
    assert errors and errors[0].exc_info is not None


def test_serve_unifies_uvicorn_logging(serve_spy):
    result = CliRunner().invoke(cli_main.cli, ["serve"])
    assert result.exit_code == 0
    assert serve_spy[0]["log_config"] is None


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
    monkeypatch.setattr(cli_main, "StandaloneTelegramClient", FakeStandaloneTelegramClient)

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


def test_profiles_command_lists_saved(monkeypatch, tmp_path):
    from tg_messenger.core.auth import SessionStore

    monkeypatch.setenv("TG_SESSION_DIR", str(tmp_path))
    store = SessionStore(tmp_path)
    store.save("alice", _valid_session_for_import())
    store.save("bob", _valid_session_for_import())
    monkeypatch.setattr(cli_main, "_session_store", lambda: SessionStore(tmp_path))
    result = CliRunner().invoke(cli_main.cli, ["profiles"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "bob" in result.output


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
    assert captured.get("session_name") == "bob"


# --- цикл 61: serve/tui учитывают глобальный --profile ---

def test_serve_uses_global_profile_as_session(monkeypatch):
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    monkeypatch.setattr(
        "tg_messenger.web.app.build_app",
        lambda **kw: captured.update(kw) or object(),
    )
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "serve"])
    assert result.exit_code == 0, result.output
    assert captured.get("session_name") == "work"


def test_tui_uses_global_profile_as_session(monkeypatch):
    captured = {}

    class FakeTUI:
        def __init__(self, *, session_name="default"):
            captured["session_name"] = session_name

        def run(self):
            pass

    monkeypatch.setattr("tg_messenger.tui.app.MessengerTUI", FakeTUI)
    result = CliRunner().invoke(cli_main.cli, ["--profile", "work", "tui"])
    assert result.exit_code == 0, result.output
    assert captured.get("session_name") == "work"
