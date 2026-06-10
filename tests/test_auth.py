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
