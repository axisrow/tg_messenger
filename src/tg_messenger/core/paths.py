"""Single source of truth for the on-disk home root (sessions/logs/db).

Everything the app persists lives under ONE root, resolved by :func:`tg_home`.
The resolution is deliberately whole-root (not per-subdir) so ``sessions/``,
``logs/`` and ``*.db`` never split across ``~/.tg/`` and the legacy
``~/.tg_messenger/``. It is computed lazily (at point of use, never at import)
so ``TG_HOME`` and the legacy-fallback state are honored at runtime and tests
can monkeypatch ``HOME`` / the env.

The narrower sub-overrides ``TG_SESSION_DIR`` (auth) and ``TG_LOG_DIR``
(logsetup) still layer on top of whatever root this returns.
"""

from __future__ import annotations

import os
from pathlib import Path

LEGACY_HOME = Path.home() / ".tg_messenger"
DEFAULT_HOME = Path.home() / ".tg"


def tg_home() -> Path:
    """Root for sessions/logs/db.

    Order: ``$TG_HOME`` → legacy ``~/.tg_messenger`` (only if it exists AND
    ``~/.tg`` does not) → ``~/.tg``. Legacy is read in place, never moved: an
    existing ``~/.tg`` always wins so a partial new root can't silently pull
    from the old one.
    """
    env = os.environ.get("TG_HOME")
    if env:
        return Path(env)
    if not DEFAULT_HOME.exists() and LEGACY_HOME.exists():
        return LEGACY_HOME
    return DEFAULT_HOME
