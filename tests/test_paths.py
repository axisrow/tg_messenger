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


def test_legacy_fallback_only_when_default_absent(homes):
    # ~/.tg absent AND ~/.tg_messenger present → read the legacy root in place
    default_home, legacy_home = homes
    legacy_home.mkdir()
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
