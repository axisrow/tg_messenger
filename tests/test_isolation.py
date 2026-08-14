"""#229 — the suite must be isolated from the developer's real machine.

Without global isolation every ``CliRunner().invoke(cli, ...)`` runs the real
``_load_dotenv()``, which reads the repo-root ``.env`` (real API creds, LangSmith
keys) plus ``~/.tg/.env`` and mutates ``os.environ`` via ``setdefault`` — outside
monkeypatch, so the values survive into every later test. A later test reaching an
unpatched seam can then build a real client (real Telegram connection attempts were
observed) or touch the real profile DB path. These tests pin the conftest guarantees.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from tg_messenger.cli import main as cli_main

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cwd_is_not_the_repo_root():
    cwd = Path.cwd()
    assert cwd != _REPO_ROOT
    assert not (cwd / ".env").exists()  # nothing for _load_dotenv to pick up


def test_no_real_credentials_or_tracing_in_env():
    for var in (
        "TG_API_ID", "TG_API_HASH", "SESSION_ENCRYPTION_KEY",
        "LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT",
    ):
        assert var not in os.environ, f"{var} leaked into the test environment"


def test_home_roots_point_at_the_sandbox():
    from tg_messenger.core import paths as core_paths
    from tg_messenger.core.paths import tg_home

    home = Path.home()
    assert core_paths.DEFAULT_HOME != home / ".tg"
    assert core_paths.LEGACY_HOME != home / ".tg_messenger"
    assert tg_home() not in (home / ".tg", home / ".tg_messenger")


# The pair below is deliberately ORDER-DEPENDENT (same file, pytest runs top to bottom):
# step 1 invokes the real CLI entrypoint against a cwd .env — _load_dotenv loads it into
# os.environ via setdefault (NOT via monkeypatch); step 2 then asserts the conftest
# snapshot/restore rolled it back, so nothing leaks into later tests.


def test_cli_dotenv_load_is_contained_step1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TG_API_ID=990099\nTG_API_HASH=leakhash\n", encoding="utf-8")
    result = CliRunner().invoke(cli_main.cli, ["profiles"])
    assert result.exit_code == 0, result.output
    # loaded for THIS invocation — expected; the restore happens at test teardown
    assert os.environ.get("TG_API_ID") == "990099"


def test_cli_dotenv_load_is_contained_step2():
    assert "TG_API_ID" not in os.environ
    assert "TG_API_HASH" not in os.environ


def test_real_network_connections_are_blocked():
    # manage the socket explicitly: create_connection would leak it on the guard's
    # non-OSError and trip filterwarnings=error with a ResourceWarning
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(RuntimeError, match="real network"):
            sock.connect(("127.0.0.1", 1))


def test_real_e2e_collection_is_inert_without_explicit_opt_in():
    """A plain pytest collection must not read repo creds or inspect real sessions."""
    env = dict(os.environ)
    env.pop("TG_RUN_REAL_E2E", None)
    probe = (
        "import tests.test_e2e as e; "
        "assert e.RUN_REAL_E2E is False; "
        "assert e.DOTENV == {}; "
        "assert e.SESSION_FILE is None; "
        "assert e.HAS_CREDS is False; "
        "assert e.HAS_SESSION is False"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
