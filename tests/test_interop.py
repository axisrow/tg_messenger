"""Циклы 105-109: interop с tg_content_factory.

httpx живёт ТОЛЬКО в interop/ — тесты мокают HTTP через httpx.MockTransport
(respx не установлен). Ретраи бьются по инжектируемому no-op sleep — без
реального ожидания. Сети нет.
"""

from __future__ import annotations

import httpx
import pytest

from tg_messenger.interop.factory_client import FactoryClient, FactoryError


async def _noop_sleep(_seconds: float) -> None:
    return None


def make_client(handler, *, base_url="http://factory.local", password="secret"):
    """FactoryClient на инжектированном MockTransport (без сети) + no-op sleep."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url=base_url)
    return FactoryClient(base_url=base_url, password=password, http=http, sleep=_noop_sleep)


# --- цикл 105: search_messages ---


async def test_search_messages_builds_request_and_returns_list():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[{"id": 1, "text": "hi"}])

    async with make_client(handler) as fc:
        result = await fc.search_messages("@chan", "привет", limit=10)

    assert result == [{"id": 1, "text": "hi"}]
    assert seen["path"] == "/search/messages/@chan"
    assert seen["query"]["query"] == "привет"
    assert seen["query"]["limit"] == "10"
    # Basic auth: пустой username, пароль в пароле
    assert seen["auth"] and seen["auth"].startswith("Basic ")


async def test_search_messages_passes_optional_filters():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    async with make_client(handler) as fc:
        await fc.search_messages(
            "123", "q", limit=5, date_from="2024-01-01", date_to="2024-02-01", topic_id=7
        )

    assert seen["query"]["date_from"] == "2024-01-01"
    assert seen["query"]["date_to"] == "2024-02-01"
    assert seen["query"]["topic_id"] == "7"


async def test_search_messages_401_raises_clear_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad password"})

    async with make_client(handler) as fc:
        with pytest.raises(FactoryError) as exc:
            await fc.search_messages("@chan", "q")
    assert "auth" in str(exc.value).lower() or "password" in str(exc.value).lower()


async def test_search_messages_retries_transient_network_errors():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=[{"id": 1}])

    async with make_client(handler) as fc:
        result = await fc.search_messages("@chan", "q")

    assert result == [{"id": 1}]
    assert attempts["n"] == 3  # дважды упало, на третий успех


async def test_search_messages_gives_up_after_retries():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("down", request=request)

    async with make_client(handler) as fc:
        with pytest.raises(FactoryError):
            await fc.search_messages("@chan", "q")
    assert attempts["n"] == 3  # 3 попытки и сдаёмся


async def test_search_messages_4xx_does_not_retry():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"detail": "bad request"})

    async with make_client(handler) as fc:
        with pytest.raises(FactoryError):
            await fc.search_messages("@chan", "q")
    assert attempts["n"] == 1  # 4xx (не сеть) — сразу наверх, без ретраев


# --- цикл 106: task-методы + InteropTask ---


async def test_create_task_returns_task_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        import json as _json

        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"id": "task-1"})

    async with make_client(handler) as fc:
        task_id = await fc.create_task("dm_reply", {"peer": 7, "text": "hi"})

    assert task_id == "task-1"
    assert seen["method"] == "POST"
    assert seen["path"] == "/tasks"
    assert seen["body"]["type"] == "dm_reply"
    assert seen["body"]["payload"]["v"] == 1  # версионирование payload
    assert seen["body"]["payload"]["peer"] == 7


async def test_get_task_returns_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tasks/task-1"
        return httpx.Response(200, json={
            "id": "task-1", "type": "dm_reply", "payload": {"v": 1, "peer": 7},
            "status": "pending", "result_payload": None,
        })

    async with make_client(handler) as fc:
        task = await fc.get_task("task-1")
    assert task["id"] == "task-1"
    assert task["status"] == "pending"


async def test_claim_next_returns_task():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={
            "id": "t9", "type": "dm_reply", "payload": {"v": 1, "peer": 1, "text": "x"},
            "status": "claimed", "result_payload": None,
        })

    async with make_client(handler) as fc:
        task = await fc.claim_next(["dm_reply", "chat_answer"])
    assert task is not None
    assert task["id"] == "t9"
    assert "dm_reply" in seen["query"]["types"]


async def test_claim_next_empty_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with make_client(handler) as fc:
        task = await fc.claim_next(["dm_reply"])
    assert task is None


async def test_complete_task_posts_result():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["path"] = request.url.path
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler) as fc:
        await fc.complete_task("t1", {"sent": 999})
    assert seen["path"] == "/tasks/t1/complete"
    assert seen["body"]["result_payload"]["sent"] == 999


async def test_fail_task_posts_error():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["path"] = request.url.path
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler) as fc:
        await fc.fail_task("t1", "kaboom")
    assert seen["path"] == "/tasks/t1/fail"
    assert seen["body"]["error"] == "kaboom"


def test_interop_task_model_validates():
    from tg_messenger.interop.factory_client import InteropTask

    task = InteropTask.model_validate({
        "id": "t1", "type": "dm_reply", "payload": {"v": 1, "peer": 7, "text": "hi"},
        "status": "pending", "result_payload": None,
    })
    assert task.id == "t1"
    assert task.payload["peer"] == 7
    assert task.result_payload is None


# --- цикл 107: Worker — poll/claim/execute/complete|fail ---


class StubFactory:
    """In-memory FactoryClient stand-in for worker tests (no httpx)."""

    def __init__(self):
        self._queue: list[dict] = []
        self.completed: list[tuple[str, dict]] = []
        self.failed: list[tuple[str, str]] = []

    def enqueue(self, task: dict) -> None:
        self._queue.append(task)

    async def claim_next(self, types):
        for i, task in enumerate(self._queue):
            if task["type"] in types:
                return self._queue.pop(i)
        return None

    async def complete_task(self, task_id, result_payload):
        self.completed.append((task_id, result_payload))

    async def fail_task(self, task_id, error):
        self.failed.append((task_id, error))


class StubCoreClient:
    """Just enough of StandaloneTelegramClient for worker executors."""

    def __init__(self, *, send_raises=False):
        self.sent: list[tuple[int, str]] = []
        self.send_raises = send_raises
        self.history_items: list = []
        self.dialog_items: list = []

    async def send_text(self, peer, text):
        if self.send_raises:
            raise RuntimeError("send blew up")
        self.sent.append((peer, text))

        class _Msg:
            id = 12345

        return _Msg()

    async def history(self, peer, limit=50, offset_id=0):
        return self.history_items

    async def dialogs(self, dm_only=True):
        return self.dialog_items


def _make_worker(factory, client, **kw):
    from tg_messenger.interop.worker import Worker

    return Worker(client, factory, types=["dm_reply", "chat_answer"], sleep=_noop_sleep, **kw)


async def test_worker_dm_reply_sends_and_completes():
    factory = StubFactory()
    client = StubCoreClient()
    factory.enqueue({
        "id": "t1", "type": "dm_reply",
        "payload": {"v": 1, "peer": 7, "text": "hello"},
        "status": "claimed", "result_payload": None,
    })
    worker = _make_worker(factory, client)
    handled = await worker.process_once()

    assert handled is True
    assert client.sent == [(7, "hello")]
    assert factory.completed == [("t1", {"sent": 12345})]
    assert factory.failed == []


async def test_worker_no_task_returns_false():
    factory = StubFactory()
    worker = _make_worker(factory, StubCoreClient())
    assert await worker.process_once() is False


async def test_worker_send_failure_fails_task_and_loop_survives(caplog):
    import logging as _logging

    factory = StubFactory()
    client = StubCoreClient(send_raises=True)
    factory.enqueue({
        "id": "bad", "type": "dm_reply",
        "payload": {"v": 1, "peer": 7, "text": "x"}, "status": "claimed",
        "result_payload": None,
    })
    factory.enqueue({  # the NEXT task must still be processable
        "id": "good", "type": "chat_answer",
        "payload": {"v": 1, "peer": 8, "text": "y"}, "status": "claimed",
        "result_payload": None,
    })
    worker = _make_worker(factory, StubCoreClient())  # placeholder, replaced below
    worker._client = client  # first run hits the failing client

    with caplog.at_level(_logging.ERROR, logger="tg_messenger.interop.worker"):
        await worker.process_once()
    assert factory.failed and factory.failed[0][0] == "bad"
    assert "send blew up" in factory.failed[0][1]
    assert any(r.levelno >= _logging.ERROR for r in caplog.records)  # logged, not swallowed

    # swap in a healthy client: the next task processes fine
    worker._client = StubCoreClient()
    await worker.process_once()
    assert worker._client.sent == [(8, "y")]


# --- цикл 108: fetch_history / fetch_dialogs ---


async def test_worker_fetch_history_serializes_messages():
    from datetime import datetime, timezone

    from tg_messenger.core.models import Message

    factory = StubFactory()
    client = StubCoreClient()
    client.history_items = [
        Message(id=1, dialog_id=7, sender_id=7, out=False, text="hi",
                date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
    ]
    factory.enqueue({
        "id": "h1", "type": "fetch_history",
        "payload": {"v": 1, "peer": 7, "limit": 20}, "status": "claimed",
        "result_payload": None,
    })
    worker = _make_worker(factory, client, types=None) if False else _make_worker(factory, client)
    worker._types = ["fetch_history"]
    await worker.process_once()

    assert factory.completed
    task_id, result = factory.completed[0]
    assert task_id == "h1"
    assert isinstance(result["messages"], list)
    assert result["messages"][0]["text"] == "hi"  # Pydantic -> dict


async def test_worker_fetch_dialogs_serializes_dialogs():
    from tg_messenger.core.models import Dialog

    factory = StubFactory()
    client = StubCoreClient()
    client.dialog_items = [Dialog(id=7, title="Ann", username="ann", unread=2)]
    factory.enqueue({
        "id": "d1", "type": "fetch_dialogs",
        "payload": {"v": 1}, "status": "claimed", "result_payload": None,
    })
    worker = _make_worker(factory, client)
    worker._types = ["fetch_dialogs"]
    await worker.process_once()

    task_id, result = factory.completed[0]
    assert task_id == "d1"
    assert result["dialogs"][0]["title"] == "Ann"


# --- цикл 109: prompt-задачи через agent ---


async def test_worker_prompt_task_without_agent_fails_clearly():
    factory = StubFactory()
    client = StubCoreClient()
    factory.enqueue({
        "id": "p1", "type": "dm_reply",
        "payload": {"v": 1, "peer": 7, "prompt": "say hi"}, "status": "claimed",
        "result_payload": None,
    })
    worker = _make_worker(factory, client)  # no agent injected
    await worker.process_once()

    assert client.sent == []  # nothing sent
    assert factory.failed and factory.failed[0][0] == "p1"
    assert "agent" in factory.failed[0][1].lower()


async def test_worker_prompt_task_with_agent_replies():
    pytest.importorskip("langgraph")  # agent path needs the LLM stack at most defensively

    class StubAgent:
        async def handle(self, dialog_id, text):
            return f"answer to: {text}"

    factory = StubFactory()
    client = StubCoreClient()
    factory.enqueue({
        "id": "p2", "type": "dm_reply",
        "payload": {"v": 1, "peer": 7, "prompt": "say hi"}, "status": "claimed",
        "result_payload": None,
    })
    worker = _make_worker(factory, client, agent=StubAgent())
    await worker.process_once()

    assert client.sent == [(7, "answer to: say hi")]
    assert factory.completed and factory.completed[0][0] == "p2"
