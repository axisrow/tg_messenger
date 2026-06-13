"""Session storage and interactive login flow.

StringSession is the source of truth (no SQLite). Sessions live as plain text
files (mode 0600) under a package-local directory; an externally supplied
session string can be wrapped without touching disk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import NamedTuple

from telethon.sessions import StringSession
from telethon.tl.functions.auth import ResendCodeRequest

from tg_messenger.core.flood import run_with_flood_wait_retry
from tg_messenger.core.names import sanitize_profile_name

logger = logging.getLogger(__name__)

DEFAULT_SESSION_DIR = Path.home() / ".tg_messenger" / "sessions"

# single source for the "you need to log in" UX hint, shared by all three UIs
LOGIN_HINT = "Not logged in. Run: tg-messenger login"


def validate_session_string(session_string: str) -> str:
    """Ensure a StringSession parses (auth_key/dc_id present); raise ValueError if not."""
    try:
        StringSession(session_string)
    except Exception as exc:  # telethon raises ValueError/binascii errors on garbage
        raise ValueError("invalid StringSession") from exc
    return session_string


def session_store_from_env() -> SessionStore:
    """SessionStore from the environment (``TG_SESSION_DIR`` dir override,
    ``SESSION_ENCRYPTION_KEY`` at-rest encryption) — one definition for CLI/web."""
    return SessionStore(
        os.environ.get("TG_SESSION_DIR") or DEFAULT_SESSION_DIR,
        encryption_key=os.environ.get("SESSION_ENCRYPTION_KEY") or None,
    )


class SessionStore:
    """Persist/load StringSession strings as private files.

    With an ``encryption_key`` (CLI/UIs pass ``SESSION_ENCRYPTION_KEY`` from env),
    sessions are stored as ``enc:v2:`` Fernet tokens — byte-compatible with
    tg_content_factory, so a shared key makes the two projects' sessions
    interchangeable (SSO). Reading a plaintext file while a key is set lazily
    rewrites it encrypted; reading an encrypted file with no key raises with a hint.
    Session strings are never logged.
    """

    def __init__(
        self,
        session_dir: Path | str = DEFAULT_SESSION_DIR,
        *,
        encryption_key: str | None = None,
    ):
        self.session_dir = Path(session_dir)
        self._encryption_key = encryption_key or None  # treat "" as no key

    def path_for(self, name: str) -> Path:
        return self.session_dir / f"{sanitize_profile_name(name)}.session"

    def load(self, name: str) -> str | None:
        from tg_messenger.core.session_cipher import decrypt_session, is_encrypted

        path = self.path_for(name)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        if is_encrypted(raw) and not self._encryption_key:
            raise ValueError(
                f"session {name!r} is encrypted but SESSION_ENCRYPTION_KEY is not set"
            )
        plaintext = decrypt_session(raw, self._encryption_key) if self._encryption_key else raw
        validated = validate_session_string(plaintext)
        # lazy migration: a plaintext file read under a key is rewritten encrypted
        if self._encryption_key and not is_encrypted(raw):
            self.save(name, validated)
        return validated

    def save(self, name: str, session_string: str) -> Path:
        from tg_messenger.core.session_cipher import encrypt_session

        self.session_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.session_dir, 0o700)
        path = self.path_for(name)
        stored = (
            encrypt_session(session_string, self._encryption_key)
            if self._encryption_key
            else session_string
        )
        path.write_text(stored, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def from_external(self, session_string: str) -> str:
        """Validate and return the session string verbatim — never written to disk."""
        return validate_session_string(session_string)

    def list_profiles(self) -> list[str]:
        """Sorted profile names = ``*.session`` files in the session dir (no extension)."""
        if not self.session_dir.is_dir():
            return []
        return sorted(p.stem for p in self.session_dir.glob("*.session") if p.is_file())

    def is_valid_profile(self, name: str) -> bool:
        """Whether ``name``'s session file is present and parseable — NO network (#52).

        Encryption-aware: an ``enc:v2:`` file with no key is present and not corrupt, so
        it counts as valid (we just can't decrypt it here). With a key, the session is
        decrypted and parsed. A missing/empty/garbage file is invalid.
        """
        from tg_messenger.core.session_cipher import is_encrypted

        path = self.path_for(name)
        if not path.is_file():
            return False
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not raw:
            return False
        # encrypted file without a key: present and intact, but undecryptable here → valid
        if is_encrypted(raw) and not self._encryption_key:
            return True
        try:
            self.load(name)
        except Exception:
            return False
        return True

    def delete(self, name: str) -> bool:
        """Remove the profile's session file; True if it existed (#11 lifecycle)."""
        path = self.path_for(name)
        if not path.is_file():
            return False
        path.unlink()
        return True


class CodeDelivery(NamedTuple):
    """Where the login code went and what a resend would use."""

    kind: str  # app / sms / call / unknown
    next_kind: str | None = None
    timeout: int | None = None  # seconds until resend is allowed, if Telegram said so


_DELIVERY_HINTS = {
    "app": "Код отправлен в приложение Telegram (проверьте чат «Telegram», отправитель 777000).",
    "sms": "Код отправлен по SMS.",
    "call": "Вам поступит звонок с кодом.",
}


def delivery_hint(delivery: CodeDelivery) -> str:
    """Human-readable 'where the code went' line for the web/TUI login wizards.

    The phone number is never part of this text — only the delivery channel.
    """
    msg = _DELIVERY_HINTS.get(delivery.kind, "Код отправлен — проверьте Telegram и SMS.")
    if delivery.next_kind:
        msg += f" Нет кода? Можно отправить повторно (канал: {delivery.next_kind})."
    return msg


def _kind_of(type_obj) -> str:
    name = type(type_obj).__name__.lower()
    for kind in ("app", "sms", "call"):
        if kind in name:
            return kind
    return "unknown"


class LoginFlow:
    """Two-step interactive sign-in over a connected Telethon client.

    The phone_code_hash from ``send_code`` is kept on the same client/session,
    matching MTProto's requirement that the hash binds to the session.
    """

    def __init__(self, client):
        self._client = client
        self._phone: str | None = None
        self._code_hash: str | None = None

    def _bind(self, sent) -> CodeDelivery:
        self._code_hash = getattr(sent, "phone_code_hash", None) or self._code_hash
        next_type = getattr(sent, "next_type", None)
        return CodeDelivery(
            kind=_kind_of(getattr(sent, "type", None)),
            next_kind=_kind_of(next_type) if next_type is not None else None,
            timeout=getattr(sent, "timeout", None),
        )

    async def send_code(self, phone: str) -> CodeDelivery:
        """Request a login code.

        Telegram prefers delivering the code to an already-logged-in Telegram app
        (SentCodeTypeApp) over SMS — callers should surface this to the user.
        """
        sent = await run_with_flood_wait_retry(
            lambda: self._client.send_code_request(phone), operation="send_code"
        )
        self._phone = phone
        delivery = self._bind(sent)
        # what Telegram promised (the phone number stays out of the log)
        logger.info(
            "send_code: code_type=%s next_type=%s timeout=%s",
            delivery.kind, delivery.next_kind, delivery.timeout,
        )
        return delivery

    async def resend_code(self) -> CodeDelivery:
        """Ask Telegram to resend the code via the next delivery channel."""
        if self._phone is None or self._code_hash is None:
            raise RuntimeError("send_code must be called before resend_code")
        sent = await run_with_flood_wait_retry(
            lambda: self._client(ResendCodeRequest(
                phone_number=self._phone, phone_code_hash=self._code_hash,
            )),
            operation="resend_code",
        )
        delivery = self._bind(sent)
        logger.info(
            "resend_code: code_type=%s next_type=%s timeout=%s",
            delivery.kind, delivery.next_kind, delivery.timeout,
        )
        return delivery

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


class LoginError(Exception):
    """A user-correctable login problem (wrong code / wrong 2FA password).

    Carries a short, human-readable message; raising it does NOT advance the
    LoginSession state machine, so the same step can simply be retried.
    """


class LoginSession:
    """State machine wrapping ``LoginFlow`` for the web/TUI login wizards.

    States: ``phone`` → ``code`` → (``password`` if 2FA) → ``done``. The whole
    flow runs over ONE connected client (``phone_code_hash`` binds to it, see
    LoginFlow) — steps must never be split across connections. Phone numbers and
    codes are never logged. User-correctable errors (bad code / bad password)
    raise :class:`LoginError` and leave the state untouched so the step can be
    retried; calling a step out of order raises ``RuntimeError``.
    """

    def __init__(self, client_or_flow):
        # accept either a raw Telethon client or a ready LoginFlow
        if isinstance(client_or_flow, LoginFlow):
            self._flow = client_or_flow
        else:
            self._flow = LoginFlow(client_or_flow)
        self._state = "phone"

    @property
    def state(self) -> str:
        return self._state

    async def submit_phone(self, phone: str) -> CodeDelivery:
        from telethon.errors import (
            PhoneNumberBannedError,
            PhoneNumberFloodError,
            PhoneNumberInvalidError,
        )

        try:
            delivery = await self._flow.send_code(phone)
        except PhoneNumberInvalidError as exc:
            raise LoginError("Invalid phone number — use the international format (+...).") from exc
        except PhoneNumberBannedError as exc:
            raise LoginError("This phone number is banned by Telegram.") from exc
        except PhoneNumberFloodError as exc:
            raise LoginError("Too many attempts for this number — try again later.") from exc
        self._state = "code"
        return delivery

    async def resend(self) -> CodeDelivery:
        if self._state not in ("code", "password"):
            raise RuntimeError("submit_phone must be called before resend")
        return await self._flow.resend_code()

    async def submit_code(self, code: str) -> None:
        from telethon.errors import (
            PhoneCodeEmptyError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )

        if self._state != "code":
            raise RuntimeError("submit_phone must be called before submit_code")
        try:
            await self._flow.sign_in(code=code)
        except SessionPasswordNeededError:
            self._state = "password"
            return
        except (PhoneCodeInvalidError, PhoneCodeEmptyError) as exc:
            raise LoginError("Wrong code — try again.") from exc
        except PhoneCodeExpiredError as exc:
            raise LoginError("Code expired — request a new one.") from exc
        self._state = "done"

    async def submit_password(self, password: str) -> None:
        from telethon.errors import PasswordHashInvalidError

        if self._state != "password":
            raise RuntimeError("submit_code must reach the 2FA step before submit_password")
        try:
            await self._flow.check_password(password)
        except PasswordHashInvalidError as exc:
            raise LoginError("Wrong 2FA password — try again.") from exc
        self._state = "done"
