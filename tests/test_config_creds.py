"""Tests for #188 Axis C — saving TG_API_ID/TG_API_HASH to ~/.tg/.env.

Covers the merge-writer (:func:`tg_messenger.core.dotenv.write_env_values`), the
``tg-messenger config set-api`` command, and the ``login`` auto-prompt fallback.
Every test drives the on-disk target off ``tmp_path`` (via the ``path`` kwarg or a
``paths.DEFAULT_HOME`` monkeypatch) — the real ``~/.tg`` is never touched.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from tg_messenger.cli import main as cli_main
from tg_messenger.cli.commands import auth as auth_cmd
from tg_messenger.cli.commands import config as config_cmd
from tg_messenger.core import dotenv
from tg_messenger.core import paths as core_paths

# --------------------------------------------------------------------------------------
# write_env_values
# --------------------------------------------------------------------------------------

def test_write_env_values_writes_both_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    out = dotenv.write_env_values(
        {"TG_API_ID": "1234567", "TG_API_HASH": "deadbeefcafe"}, path=env
    )
    assert out == env
    pairs = _read_kv(env)
    assert pairs["TG_API_ID"] == "1234567"
    assert pairs["TG_API_HASH"] == "deadbeefcafe"


def test_write_env_values_preserves_other_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(
        "SESSION_ENCRYPTION_KEY=keepme-fernet-key\nTG_SEND_RATE=20\n",
        encoding="utf-8",
    )
    dotenv.write_env_values(
        {"TG_API_ID": "1234567", "TG_API_HASH": "deadbeefcafe"}, path=env
    )
    pairs = _read_kv(env)
    # the creds were written…
    assert pairs["TG_API_ID"] == "1234567"
    assert pairs["TG_API_HASH"] == "deadbeefcafe"
    # …and the unrelated keys are untouched (NOT clobbered).
    assert pairs["SESSION_ENCRYPTION_KEY"] == "keepme-fernet-key"
    assert pairs["TG_SEND_RATE"] == "20"


def test_write_env_values_creates_dir_and_file_at_0700_0600(tmp_path: Path) -> None:
    # dir does NOT exist yet — writer must create it at 0700 and the file at 0600.
    target_dir = tmp_path / "nested" / "tg"
    assert not target_dir.exists()
    env = target_dir / ".env"
    dotenv.write_env_values({"TG_API_ID": "1", "TG_API_HASH": "h"}, path=env)
    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_write_env_values_second_call_updates_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    dotenv.write_env_values({"TG_API_ID": "111", "TG_API_HASH": "aaa"}, path=env)
    dotenv.write_env_values({"TG_API_ID": "222"}, path=env)  # only api_id changes
    text = env.read_text(encoding="utf-8")
    # no duplicate KEY lines, the new value wins, the untouched key survives
    assert text.count("TG_API_ID=") == 1
    assert text.count("TG_API_HASH=") == 1
    pairs = _read_kv(env)
    assert pairs["TG_API_ID"] == "222"
    assert pairs["TG_API_HASH"] == "aaa"


def test_write_env_values_default_target_uses_default_home(tmp_path, monkeypatch) -> None:
    # the default (path=None) writes to DEFAULT_HOME/.env; monkeypatch reaches it live
    monkeypatch.setattr(core_paths, "DEFAULT_HOME", tmp_path)
    out = dotenv.write_env_values({"TG_API_ID": "42", "TG_API_HASH": "hh"})
    assert out == tmp_path / ".env"
    pairs = _read_kv(out)
    assert pairs["TG_API_ID"] == "42"
    assert pairs["TG_API_HASH"] == "hh"


# --------------------------------------------------------------------------------------
# tg-messenger config set-api
# --------------------------------------------------------------------------------------

@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Point DEFAULT_HOME at tmp_path + clear any TG_API_* from the real env."""
    monkeypatch.setattr(core_paths, "DEFAULT_HOME", tmp_path)
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    return tmp_path / ".env"


def test_config_set_api_via_flags(isolated_env) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_main.cli, ["config", "set-api", "--api-id", "1234567", "--api-hash", "deadbeef"]
    )
    assert result.exit_code == 0, result.output
    pairs = _read_kv(isolated_env)
    assert pairs["TG_API_ID"] == "1234567"
    assert pairs["TG_API_HASH"] == "deadbeef"


