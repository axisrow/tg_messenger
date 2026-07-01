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

The memo only fixes *same-process* ordering, though. Across separate runs a bare,
**empty** ``~/.tg`` left behind by a prior process (again, that ``TG_LOG_DIR`` mkdir
is the usual culprit) would, on the next run, still be picked over a populated
legacy root. So the legacy-vs-default test treats an empty ``~/.tg`` as absent (see
:func:`_has_data`): legacy wins when ``~/.tg`` has no data AND ``~/.tg_messenger``
does. A ``~/.tg`` that actually holds data always wins (the user adopted it).

A ``~/.tg`` holding ONLY ``.env`` counts as absent too (#188 Axis B): the config
docs tell users to create ``~/.tg/.env`` for their creds, and that lone file making
the root non-empty would otherwise flip a legacy user off their existing session —
the docs manufacturing the very data loss they were meant to fix. Real data next to
the ``.env`` (a ``sessions/`` dir, ``*.db``, ``logs/``) still counts as adoption.
"""

from __future__ import annotations

import os
from pathlib import Path

LEGACY_HOME = Path.home() / ".tg_messenger"
DEFAULT_HOME = Path.home() / ".tg"

# per-process memo of the resolved root: frozen on first tg_home() call so a later
# subdir mkdir (e.g. from a TG_LOG_DIR under ~/.tg) can't flip the legacy fallback.
_ROOT_CACHE: Path | None = None


# A config-only ``.env`` does not make a root "adopted" — see _has_data. Axis B
# (#188) tells users to put creds in ``~/.tg/.env``; that file alone must not flip a
# legacy user off their real session dir.
_NON_DATA_NAMES = frozenset({".env"})


def _has_data(p: Path) -> bool:
    """Whether ``p`` is a directory that holds actual DATA (sessions/logs/db), not
    just configuration.

    Used for the legacy-vs-default decision:

    - A bare/empty ``~/.tg`` (a residue a prior process left behind — e.g. a
      ``TG_LOG_DIR=~/.tg/logs`` mkdir that was then emptied, or any stray dir) must
      NOT count as "the user adopted the new root", or it would strand a real session
      sitting in ``~/.tg_messenger``.
    - A ``~/.tg`` holding ONLY ``.env`` doesn't count either (#188 Axis B): the docs
      tell users to create ``~/.tg/.env`` for their creds, and that lone config file
      making the root non-empty would flip a legacy user off their existing session —
      our own instructions manufacturing the data loss. Real data (a ``sessions/``
      dir, ``*.db``, ``logs/``) alongside the ``.env`` still counts as adoption.

    A permission error on ``iterdir`` fails safe as "no data" so resolution never
    crashes.
    """
    try:
        return p.is_dir() and any(child.name not in _NON_DATA_NAMES for child in p.iterdir())
    except OSError:
        return False


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

    Order: ``$TG_HOME`` → legacy ``~/.tg_messenger`` (only if it holds data AND
    ``~/.tg`` holds none) → ``~/.tg``. Legacy is read in place, never moved: a
    ``~/.tg`` that actually holds data always wins so a partial new root can't
    silently pull from the old one. An empty ``~/.tg`` (a residue left by a prior
    process — see :func:`_has_data` and the module docstring) counts as absent, so
    it can't strand a real session living in the legacy root.

    ``TG_HOME`` is ``~``/``$VAR``-expanded and must resolve to an absolute path.
    A misconfigured value (unset ``$VAR`` left literal, or a relative root) would
    otherwise scatter sessions/logs/db under the launch cwd instead of the home
    dir — so it fails closed with a clear error rather than silently writing
    Telegram auth state to the wrong place. A blank/whitespace-only value is
    treated as unset (falls through to the legacy/default resolution below).

    Only the *fallback* decision (legacy vs default, which reads live FS state) is
    memoized for the process — see the module docstring and
    :func:`reset_tg_home_cache` — so a subdir created after the first call can't
    change it. ``TG_HOME`` itself is re-read and re-validated on EVERY call (never
    cached): an explicit override always wins over a frozen fallback, and a
    misconfigured value fails closed with a :class:`ValueError` every time rather
    than being masked by a previously-cached root.
    """
    global _ROOT_CACHE
    # Always evaluate TG_HOME first — BEFORE the cache short-circuit. An explicit,
    # valid override wins on every call; an invalid one raises every call (matching
    # the fail-closed contract); only its ABSENCE lets the memoized fallback stand.
    env = resolve_env_dir("TG_HOME")
    if env is not None:
        return env
    if _ROOT_CACHE is not None:
        return _ROOT_CACHE
    # No TG_HOME: freeze the legacy-vs-default choice from the honest on-disk state
    # now, so a later subdir mkdir (e.g. TG_LOG_DIR under ~/.tg) can't flip it.
    # A bare/EMPTY ~/.tg counts as "absent" here: only a ~/.tg that actually holds
    # data means the user adopted the new root. Otherwise a prior process's empty
    # ~/.tg residue would strand a real session living in ~/.tg_messenger.
    if not _has_data(DEFAULT_HOME) and _has_data(LEGACY_HOME):
        _ROOT_CACHE = LEGACY_HOME
    else:
        _ROOT_CACHE = DEFAULT_HOME
    return _ROOT_CACHE
