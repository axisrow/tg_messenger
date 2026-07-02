"""tg_home() — the single on-disk root resolver (sessions/logs/db).

Resolution order is whole-root (never per-subdir): $TG_HOME → legacy
~/.tg_messenger (only if it exists AND ~/.tg does not) → ~/.tg. Every test drives
the decision off tmp_path via monkeypatching the module's DEFAULT_HOME/LEGACY_HOME
attributes and the TG_HOME env — the real home dir is never read or touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tg_messenger.core import paths


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Point DEFAULT_HOME/LEGACY_HOME at tmp_path and clear TG_HOME.

    Neither dir exists yet; a test creates whichever it needs with .mkdir().
    """
    default_home = tmp_path / ".tg"
    legacy_home = tmp_path / ".tg_messenger"
    monkeypatch.setattr(paths, "DEFAULT_HOME", default_home)
    monkeypatch.setattr(paths, "LEGACY_HOME", legacy_home)
    monkeypatch.delenv("TG_HOME", raising=False)
    return default_home, legacy_home


def test_tg_home_env_wins(homes, tmp_path, monkeypatch):
    # $TG_HOME beats everything — even a present legacy dir with no ~/.tg
    _default_home, legacy_home = homes
    legacy_home.mkdir()
    override = tmp_path / "custom-root"
    monkeypatch.setenv("TG_HOME", str(override))
    assert paths.tg_home() == override


def test_tg_home_expands_tilde(homes, monkeypatch):
    # TG_HOME=~/.tg (the value advertised in .env.example, loaded verbatim from a
    # .env into os.environ) must expand ~, not create a literal ./~ tree under cwd.
    monkeypatch.setenv("TG_HOME", "~/.tg")
    resolved = paths.tg_home()
    assert "~" not in str(resolved)
    assert resolved == Path.home() / ".tg"


def test_tg_home_expands_env_vars(homes, tmp_path, monkeypatch):
    # $VAR-style roots are also expanded (same os.environ round-trip as ~).
    monkeypatch.setenv("MY_ROOT", str(tmp_path))
    monkeypatch.setenv("TG_HOME", "$MY_ROOT/tg")
    assert paths.tg_home() == tmp_path / "tg"


def test_tg_home_undefined_var_rejected(homes, monkeypatch):
    # expandvars leaves an unset $VAR literal → a relative "$UNDEFINED/tg" under
    # cwd. Fail closed rather than silently write auth state to the wrong place.
    monkeypatch.delenv("UNDEFINED_ROOT", raising=False)
    monkeypatch.setenv("TG_HOME", "$UNDEFINED_ROOT/tg")
    with pytest.raises(ValueError, match="absolute path"):
        paths.tg_home()


def test_tg_home_relative_rejected(homes, monkeypatch):
    # a plainly relative TG_HOME would resolve against cwd → rejected
    monkeypatch.setenv("TG_HOME", "relative/tg")
    with pytest.raises(ValueError, match="absolute path"):
        paths.tg_home()


def test_tg_home_blank_is_treated_as_unset(homes, monkeypatch):
    # whitespace-only must not create a spaces-named tree; fall through to default
    default_home, _legacy_home = homes
    monkeypatch.setenv("TG_HOME", "   ")
    assert paths.tg_home() == default_home


def test_tg_home_literal_dollar_in_absolute_path_is_kept(homes, monkeypatch):
    # a legitimate absolute path with a literal '$' in a dir name (no such env var)
    # must NOT be rejected — only an UNRESOLVED $VAR reference is. Reject-any-'$'
    # would be a false positive (flagged by review).
    monkeypatch.delenv("literal", raising=False)  # ensure $literal stays literal
    monkeypatch.setenv("TG_HOME", "/tmp/tg-$literal")
    assert paths.tg_home() == Path("/tmp/tg-$literal")


# --- resolve_env_dir: the shared validator used by TG_HOME / TG_SESSION_DIR / TG_LOG_DIR ---

def test_resolve_env_dir_unset_returns_none(monkeypatch):
    monkeypatch.delenv("TG_SESSION_DIR", raising=False)
    assert paths.resolve_env_dir("TG_SESSION_DIR") is None


