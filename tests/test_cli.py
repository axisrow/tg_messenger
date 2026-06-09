from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from tg_messenger.cli import main as cli_main
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.models import Dialog, Message


class StubClient:
    def __init__(self, **kw):
        self.sent = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def dialogs(self, dm_only=True):
        return [Dialog(id=7, title="Ann", username="ann", unread=2)]

    async def history(self, peer, limit=50, offset_id=0):
        return [
            Message(id=1, dialog_id=peer, sender_id=peer, out=False, text="hi",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        ]

    async def send_text(self, peer, text):
        self.sent.append((peer, text))
        return Message(id=2, dialog_id=peer, sender_id=1, out=True, text=text,
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


def test_flood_wait_friendly_message(runner, monkeypatch):
    r, stub = runner

    async def boom(peer, text):
        raise HandledFloodWaitError("send_text", 9999)

    monkeypatch.setattr(stub, "send_text", boom)
    result = r.invoke(cli_main.cli, ["send", "7", "hi"])
    assert result.exit_code != 0 or "flood" in result.output.lower()


def test_help_lists_commands():
    result = CliRunner().invoke(cli_main.cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("login", "dialogs", "read", "send", "listen", "serve", "tui"):
        assert cmd in result.output
