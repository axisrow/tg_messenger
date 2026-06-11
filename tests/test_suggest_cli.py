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
    def __init__(self):
        self.connected = 0
        self.closed = 0
        self.migrations = []

    def register_migrations(self, statements):
        self.migrations.extend(statements)

    async def connect(self):
        self.connected += 1

    async def close(self):
        self.closed += 1


@pytest.fixture
def suggest_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = StubClient()
    suggester = StubSuggester()
    storage_profiles: list[str] = []
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    monkeypatch.setattr(
        cli_main,
        "make_storage",
        lambda profile="default": storage_profiles.append(profile) or StubStorage(),
    )
    monkeypatch.setattr(cli_main, "make_suggester", lambda c, **kw: suggester)
    return CliRunner(), client, suggester, storage_profiles


def test_suggest_prints_draft(suggest_cli):
    r, client, suggester, _ = suggest_cli
    result = r.invoke(cli_main.cli, ["suggest", "42"])
    assert result.exit_code == 0, result.output
    assert "draft text" in result.output
    assert suggester.suggested == [42]
    assert client.sent == []  # без --send только печать


def test_suggest_send_sends_via_client(suggest_cli):
    r, client, suggester, _ = suggest_cli
    result = r.invoke(cli_main.cli, ["suggest", "42", "--send"])
    assert result.exit_code == 0, result.output
    assert client.sent == [{"peer": 42, "text": "draft text"}]


def test_suggest_learn_builds_and_saves(suggest_cli):
    r, client, suggester, _ = suggest_cli
    result = r.invoke(cli_main.cli, ["suggest", "--learn", "42"])
    assert result.exit_code == 0, result.output
    assert suggester.learned == [42]
    assert suggester.suggested == []  # --learn не зовёт suggest


def test_suggest_uses_requested_session_storage(suggest_cli):
    r, _, _, storage_profiles = suggest_cli
    result = r.invoke(cli_main.cli, ["--profile", "work", "suggest", "42"])
    assert result.exit_code == 0, result.output
    assert storage_profiles == ["work"]


@pytest.mark.asyncio
async def test_make_optional_suggester_wires_profile_storage(monkeypatch):
    storage = StubStorage()
    storage_profiles: list[str] = []
    captured = {}
    inner = StubSuggester()

    monkeypatch.setattr(
        cli_main,
        "make_storage",
        lambda profile="default": storage_profiles.append(profile) or storage,
    )
    monkeypatch.setattr(
        cli_main,
        "make_suggester",
        lambda c, **kw: captured.update(kw) or inner,
    )

    suggester = cli_main.make_optional_suggester(StubClient(), session="work")

    assert storage_profiles == ["work"]
    assert captured["storage"] is storage
    assert suggester is not None

    assert await suggester.suggest(42) == "draft text"
    assert await suggester.suggest(43) == "draft text"
    assert storage.connected == 1
    assert inner.suggested == [42, 43]

    await suggester.close()
    assert storage.closed == 1


def test_suggest_listed_in_help(suggest_cli):
    r, *_ = suggest_cli
    result = r.invoke(cli_main.cli, ["--help"])
    assert "suggest" in result.output


def test_suggest_requires_login(suggest_cli):
    r, client, suggester, _ = suggest_cli
    client.authorized = False
    result = r.invoke(cli_main.cli, ["suggest", "42"])
    assert result.exit_code != 0
    assert "login" in result.output


def test_make_suggester_requires_model(monkeypatch, tmp_path):
    """Без TG_AGENT_MODEL make_suggester падает дружелюбной ClickException."""
    # this asserts the *model-missing* error, which only surfaces when the agent
    # stack IS installed (otherwise the [agent]-extra ImportError fires first)
    pytest.importorskip("langchain")
    import click

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TG_AGENT_MODEL", raising=False)
    monkeypatch.delenv("TG_AGENT_ALLOWLIST", raising=False)
    with pytest.raises(click.ClickException) as exc:
        cli_main.make_suggester(StubClient())
    assert "TG_AGENT_MODEL" in str(exc.value)


def test_make_suggester_does_not_require_agent_allowlist(monkeypatch, tmp_path):
    factory = pytest.importorskip("tg_messenger.agent.factory")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_AGENT_MODEL", "openai:gpt-5.4")
    monkeypatch.delenv("TG_AGENT_ALLOWLIST", raising=False)
    built = {}

    def fake_build_suggester(client, cfg, storage=None):
        built["cfg"] = cfg
        built["storage"] = storage
        return StubSuggester()

    monkeypatch.setattr(factory, "build_suggester", fake_build_suggester)

    storage = StubStorage()
    suggester = cli_main.make_suggester(StubClient(), storage=storage)

    assert isinstance(suggester, StubSuggester)
    assert built["cfg"].model == "openai:gpt-5.4"
    assert built["cfg"].allow_ids == frozenset()
    assert built["storage"] is storage


def test_make_suggester_wraps_provider_import_error(monkeypatch, tmp_path):
    factory = pytest.importorskip("tg_messenger.agent.factory")
    import click

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_AGENT_MODEL", "openai:gpt-5.4")
    monkeypatch.delenv("TG_AGENT_ALLOWLIST", raising=False)
    monkeypatch.setattr(
        factory,
        "build_suggester",
        lambda *a, **kw: (_ for _ in ()).throw(ImportError("missing provider")),
    )

    with pytest.raises(click.ClickException, match="missing provider"):
        cli_main.make_suggester(StubClient())


def test_suggest_missing_model_friendly_error(monkeypatch, tmp_path):
    pytest.importorskip("langchain")  # see test_make_suggester_requires_model
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    monkeypatch.delenv("TG_AGENT_MODEL", raising=False)
    monkeypatch.delenv("TG_AGENT_ALLOWLIST", raising=False)
    result = CliRunner().invoke(cli_main.cli, ["suggest", "42"])
    assert result.exit_code != 0
    assert "TG_AGENT_MODEL" in result.output
    assert "Traceback" not in result.output
