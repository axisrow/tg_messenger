import stat

import pytest

from tg_messenger.core import auth
from tg_messenger.core.auth import SessionStore


def test_save_load_round_trip(session_dir):
    store = SessionStore(session_dir)
    store.save("default", "MY_SESSION_STRING")
    assert store.load("default") == "MY_SESSION_STRING"


def test_load_missing_returns_none(session_dir):
    store = SessionStore(session_dir)
    assert store.load("nope") is None


def test_saved_file_is_private(session_dir):
    store = SessionStore(session_dir)
    store.save("default", "S")
    path = store.path_for("default")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_name_is_sanitized(session_dir):
    store = SessionStore(session_dir)
    store.save("../../evil name", "S")
    # file must live inside session_dir, no traversal
    files = list(session_dir.iterdir())
    assert len(files) == 1
    assert files[0].parent == session_dir


def test_from_external_does_not_write(session_dir):
    store = SessionStore(session_dir)
    wrapped = store.from_external("EXTERNAL")
    assert wrapped == "EXTERNAL"
    assert list(session_dir.iterdir()) == []


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
