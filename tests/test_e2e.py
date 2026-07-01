"""E2E: запуск реального приложения подпроцессом (без фейков).

Креды берутся из окружения или из .env в корне проекта (приложение само
.env НЕ читает — тут он парсится только чтобы передать значения подпроцессу).
Тесты, требующие TG_API_ID/TG_API_HASH, скипаются, если кредов нет.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tg_messenger.cli.main import _parse_dotenv
from tg_messenger.core.auth import default_session_dir

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# resolve the session path the same way the app does (tg_home(): ~/.tg, or the
# legacy ~/.tg_messenger fallback) rather than hardcoding a root
SESSION_FILE = default_session_dir() / "default.session"

DOTENV = _parse_dotenv(PROJECT_ROOT / ".env")


def _cred_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("TG_API_ID", "TG_API_HASH"):
        if not env.get(key) and DOTENV.get(key):
            env[key] = DOTENV[key]
    return env


HAS_CREDS = bool(_cred_env().get("TG_API_ID") and _cred_env().get("TG_API_HASH"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int, env: dict[str, str], cwd: Path = PROJECT_ROOT) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "tg_messenger.cli.main", "serve", "--port", str(port)],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_http(port: int, proc: subprocess.Popen, path: str = "/", deadline_sec: float = 15.0):
    """Poll the server until it responds (or the process dies)."""
    deadline = time.monotonic() + deadline_sec
    last_exc = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, _ = proc.communicate(timeout=5)
            pytest.fail(f"server exited early (code {proc.returncode}):\n{out[-2000:]}")
        try:
            return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=2)
        except httpx.TransportError as exc:
            last_exc = exc
            time.sleep(0.2)
    pytest.fail(f"server did not come up in {deadline_sec}s: {last_exc}")


def _stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate(timeout=5)
    return out or ""


def test_serve_without_creds_fails_with_clear_error(tmp_path):
    """Без TG_API_ID/TG_API_HASH (и без .env в cwd) сервер падает с внятной ошибкой."""
    env = {k: v for k, v in os.environ.items() if k not in ("TG_API_ID", "TG_API_HASH")}
    proc = _serve(_free_port(), env, cwd=tmp_path)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("server without creds should exit, but kept running")
    assert proc.returncode != 0
    assert "API ID or Hash cannot be empty" in out


@pytest.mark.skipif(not HAS_CREDS, reason="нет TG_API_ID/TG_API_HASH (окружение или .env)")
def test_serve_starts_and_serves_index():
    """Пользовательский сценарий: креды только в .env, приложение само его читает."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("TG_API_ID", "TG_API_HASH")}
    proc = _serve(port, env)  # cwd = корень проекта, где лежит .env
    try:
        r = _wait_http(port, proc)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "tg_messenger" in r.text
    finally:
        out = _stop(proc)
    assert "Traceback" not in out, f"server log has errors:\n{out[-2000:]}"


@pytest.mark.skipif(not HAS_CREDS, reason="нет TG_API_ID/TG_API_HASH (окружение или .env)")
@pytest.mark.skipif(SESSION_FILE.exists(), reason="сессия есть — этот тест про её отсутствие")
def test_serve_dialogs_without_session_gives_401_hint():
    """Без логина /dialogs отвечает 401 с подсказкой, а не 500 с traceback."""
    port = _free_port()
    proc = _serve(port, _cred_env())
    try:
        _wait_http(port, proc)  # дождаться старта по индексу
        r = httpx.get(f"http://127.0.0.1:{port}/dialogs", timeout=15)
        assert r.status_code == 401
        assert "tg-messenger login" in r.text
    finally:
        _stop(proc)


@pytest.mark.skipif(not HAS_CREDS, reason="нет TG_API_ID/TG_API_HASH (окружение или .env)")
@pytest.mark.skipif(not SESSION_FILE.exists(),
                    reason="нет сессии — выполните: tg-messenger login")
def test_serve_dialogs_with_real_session():
    """С залогиненной сессией /dialogs отдаёт список без 500-х."""
    port = _free_port()
    proc = _serve(port, _cred_env())
    try:
        r = _wait_http(port, proc, path="/dialogs", deadline_sec=25)
        assert r.status_code == 200
    finally:
        _stop(proc)