def test_config_set_api_values_not_echoed(isolated_env) -> None:
    # success names the file but NEVER the credential values
    runner = CliRunner()
    result = runner.invoke(
        cli_main.cli, ["config", "set-api", "--api-id", "1234567", "--api-hash", "S3CRET-HASH-XYZ"]
    )
    assert result.exit_code == 0, result.output
    assert "1234567" not in result.output
    assert "S3CRET-HASH-XYZ" not in result.output
    assert ".env" in result.output  # does name the file written


def test_config_set_api_interactive_prompt(isolated_env) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_main.cli,
        ["config", "set-api"],
        # two prompts: api_id then api_hash (hidden)
        input="9876543\nmyhashvalue\n",
    )
    assert result.exit_code == 0, result.output
    pairs = _read_kv(isolated_env)
    assert pairs["TG_API_ID"] == "9876543"
    assert pairs["TG_API_HASH"] == "myhashvalue"


def test_config_set_api_bad_api_id_is_click_exception(isolated_env) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_main.cli, ["config", "set-api", "--api-id", "not-a-number", "--api-hash", "ok"]
    )
    assert isinstance(result.exception, SystemExit) or result.exit_code != 0
    # the friendly hint lives in the exception message, never the raw values
    assert "my.telegram.org" in (result.output + str(result.exception))


def test_config_set_api_blank_api_hash_is_click_exception(isolated_env) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_main.cli, ["config", "set-api", "--api-id", "1234567", "--api-hash", "   "]
    )
    assert result.exit_code != 0


# --------------------------------------------------------------------------------------
# login auto-prompt fallback
# --------------------------------------------------------------------------------------

def test_maybe_prompt_saves_and_folds_into_env_when_missing_and_tty(
    isolated_env, monkeypatch
) -> None:
    # creds are genuinely missing from the env (isolated_env fixture cleared them)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # pretend stdin is interactive

    # a fake click.prompt that returns canned answers (api_id, then api_hash)
    answers = iter(["1234567", "myhashvalue"])
    monkeypatch.setattr(
        config_cmd.click, "prompt", lambda *a, **k: next(answers)
    )

    auth_cmd._maybe_prompt_for_creds()

    # creds persisted to the on-disk env
    pairs = _read_kv(isolated_env)
    assert pairs["TG_API_ID"] == "1234567"
    assert pairs["TG_API_HASH"] == "myhashvalue"
    # folded into the LIVE process env so login proceeds without a restart
    assert os.environ.get("TG_API_ID") == "1234567"
    assert os.environ.get("TG_API_HASH") == "myhashvalue"


def test_maybe_prompt_noop_when_creds_present(isolated_env, monkeypatch) -> None:
    # creds present in env -> the prompt must NOT fire (no prompter is even callable)
    monkeypatch.setenv("TG_API_ID", "55555")
    monkeypatch.setenv("TG_API_HASH", "already-here")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("prompt should not fire when creds are present")

    monkeypatch.setattr(config_cmd.click, "prompt", _boom)
    auth_cmd._maybe_prompt_for_creds()  # returns without prompting

    assert not isolated_env.exists()  # nothing written


def test_maybe_prompt_noop_when_noninteractive(isolated_env, monkeypatch) -> None:
    # creds missing but stdin is NOT a tty -> no prompt, no hang, nothing written
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("prompt should not fire on non-interactive stdin")

    monkeypatch.setattr(config_cmd.click, "prompt", _boom)
    auth_cmd._maybe_prompt_for_creds()

    assert not isolated_env.exists()


def test_login_noninteractive_surfaces_missing_creds_error(isolated_env, monkeypatch) -> None:
    """End-to-end: missing creds + non-interactive stdin -> the friendly error, no hang."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    runner = CliRunner()
    result = runner.invoke(cli_main.cli, ["login", "--phone", "+10000000000"])

    # the friendly MissingCredentialsError surfaces (via ClickException wrapping), not a hang
    assert result.exit_code != 0
    out = result.output + str(result.exception)
    assert "Telegram API credentials" in out or "my.telegram.org" in out
    # nothing was written (no prompt fired on non-interactive stdin)
    assert not isolated_env.exists()


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def _read_kv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader for assertions (independent of the writer under test)."""
    pairs: dict[str, str] = {}
    if not path.exists():
        return pairs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip().strip("'\"")
    return pairs
