"""Web-auth cookie signing/validation + the failed-login delay (#24).

A signed cookie is ``"{expiry}:{hmac_hex}"`` — HMAC-SHA256 over the expiry with a per-process
random key, so a client cannot forge or extend it. Pure crypto/stdlib — no FastAPI. Re-exported
from ``tg_messenger.web.app`` so the auth middleware/login routes resolve these as module globals.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from hashlib import sha256

COOKIE_NAME = "tg_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days
# paths reachable without a valid cookie (the login wizard itself)
_PUBLIC_PATHS = frozenset({"/login", "/logout"})
# wrong-password penalty; injected so tests monkeypatch it to a no-op (no real sleep)
_WRONG_PASSWORD_DELAY = 1.0


async def _login_delay(seconds: float) -> None:
    """Sleep after a failed login. Tests monkeypatch this to a no-op."""
    await asyncio.sleep(seconds)


def _sign_cookie(key: bytes, *, expiry: int | None = None) -> str:
    """Build a signed cookie value ``"{expiry}:{hmac_hex}"``.

    The HMAC-SHA256 (per-process random ``key``) is taken over the expiry, so a
    client cannot forge or extend a cookie without the key.
    """
    if expiry is None:
        expiry = int(time.time()) + COOKIE_MAX_AGE
    mac = hmac.new(key, str(expiry).encode("ascii"), sha256).hexdigest()
    return f"{expiry}:{mac}"


def _valid_cookie(key: bytes, value: str | None) -> bool:
    """Constant-time validate a cookie: good signature AND not expired."""
    if not value or ":" not in value:
        return False
    expiry_str, _, mac = value.partition(":")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    expected = hmac.new(key, str(expiry).encode("ascii"), sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return False
    return expiry > int(time.time())
