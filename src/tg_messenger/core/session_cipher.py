"""Optional at-rest encryption for StringSession strings — byte-compatible with tg_content_factory.

`enc:v2:<token>` is Fernet over a PBKDF2-HMAC-SHA256(secret, salt, 200k iters, 32 bytes)
key, urlsafe-b64-encoded — exactly the factory's `src/security/session_cipher.py` scheme.
A shared `SESSION_ENCRYPTION_KEY` therefore makes the two projects' encrypted sessions
interchangeable (SSO). `enc:v1:` (Fernet over the raw secret, no PBKDF2) is read-only legacy.
Plaintext (no `enc:` prefix) passes through unchanged, so encryption is opt-in.

`cryptography` is an optional dependency (`[crypto]` extra): imported lazily so the module
is only needed when a key is actually configured.
"""

from __future__ import annotations

import base64
import hashlib

_V2_PREFIX = "enc:v2:"
_V1_PREFIX = "enc:v1:"

# factory-compatible KDF constants — do NOT change without bumping the format version
_SALT = b"tg_session_key_v2"
_ITERATIONS = 200_000
_DKLEN = 32

_CRYPTO_HINT = "session encryption requires: pip install 'tg-messenger[crypto]'"


def _require_fernet():
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:  # extra not installed but a key was supplied
        raise ValueError(_CRYPTO_HINT) from exc
    return Fernet, InvalidToken  # classes, returned for the callers to use


def _derive_key_v2(secret: str) -> bytes:
    raw = hashlib.pbkdf2_hmac("sha256", secret.encode(), _SALT, _ITERATIONS, _DKLEN)
    return base64.urlsafe_b64encode(raw)


def _derive_key_v1(secret: str) -> bytes:
    # legacy: raw secret padded/truncated to 32 bytes, then urlsafe-b64 (read-only)
    return base64.urlsafe_b64encode(secret.encode().ljust(32, b"\0")[:32])


def is_encrypted(stored: str) -> bool:
    """True if the stored value carries an `enc:v1:`/`enc:v2:` envelope."""
    return stored.startswith((_V1_PREFIX, _V2_PREFIX))


def encrypt_session(plaintext: str, secret: str) -> str:
    """Wrap `plaintext` as `enc:v2:<token>` using the factory-compatible key derivation."""
    Fernet, _ = _require_fernet()  # noqa: N806 — Fernet is a class
    token = Fernet(_derive_key_v2(secret)).encrypt(plaintext.encode()).decode()
    return _V2_PREFIX + token


def decrypt_session(stored: str, secret: str) -> str:
    """Decrypt an `enc:v2:`/`enc:v1:` value; plaintext passes through unchanged.

    A wrong key (or corrupt token) raises ValueError with a readable message rather
    than leaking a Fernet traceback.
    """
    if not is_encrypted(stored):
        return stored  # plaintext / legacy unencrypted — opt-in encryption
    Fernet, InvalidToken = _require_fernet()  # noqa: N806 — both are classes
    if stored.startswith(_V2_PREFIX):
        key, body = _derive_key_v2(secret), stored[len(_V2_PREFIX):]
    else:
        key, body = _derive_key_v1(secret), stored[len(_V1_PREFIX):]
    try:
        return Fernet(key).decrypt(body.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("could not decrypt session: wrong SESSION_ENCRYPTION_KEY") from exc
