"""Session cipher — Fernet over a PBKDF2-derived key, byte-compatible with tg_content_factory.

The factory's `src/security/session_cipher.py` derives the key as
PBKDF2-HMAC-SHA256(secret, salt=b"tg_session_key_v2", iterations=200_000, dklen=32),
urlsafe_b64encode'd, and wraps the StringSession as `enc:v2:<fernet-token>`. We accept
that scheme verbatim so a shared SESSION_ENCRYPTION_KEY makes the two projects' encrypted
strings interchangeable (SSO). The compatibility test re-derives the key with the same
parameters hardcoded INDEPENDENTLY — if either side drifts the constants, it fails.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

pytest.importorskip("cryptography")

from tg_messenger.core.session_cipher import (  # noqa: E402
    decrypt_session,
    encrypt_session,
    is_encrypted,
)

# the StringSession payload is opaque to the cipher — any text round-trips
_PLAINTEXT = "1BVtsOHIBu1A2b3c4d5e6f7g8h9i0jKlMnOpQrStUvWxYz=="
_SECRET = "shared-sso-secret-key"

# factory constants, hardcoded here so a drift breaks this test (cycle 51)
_FACTORY_SALT = b"tg_session_key_v2"
_FACTORY_ITERATIONS = 200_000
_FACTORY_DKLEN = 32


def _factory_fernet(secret: str):
    """Re-derive the factory's Fernet key independently of our implementation."""
    from cryptography.fernet import Fernet

    raw = hashlib.pbkdf2_hmac("sha256", secret.encode(), _FACTORY_SALT, _FACTORY_ITERATIONS, _FACTORY_DKLEN)
    return Fernet(base64.urlsafe_b64encode(raw))


def test_encrypt_decrypt_roundtrip():
    token = encrypt_session(_PLAINTEXT, _SECRET)
    assert token.startswith("enc:v2:")
    assert decrypt_session(token, _SECRET) == _PLAINTEXT


def test_is_encrypted():
    assert is_encrypted("enc:v2:whatever")
    assert is_encrypted("enc:v1:legacy")
    assert not is_encrypted(_PLAINTEXT)


def test_factory_compatibility_we_decrypt_theirs():
    # a string the factory would have produced — we must read it with the same secret
    their_token = "enc:v2:" + _factory_fernet(_SECRET).encrypt(_PLAINTEXT.encode()).decode()
    assert decrypt_session(their_token, _SECRET) == _PLAINTEXT


def test_factory_compatibility_they_decrypt_ours():
    # our token must be readable by the factory's independent key derivation
    ours = encrypt_session(_PLAINTEXT, _SECRET)
    body = ours[len("enc:v2:"):]
    assert _factory_fernet(_SECRET).decrypt(body.encode()).decode() == _PLAINTEXT


def test_plaintext_passthrough_on_decrypt():
    # an unencrypted (legacy plaintext) value decrypts to itself
    assert decrypt_session(_PLAINTEXT, _SECRET) == _PLAINTEXT


def test_wrong_key_raises_valueerror():
    token = encrypt_session(_PLAINTEXT, _SECRET)
    with pytest.raises(ValueError):
        decrypt_session(token, "the-wrong-secret")


def test_v1_legacy_is_readable():
    # enc:v1: = Fernet over the raw secret (no PBKDF2) — read-only support
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(_SECRET.encode().ljust(32, b"\0")[:32])
    legacy = "enc:v1:" + Fernet(key).encrypt(_PLAINTEXT.encode()).decode()
    assert decrypt_session(legacy, _SECRET) == _PLAINTEXT


# --- цикл 52: понятная деградация без cryptography ---

def test_encrypt_without_cryptography_hints_extra(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_crypto(name, *args, **kwargs):
        if name == "cryptography.fernet" or name == "cryptography":
            raise ImportError("No module named 'cryptography'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_crypto)
    with pytest.raises(ValueError, match=r"tg-messenger\[crypto\]"):
        encrypt_session(_PLAINTEXT, _SECRET)
