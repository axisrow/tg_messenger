"""Цикл 111: CLI-команда `worker` — опрос фабрики, исполнение задач.

Тестируется через CliRunner со стабами make_client / make_factory_client /
make_worker (seam). Сети и httpx нет; Ctrl+C завершает чисто.
"""

from __future__ import annotations

import sys

from click.testing import CliRunner

from tg_messenger.cli import main as cli_main


class StubClient:
    def __init__(self, **kw):
        self.connected = False
        self.authorized = True

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_authorized(self):
        return self.authorized


class StubWorker:
    last = None

    def __init__(self, client, factory, *, types=None, sleep=None, agent=None, idle_sleep=None):
        self.client = client
        self.factory = factory
        self.types = types
        self.runs = 0
        self.interrupt = False
        StubWorker.last = self

    async def run(self):
        self.runs += 1
        if self.interrupt:
            raise KeyboardInterrupt


def _setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = StubClient()
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: client)
    monkeypatch.setattr(cli_main, "make_factory_client", lambda **kw: object())
    monkeypatch.setattr("tg_messenger.interop.worker.Worker", StubWorker)
    return client


def test_worker_runs_and_disconnects(monkeypatch, tmp_path):
    client = _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main.cli, ["worker", "--factory-url", "http://f"])
    assert result.exit_code == 0, result.output
    assert StubWorker.last.runs == 1
    assert client.connected is False  # disconnect в finally


def test_worker_requires_login(monkeypatch, tmp_path):
    client = _setup(monkeypatch, tmp_path)
    client.authorized = False
    result = CliRunner().invoke(cli_main.cli, ["worker", "--factory-url", "http://f"])
    assert result.exit_code != 0
    assert "login" in result.output


def test_worker_ctrl_c_says_stopped(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    orig_init = StubWorker.__init__

    def _init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.interrupt = True

    monkeypatch.setattr(StubWorker, "__init__", _init)
    result = CliRunner().invoke(cli_main.cli, ["worker", "--factory-url", "http://f"])
    assert "stopped." in result.output


def test_worker_types_filter_passed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        cli_main.cli, ["worker", "--factory-url", "http://f", "--types", "dm_reply,fetch_history"]
    )
    assert result.exit_code == 0, result.output
    assert StubWorker.last.types == ["dm_reply", "fetch_history"]


def test_worker_requires_factory_url(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main.cli, ["worker"])
    assert result.exit_code != 0  # --factory-url обязателен (или из env)


def test_worker_missing_extra_gives_pip_hint(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "make_client", lambda **kw: StubClient())
    # import tg_messenger.interop -> ImportError, как при невыставленном [interop]
    monkeypatch.setitem(sys.modules, "tg_messenger.interop.worker", None)
    result = CliRunner().invoke(cli_main.cli, ["worker", "--factory-url", "http://f"])
    assert result.exit_code != 0
    assert "tg-messenger[interop]" in result.output
    assert "Traceback" not in result.output


def test_worker_listed_in_help(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli_main.cli, ["--help"])
    assert "worker" in result.output
