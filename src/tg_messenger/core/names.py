"""Shared naming helpers for filesystem-backed profile resources."""

from __future__ import annotations

import re

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_profile_name(name: str) -> str:
    """Return a filename-safe profile name; empty/unsafe-only names become default."""
    return _SAFE.sub("_", name).strip("._") or "default"


def is_safe_profile_name(name: str) -> bool:
    """Whether ``name`` is already its own canonical, filename-safe form.

    A name is safe only when it is non-empty AND ``sanitize_profile_name`` leaves it
    unchanged — so inputs that would silently collapse onto a *different* session file
    (``!!!``/``..``/``../default`` -> ``default``, ``work/personal`` -> ``work_personal``,
    a leading/trailing ``.``/``_``) are rejected rather than overwriting another account.
    Callers that accept a profile name from the user (e.g. the TUI account settings)
    gate on this before building a client; the CLI ``--profile`` path can reuse it too.
    """
    return bool(name) and sanitize_profile_name(name) == name
