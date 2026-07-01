"""Single source of truth for the on-disk home root (sessions/logs/db).

Everything the app persists lives under ONE root, resolved by :func:`tg_home`.
The resolution is deliberately whole-root (not per-subdir) so ``sessions/``,
``logs/`` and ``*.db`` never split across ``~/.tg/`` and the legacy
``~/.tg_messenger/``. It is computed lazily (at point of use, never at import)
so ``TG_HOME`` and the legacy-fallback state are honored at runtime. ``TG_HOME``
is read live each call (and tilde/``$VAR``-expanded); ``DEFAULT_HOME`` /
``LEGACY_HOME`` are bound at import, so tests monkeypatch those module
attributes (see ``test_paths.py``) rather than ``$HOME``.

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

    ``TG_HOME`` is ``~``/``$VAR``-expanded and must resolve to an absolute path.
    A misconfigured value (unset ``$VAR`` left literal, or a relative root) would
    otherwise scatter sessions/logs/db under the launch cwd instead of the home
    dir — so it fails closed with a clear error rather than silently writing
    Telegram auth state to the wrong place. A blank/whitespace-only value is
    treated as unset (falls through to the legacy/default resolution below).
    """
    env = (os.environ.get("TG_HOME") or "").strip()
    if env:
        # expand ~ / $VARS: TG_HOME is commonly set to "~/.tg" (incl. from a .env,
        # which is loaded verbatim into os.environ), and Path("~/.tg") would create
        # a literal ./~ tree under the cwd instead of the user's home.
        expanded = os.path.expanduser(os.path.expandvars(env))
        # expandvars leaves an undefined $VAR literal; that (or any relative value)
        # would resolve against cwd — reject it loudly instead of writing there.
        if "$" in expanded or not os.path.isabs(expanded):
            raise ValueError(
                f"TG_HOME must be an absolute path (after ~/$VAR expansion); got {env!r} "
                f"→ {expanded!r}. Set it to an absolute directory such as ~/.tg."
            )
        return Path(expanded)
    if not DEFAULT_HOME.exists() and LEGACY_HOME.exists():
        return LEGACY_HOME
    return DEFAULT_HOME
