"""Shared naming helpers for filesystem-backed profile resources."""

from __future__ import annotations

import re

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_profile_name(name: str) -> str:
    """Return a filename-safe profile name; empty/unsafe-only names become default."""
    return _SAFE.sub("_", name).strip("._") or "default"
