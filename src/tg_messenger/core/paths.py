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
(logsetup) still layer on top of whatever root this returns; they go through the
same :func:`resolve_env_dir` validator (``~``/``$VAR`` expansion, absolute-path
fail-closed) so a misconfigured sub-override can't scatter auth state under cwd
either.
"""

from __future__ import annotations

import os
from pathlib import Path

LEGACY_HOME = Path.home() / ".tg_messenger"
DEFAULT_HOME = Path.home() / ".tg"


def resolve_env_dir(var: str) -> Path | None:
    """Resolve a directory-path env override (``TG_HOME``/``TG_SESSION_DIR``/``TG_LOG_DIR``).

    Returns the expanded absolute :class:`Path`, or ``None`` when the var is unset
    or blank (caller falls back to its default). Expands ``~`` and ``$VAR``, then
    fails closed with a clear :class:`ValueError` if the result is **not absolute**
    — a relative value (a plainly relative root, or an unset ``$VAR`` that
    ``expandvars`` left literal like ``$UNSET/sessions``) would otherwise resolve
    against the launch cwd and scatter sessions/logs/db (incl. Telegram auth
    state) there instead of the home dir.

    The check is deliberately just "is it absolute?", not "does it still contain a
    ``$``?": an unresolved ``$VAR`` and a legitimate literal ``$`` in a directory
    name are indistinguishable after expansion, so banning every ``$`` would
    falsely reject a valid absolute path like ``/srv/u$er/.tg``. The dangerous
    case — writing under cwd — is exactly the non-absolute one, which this catches.
    """
    raw = (os.environ.get(var) or "").strip()
    if not raw:
        return None
    expanded = os.path.expanduser(os.path.expandvars(raw))
    if not os.path.isabs(expanded):
        raise ValueError(
            f"{var} must be an absolute path (after ~/$VAR expansion); got {raw!r} "
            f"→ {expanded!r}. Set it to an absolute directory such as ~/.tg."
        )
    return Path(expanded)


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
    env = resolve_env_dir("TG_HOME")
    if env is not None:
        return env
    if not DEFAULT_HOME.exists() and LEGACY_HOME.exists():
        return LEGACY_HOME
    return DEFAULT_HOME
