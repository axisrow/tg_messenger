"""Tests for scripts/import_factory_sessions.py's pure/testable helpers.

The script itself is a human-run migration helper (never wired into pytest's
subprocess execution, same rule as scripts/e2e/), but its logic is an
ordinary importable Python module — nothing prevents unit-testing the pure
helpers directly. Imported by file path since ``scripts/`` is not a package.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession


def _make_session(dc_id: int) -> str:
    s = StringSession()
    s.set_dc(dc_id, "149.154.167.51", 443)
    s.auth_key = AuthKey(bytes([dc_id]) * 256)
    return s.save()

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "import_factory_sessions.py"
_spec = importlib.util.spec_from_file_location("import_factory_sessions", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
import_factory_sessions = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = import_factory_sessions
_spec.loader.exec_module(import_factory_sessions)

profile_name = import_factory_sessions.profile_name


def test_profile_name_strips_leading_plus():
    assert profile_name("factory_", "+123456") == "factory_123456"


@pytest.mark.parametrize("bad_phone", [None, "", "   "])
def test_profile_name_returns_none_for_missing_phone(bad_phone):
    # A NULL/blank phone must never crash the import loop — it's just an
    # unusable row, like a decrypt failure elsewhere in the same loop.
    assert profile_name("factory_", bad_phone) is None


def test_profile_name_non_canonical_prefix_is_rejected_before_save():
    """Reproduces the Codex-reported overwrite bug (round 1, cycle 1):

    ``--prefix "../"`` + an existing profile's bare name produces a raw
    profile string that ``sanitize_profile_name`` collapses onto that EXISTING
    profile's canonical file — bypassing an `in existing` guard that compares
    against the raw (non-canonical) name instead of the sanitized one.
    """
    from tg_messenger.core.names import is_safe_profile_name, sanitize_profile_name

    raw = profile_name("../", "existing")
    assert raw == "../existing"
    # The bug: comparing `raw` against a set of *canonical* names never matches,
    # even though saving `raw` silently lands on the canonical "existing" file.
    assert raw not in {"existing"}
    assert sanitize_profile_name(raw) == "existing"
    # The fix in main(): every generated name must pass this check before any
    # existence check or save — a non-canonical name is refused outright.
    assert is_safe_profile_name(raw) is False


def _make_factory_db(tmp_path: Path, rows: list[tuple[int, str | None, int, str]]) -> Path:
    db_path = tmp_path / "factory.db"
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE accounts (id INTEGER, phone TEXT, is_primary INTEGER, session_string TEXT)"
        )
        con.executemany("INSERT INTO accounts VALUES (?, ?, ?, ?)", rows)
        con.commit()
    finally:
        con.close()
    return db_path


def test_main_never_overwrites_an_existing_profile_via_crafted_prefix(tmp_path, capsys):
    """End-to-end mutation guard: drives the real main() loop, not just the
    pure helper, so a regression in the wiring (not just profile_name/
    is_safe_profile_name themselves) still fails this test."""
    from tg_messenger.core.auth import SessionStore
    from tg_messenger.core.session_cipher import encrypt_session

    key = "test-key-not-a-real-secret"
    session_dir = tmp_path / "sessions"
    store = SessionStore(session_dir=session_dir, encryption_key=key)
    original_session = _make_session(dc_id=2)
    store.save("existing", original_session)

    attacker_session = _make_session(dc_id=4)
    db_path = _make_factory_db(
        tmp_path,
        [(1, "existing", 0, encrypt_session(attacker_session, key))],
    )

    argv = [
        "import_factory_sessions.py",
        "--db",
        str(db_path),
        "--key",
        key,
        "--prefix",
        "../",
        "--session-dir",
        str(session_dir),
        "--apply",
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        rc = import_factory_sessions.main()
    finally:
        sys.argv = old_argv

    assert rc == 0
    # The attacker-controlled row must be refused, not silently collapsed onto
    # the existing "existing" profile.
    assert store.load("existing") == original_session
    out = capsys.readouterr().out
    assert "not canonical" in out and "refusing" in out
