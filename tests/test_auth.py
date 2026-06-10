import stat

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from tg_messenger.core import auth
from tg_messenger.core.auth import SessionStore


def _make_session() -> str:
    s = StringSession()
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(b"\x00" * 256)
    return s.save()


VALID_SESSION = _make_session()


def test_save_load_round_trip(session_dir):
    store = SessionStore(session_dir)
    store.save("default", VALID_SESSION)
    assert store.load("default") == VALID_SESSION


def test_load_missing_returns_none(session_dir):
    store = SessionStore(session_dir)
    assert store.load("nope") is None


def test_saved_file_is_private(session_dir):
    store = SessionStore(session_dir)
    store.save("default", "S")
    path = store.path_for("default")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_session_dir_is_private(session_dir):
    nested = session_dir / "nested"
    store = SessionStore(nested)
    store.save("default", "S")
    mode = stat.S_IMODE(nested.stat().st_mode)
    assert mode == 0o700


def test_name_is_sanitized(session_dir):
    store = SessionStore(session_dir)
    store.save("../../evil name", "S")
    # file must live inside session_dir, no traversal
    files = list(session_dir.iterdir())
    assert len(files) == 1
    assert files[0].parent == session_dir


def test_from_external_does_not_write(session_dir):
    store = SessionStore(session_dir)
    wrapped = store.from_external(VALID_SESSION)
    assert wrapped == VALID_SESSION
    assert list(session_dir.iterdir()) == []


def test_from_external_rejects_garbage(session_dir):
    store = SessionStore(session_dir)
    with pytest.raises(ValueError):
        store.from_external("not-a-real-session")


def test_load_rejects_corrupt_session(session_dir):
    store = SessionStore(session_dir)
    store.path_for("default").write_text("garbage", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load("default")


async def test_send_code_reports_delivery_channel(fake_client):
    flow = auth.LoginFlow(fake_client)
    delivery = await flow.send_code("+10000000000")
    assert delivery.kind == "app"  # fake SentCode.type is SentCodeTypeApp


async def test_resend_code_switches_channel_and_rebinds_hash(fake_client):
    flow = auth.LoginFlow(fake_client)
    await flow.send_code("+10000000000")
    delivery = await flow.resend_code()
    assert delivery.kind == "sms"  # next channel after the in-app code
    assert len(fake_client.resend_requests) == 1
    # the new phone_code_hash must be bound for the subsequent sign_in
    assert flow._code_hash == "hash456"


async def test_send_code_logs_delivery_without_phone(fake_client, caplog):
    flow = auth.LoginFlow(fake_client)
    with caplog.at_level("INFO", logger="tg_messenger.core.auth"):
        await flow.send_code("+10000000000")
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("send_code" in m and "code_type=app" in m for m in infos)
    assert "+10000000000" not in caplog.text  # phone numbers stay out of the log


async def test_resend_code_logs_delivery(fake_client, caplog):
    flow = auth.LoginFlow(fake_client)
    await flow.send_code("+10000000000")
    with caplog.at_level("INFO", logger="tg_messenger.core.auth"):
        await flow.resend_code()
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("resend_code" in m and "code_type=sms" in m for m in infos)


async def test_resend_before_send_code_raises(fake_client):
    flow = auth.LoginFlow(fake_client)
    with pytest.raises(RuntimeError):
        await flow.resend_code()


async def test_send_code_and_sign_in(fake_client):
    flow = auth.LoginFlow(fake_client)
    await flow.send_code("+10000000000")
    assert fake_client.code_requests == ["+10000000000"]
    user = await flow.sign_in(code="12345")
    assert fake_client.signed_in_with[-1]["code"] == "12345"
    assert user.id == 1


async def test_check_password_2fa(fake_client):
    flow = auth.LoginFlow(fake_client)
    await flow.send_code("+10000000000")
    await flow.check_password("hunter2")
    assert fake_client.signed_in_with[-1]["password"] == "hunter2"


async def test_sign_in_before_code_raises(fake_client):
    flow = auth.LoginFlow(fake_client)
    with pytest.raises(RuntimeError):
        await flow.sign_in(code="12345")


# --- циклы 53–54: опциональное шифрование SessionStore (Fernet enc:v2:) ---

pytest.importorskip("cryptography")

_ENC_KEY = "shared-sso-secret"


def test_save_encrypts_with_key(session_dir):
    store = SessionStore(session_dir, encryption_key=_ENC_KEY)
    store.save("default", VALID_SESSION)
    raw = store.path_for("default").read_text(encoding="utf-8").strip()
    assert raw.startswith("enc:v2:")
    # the plaintext session must NOT appear anywhere in the file
    assert VALID_SESSION not in raw
    # and load round-trips it back
    assert store.load("default") == VALID_SESSION


def test_load_factory_written_session_sso(session_dir):
    # simulate a session encrypted by tg_content_factory with the same shared key
    from tg_messenger.core.session_cipher import encrypt_session

    path = SessionStore(session_dir).path_for("default")
    session_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(encrypt_session(VALID_SESSION, _ENC_KEY), encoding="utf-8")
    store = SessionStore(session_dir, encryption_key=_ENC_KEY)
    assert store.load("default") == VALID_SESSION


def test_lazy_migration_plaintext_to_encrypted(session_dir):
    import stat as _stat

    plain = SessionStore(session_dir)
    plain.save("default", VALID_SESSION)  # writes plaintext
    # now load with a key — should rewrite the file encrypted, preserving 0600
    keyed = SessionStore(session_dir, encryption_key=_ENC_KEY)
    assert keyed.load("default") == VALID_SESSION
    raw = keyed.path_for("default").read_text(encoding="utf-8").strip()
    assert raw.startswith("enc:v2:")
    mode = _stat.S_IMODE(keyed.path_for("default").stat().st_mode)
    assert mode == 0o600


def test_encrypted_file_without_key_errors_with_hint(session_dir):
    keyed = SessionStore(session_dir, encryption_key=_ENC_KEY)
    keyed.save("default", VALID_SESSION)
    # drop the key — the encrypted file can't be read, with a SESSION_ENCRYPTION_KEY hint
    no_key = SessionStore(session_dir)
    with pytest.raises(ValueError, match="SESSION_ENCRYPTION_KEY"):
        no_key.load("default")
