"""Interop with tg_content_factory — the ONLY place talking HTTP (httpx).

``core/`` never imports httpx; this package (optional ``[interop]`` extra) is the
single seam. ``agent/tools.py`` imports it lazily, inside the tool functions.
"""

from __future__ import annotations

from tg_messenger.interop.factory_client import FactoryClient, FactoryError, InteropTask

__all__ = ["FactoryClient", "FactoryError", "InteropTask"]