def test_resolve_env_dir_blank_returns_none(monkeypatch):
    monkeypatch.setenv("TG_SESSION_DIR", "  ")
    assert paths.resolve_env_dir("TG_SESSION_DIR") is None


def test_resolve_env_dir_expands_tilde(monkeypatch):
    monkeypatch.setenv("TG_SESSION_DIR", "~/sess")
    assert paths.resolve_env_dir("TG_SESSION_DIR") == Path.home() / "sess"


def test_resolve_env_dir_rejects_undefined_var(monkeypatch):
    # the sub-override leak Codex flagged: TG_SESSION_DIR=$UNSET/sessions would
    # write StringSession creds under cwd — fail closed instead.
    monkeypatch.delenv("UNSET_ROOT", raising=False)
    monkeypatch.setenv("TG_SESSION_DIR", "$UNSET_ROOT/sessions")
    with pytest.raises(ValueError, match="absolute path"):
        paths.resolve_env_dir("TG_SESSION_DIR")


def test_resolve_env_dir_rejects_relative(monkeypatch):
    monkeypatch.setenv("TG_LOG_DIR", "relative/logs")
    with pytest.raises(ValueError, match="absolute path"):
        paths.resolve_env_dir("TG_LOG_DIR")


def _populate(home):
    """Give a root dir some data so it counts as adopted (mirrors a real session file)."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "default.session").write_text("s", encoding="utf-8")


def test_legacy_fallback_only_when_default_absent(homes):
    # ~/.tg absent AND ~/.tg_messenger present-with-data → read the legacy root in place
    default_home, legacy_home = homes
    _populate(legacy_home)
    assert not default_home.exists()
    assert paths.tg_home() == legacy_home


def test_existing_default_beats_legacy(homes):
    # both present → the new root wins; sessions/logs/db never pull from legacy
    default_home, legacy_home = homes
    default_home.mkdir()
    legacy_home.mkdir()
    assert paths.tg_home() == default_home


def test_plain_default_when_neither_exists(homes):
    # nothing on disk → the default ~/.tg (created later at point of use)
    default_home, legacy_home = homes
    assert not default_home.exists()
    assert not legacy_home.exists()
    assert paths.tg_home() == default_home


def test_default_wins_even_if_only_default_exists(homes):
    # ~/.tg present, legacy absent → default (no fallback to consider)
    default_home, _legacy_home = homes
    default_home.mkdir()
    assert paths.tg_home() == default_home


# --- empty ~/.tg counts as absent, so a prior process's residue can't strand legacy ---

def test_empty_default_does_not_strand_populated_legacy(homes):
    # Regression (cross-process footgun): a prior run left an EMPTY ~/.tg (e.g. a
    # TG_LOG_DIR=~/.tg/logs mkdir that was emptied). The next run must still resolve
    # to the populated legacy root, not the empty ~/.tg, or the user looks logged out.
    default_home, legacy_home = homes
    _populate(legacy_home)          # real session lives here
    default_home.mkdir()            # empty ~/.tg residue
    assert not any(default_home.iterdir())
    assert paths.tg_home() == legacy_home


def test_empty_default_and_empty_legacy_pick_default(homes):
    # both present but empty → default (an empty legacy is no reason to prefer it)
    default_home, legacy_home = homes
    default_home.mkdir()
    legacy_home.mkdir()
    assert paths.tg_home() == default_home


def test_populated_default_beats_populated_legacy(homes):
    # ~/.tg with data always wins even when legacy also has data (user adopted it)
    default_home, legacy_home = homes
    _populate(legacy_home)
    _populate(default_home)
    assert paths.tg_home() == default_home


# --- a config-only ~/.tg/.env must NOT count as adoption (#188 Axis B review) ---


def test_default_holding_only_dotenv_does_not_strand_legacy(homes):
    # Regression (Codex, #190 review cycle 1): Axis B tells users to put creds in
    # ~/.tg/.env. Creating ONLY that file makes ~/.tg non-empty. If a bare .env counted
    # as "the user adopted ~/.tg", a legacy user with a real session in ~/.tg_messenger
    # would flip to the empty ~/.tg and look logged out — our own docs manufacturing the
    # data loss. A ~/.tg holding nothing but .env must still fall back to the legacy root.
    default_home, legacy_home = homes
    _populate(legacy_home)  # real session lives in legacy
    default_home.mkdir()
    (default_home / ".env").write_text("TG_API_ID=1\nTG_API_HASH=h\n", encoding="utf-8")
    assert paths.tg_home() == legacy_home


def test_default_with_dotenv_and_real_data_still_wins(homes):
    # The .env exemption is narrow: a ~/.tg that ALSO holds real data (a session, a db,
    # logs) is a genuinely adopted root and must still win over legacy.
    default_home, legacy_home = homes
    _populate(legacy_home)
    _populate(default_home)  # real session data in ~/.tg …
    (default_home / ".env").write_text("TG_API_ID=1\n", encoding="utf-8")  # … plus a .env
    assert paths.tg_home() == default_home


def test_default_holding_only_dotenv_still_beats_empty_legacy(homes):
    # No legacy data at all → the default root is still the answer even if it holds only
    # .env (there's nothing to strand; a config-only ~/.tg is where a fresh user lands).
    default_home, _legacy_home = homes
    default_home.mkdir()
    (default_home / ".env").write_text("TG_API_ID=1\n", encoding="utf-8")
    assert paths.tg_home() == default_home


# --- per-process memo: a subdir created AFTER the first resolve must not flip the root ---

def test_root_decision_frozen_against_later_default_creation(homes):
    # Regression (Codex, cycle 1 of the final review): a legacy user (~/.tg_messenger
    # present, ~/.tg absent) whose TG_LOG_DIR is under ~/.tg would have setup_logging
    # mkdir ~/.tg at startup, BEFORE sessions/db resolve. If tg_home() re-checked live
    # FS state it would then flip to the empty ~/.tg and hide the existing session.
    # Freezing the decision on the first call keeps every later lookup on legacy.
    default_home, legacy_home = homes
    _populate(legacy_home)
    assert paths.tg_home() == legacy_home  # first resolve: legacy (honest state)
    # a later subdir mkdir creates ~/.tg (as setup_logging on TG_LOG_DIR=~/.tg/logs would)
    (default_home / "logs").mkdir(parents=True)
    assert default_home.exists()
    # the frozen decision must NOT flip — sessions/db stay on the legacy root
    assert paths.tg_home() == legacy_home


def test_reset_tg_home_cache_re_resolves(homes):
    # the cache is per-process; reset lets a test (or a re-config) re-decide
    default_home, legacy_home = homes
    _populate(legacy_home)
    assert paths.tg_home() == legacy_home
    _populate(default_home)  # ~/.tg now holds data → it's the adopted root
    paths.reset_tg_home_cache()
    # after a reset + a now-populated ~/.tg, the fresh resolve prefers the default root
    assert paths.tg_home() == default_home


def test_cached_root_does_not_suppress_a_later_bad_tg_home(homes, tmp_path, monkeypatch):
    # Regression (Codex, cycle 2): TG_HOME is validated on EVERY call, before the
    # cache short-circuit — a valid root cached first must NOT mask a later invalid
    # TG_HOME (the memo only freezes the no-TG_HOME fallback decision, never TG_HOME).
    monkeypatch.setenv("TG_HOME", str(tmp_path / "good-root"))
    # a valid TG_HOME returns via the env branch (nothing is memoized here — only
    # the no-TG_HOME fallback is); the point is the NEXT call re-validates from scratch
    assert paths.tg_home() == tmp_path / "good-root"
    monkeypatch.setenv("TG_HOME", "relative-bad")
    with pytest.raises(ValueError, match="absolute path"):
        paths.tg_home()


def test_valid_tg_home_always_wins_over_frozen_fallback(homes, tmp_path, monkeypatch):
    # freeze a fallback root (no TG_HOME) first, then set a valid TG_HOME: the
    # explicit override must win on the next call, not the cached fallback.
    default_home, _legacy_home = homes
    assert paths.tg_home() == default_home  # frozen fallback
    override = tmp_path / "explicit-root"
    monkeypatch.setenv("TG_HOME", str(override))
    assert paths.tg_home() == override
