import asyncio
import os
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from tg_messenger.cli import main as cli_main
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.models import Dialog, IncomingEvent, MediaRef, Message


class StubClient:
    def __init__(self, **kw):
        self.sent = []
        self.downloaded = []
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
        return [Dialog(id=7, title="Ann", username="ann", unread=2)]

    async def history(self, peer, limit=50, offset_id=0):
        if self.history_items is not None:
            return self.history_items
        return [
            Message(id=1, dialog_id=peer, sender_id=peer, out=False, text="hi",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        ]

    async def download_message_media(self, peer, message_id, dest):
        self.downloaded.append((message_id, str(dest)))
        return str(dest)

    async def send_text(self, peer, text):
        self.sent.append((peer, text))
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def send_media(self, peer, file_path, caption=None):
        self.sent.append((peer, "file", str(file_path), caption))
        return Message(id=3, dialog_id=peer, sender_id=1, out=True, text=caption,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))


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


def test_read_prints_history(runner):
    r, _ = runner
    result = r.invoke(cli_main.cli, ["read", "7"])
    assert result.exit_code == 0
    assert "hi" in result.output


def test_send_calls_client(runner):
    r, stub = runner
    result = r.invoke(cli_main.cli, ["send", "7", "hello"])
    assert result.exit_code == 0
    assert stub.sent == [(7, "hello")]


def test_send_file_uses_send_media(runner, tmp_path):
    r, stub = runner
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    result = r.invoke(cli_main.cli, ["send", "7", "caption", "--file", str(f)])
    assert result.exit_code == 0
    assert stub.sent[-1] == (7, "file", str(f), "caption")


def test_flood_wait_friendly_message(runner, monkeypatch):
    r, stub = runner

    async def boom(peer, text):
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
    assert (7, "hello") in stub.sent
    assert stub.connected is False


class FakeInnerLoginClient:
    """Stands in for the raw Telethon client used by LoginFlow."""

    def __init__(self, sign_in_error=None):
        self.sign_in_error = sign_in_error
        self.signed_in = []

    async def send_code_request(self, phone):
        return type("Sent", (), {"phone_code_hash": "h"})()

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


def test_help_lists_commands():
    result = CliRunner().invoke(cli_main.cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("login", "dialogs", "read", "send", "listen", "serve", "tui"):
        assert cmd in result.output
