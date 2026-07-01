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

The legacy-vs-default decision is **cached per process** (:func:`tg_home` memoizes
its first result). Without that, a sub-override that creates ``~/.tg`` as a side
effect — e.g. ``TG_LOG_DIR=~/.tg/logs`` makes ``setup_logging`` ``mkdir`` it at
startup, *before* sessions/db are resolved — would flip a legacy user's later
session/db lookup from ``~/.tg_messenger`` to a fresh empty ``~/.tg`` (they'd look
logged out). Freezing the decision on first use keeps the whole-root choice stable
regardless of which subdir gets created first. Tests reset the cache via
:func:`reset_tg_home_cache` (an autouse conftest fixture does this per test).
"""

from __future__ import annotations

import os
from pathlib import Path

LEGACY_HOME = Path.home() / ".tg_messenger"
DEFAULT_HOME = Path.home() / ".tg"

# per-process memo of the resolved root: frozen on first tg_home() call so a later
# subdir mkdir (e.g. from a TG_LOG_DIR under ~/.tg) can't flip the legacy fallback.
_ROOT_CACHE: Path | None = None


def reset_tg_home_cache() -> None:
    """Clear the memoized root so the next :func:`tg_home` re-resolves from scratch.

    For tests that monkeypatch ``TG_HOME`` / ``DEFAULT_HOME`` / ``LEGACY_HOME``
    between cases; production never needs it (the root is stable for a run).
    """
    global _ROOT_CACHE
    _ROOT_CACHE = None


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

    The resolved root is memoized for the process (see module docstring and
    :func:`reset_tg_home_cache`) so a subdir created after the first call can't
    change the answer. A ``TG_HOME`` validation error is raised on every call (not
    cached), so a misconfigured override never silently resolves to a default.
    """
    global _ROOT_CACHE
    if _ROOT_CACHE is not None:
        return _ROOT_CACHE
    # resolve_env_dir raises on a bad TG_HOME — deliberately BEFORE touching the
    # cache, so an invalid override fails loudly on every call rather than once.
    env = resolve_env_dir("TG_HOME")
    if env is not None:
        root = env
    elif not DEFAULT_HOME.exists() and LEGACY_HOME.exists():
        root = LEGACY_HOME
    else:
        root = DEFAULT_HOME
    _ROOT_CACHE = root
    return root
