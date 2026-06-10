"""Цикл 95: CLI-команда `suggest` (#17) — печать/отправка черновика, обучение.

make_suggester — seam (как make_agent_runner): тесты подменяют его на фейк,
LLM-стек не вызывается. fail-fast при невыставленном TG_AGENT_MODEL.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tg_messenger.cli import main as cli_main


class StubClient:
    def __init__(self, **kw):
        self.connected = False
        self.authorized = True
        self.sent: list[dict] = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_authorized(self):
        return self.authorized

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append({"peer": peer, "text": text})


class StubSuggester:
    def __init__(self, draft="draft text"):
        self.draft = draft
        self.suggested: list[int] = []
        self.learned: list[int] = []

    async def suggest(self, dialog_id):
        self.suggested.append(dialog_id)
        return self.draft

    async def learn(self, dialog_id):
        self.learned.append(dialog_id)
        from tg_messenger.agent.suggest import StyleProfile

        return StyleProfile(avg_length=5.0)


class StubStorage:
    def register_migrations(self, statements):
        pass

    async def connect(self):
        pass

    async def close(self):
        pass


@pytest.fixture
def suggest_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = StubClient()
    suggester = StubSuggester()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    monkeypatch.setattr(cli_main, "make_storage", lambda *a, **kw: StubStorage())
    monkeypatch.setattr(cli_main, "make_suggester", lambda c, **kw: suggester)
    return CliRunner(), client, suggester


def test_suggest_prints_draft(suggest_cli):
    r, client, suggester = suggest_cli
    result = r.invoke(cli_main.cli, ["suggest", "42"])
    assert result.exit_code == 0, result.output
    assert "draft text" in result.output
    assert suggester.suggested == [42]
    assert client.sent == []  # без --send только печать


def test_suggest_send_sends_via_client(suggest_cli):
    r, client, suggester = suggest_cli
    result = r.invoke(cli_main.cli, ["suggest", "42", "--send"])
    assert result.exit_code == 0, result.output
    assert client.sent == [{"peer": 42, "text": "draft text"}]


def test_suggest_learn_builds_and_saves(suggest_cli):
    r, client, suggester = suggest_cli
    result = r.invoke(cli_main.cli, ["suggest", "--learn", "42"])
    assert result.exit_code == 0, result.output
    assert suggester.learned == [42]
    assert suggester.suggested == []  # --learn не зовёт suggest


def test_suggest_listed_in_help(suggest_cli):
    r, *_ = suggest_cli
    result = r.invoke(cli_main.cli, ["--help"])
    assert "suggest" in result.output


def test_suggest_requires_login(suggest_cli):
    r, client, suggester = suggest_cli
    client.authorized = False
    result = r.invoke(cli_main.cli, ["suggest", "42"])
    assert result.exit_code != 0
    assert "login" in result.output


def test_make_suggester_requires_model(monkeypatch, tmp_path):
    """Без TG_AGENT_MODEL make_suggester падает дружелюбной ClickException."""
    import click

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TG_AGENT_MODEL", raising=False)
    monkeypatch.delenv("TG_AGENT_ALLOWLIST", raising=False)
    with pytest.raises(click.ClickException) as exc:
        cli_main.make_suggester(StubClient())
    assert "TG_AGENT_MODEL" in str(exc.value)


def test_suggest_missing_model_friendly_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    monkeypatch.delenv("TG_AGENT_MODEL", raising=False)
    monkeypatch.delenv("TG_AGENT_ALLOWLIST", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["suggest", "42"])
    assert result.exit_code != 0
    assert "TG_AGENT_MODEL" in result.output
    assert "Traceback" not in result.output
