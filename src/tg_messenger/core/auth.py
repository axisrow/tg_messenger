"""Session storage and interactive login flow.

StringSession is the source of truth (no SQLite). Sessions live as plain text
files (mode 0600) under a package-local directory; an externally supplied
session string can be wrapped without touching disk.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from telethon.sessions import StringSession

DEFAULT_SESSION_DIR = Path.home() / ".tg_messenger" / "sessions"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("._") or "default"
    return cleaned


def _validate_session_string(session_string: str) -> str:
    """Ensure a StringSession parses (auth_key/dc_id present); raise ValueError if not."""
    try:
        StringSession(session_string)
    except Exception as exc:  # telethon raises ValueError/binascii errors on garbage
        raise ValueError("invalid StringSession") from exc
    return session_string


class SessionStore:
    """Persist/load StringSession strings as private files."""

    def __init__(self, session_dir: Path | str = DEFAULT_SESSION_DIR):
        self.session_dir = Path(session_dir)

    def path_for(self, name: str) -> Path:
        return self.session_dir / f"{_sanitize(name)}.session"

    def load(self, name: str) -> str | None:
        path = self.path_for(name)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return _validate_session_string(raw)

    def save(self, name: str, session_string: str) -> Path:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(name)
        path.write_text(session_string, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def from_external(self, session_string: str) -> str:
        """Validate and return the session string verbatim — never written to disk."""
        return _validate_session_string(session_string)


class LoginFlow:
    """Two-step interactive sign-in over a connected Telethon client.

    The phone_code_hash from ``send_code`` is kept on the same client/session,
    matching MTProto's requirement that the hash binds to the session.
    """

    def __init__(self, client):
        self._client = client
        self._phone: str | None = None
        self._code_hash: str | None = None

    async def send_code(self, phone: str) -> None:
        sent = await self._client.send_code_request(phone)
        self._phone = phone
        self._code_hash = getattr(sent, "phone_code_hash", None)

    async def sign_in(self, code: str):
        if self._phone is None:
            raise RuntimeError("send_code must be called before sign_in")
        return await self._client.sign_in(
            phone=self._phone,
            code=code,
            phone_code_hash=self._code_hash,
        )

    async def check_password(self, password: str):
        if self._phone is None:
            raise RuntimeError("send_code must be called before check_password")
        return await self._client.sign_in(password=password)
