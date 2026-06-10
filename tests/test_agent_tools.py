"""Цикл 10: make_telegram_tools — async-инструменты deep-агента поверх core-клиента."""

import inspect
from datetime import datetime, timezone

import pytest

from tg_messenger.agent.tools import make_telegram_tools
from tg_messenger.core.models import Dialog, MediaRef, Message


class StubClient:
    def __init__(self):
        self.sent = []
        self.history_items = []
        self.dialog_items = []

    async def send_text(self, peer, text):
        self.sent.append((peer, text))
        return Message(id=42, dialog_id=peer, sender_id=1, out=True, text=text,
                       date=datetime(2024, 1, 1, tzinfo=timezone.utc))

    async def history(self, peer, limit=50, offset_id=0):
        return self.history_items[:limit]

    async def dialogs(self, dm_only=True):
        assert dm_only is True  # инструменты работают только с личками
        return self.dialog_items


@pytest.fixture
def client():
    return StubClient()


@pytest.fixture
def tools(client):
    return {fn.__name__: fn for fn in make_telegram_tools(client)}


def test_returns_three_async_tools_with_docstrings(tools):
    assert set(tools) == {"send_telegram_message", "read_telegram_history", "list_telegram_dialogs"}
    for fn in tools.values():
        # docstring — это промпт инструмента, аннотации — его схема
        assert inspect.iscoroutinefunction(fn)
        assert fn.__doc__ and fn.__doc__.strip()
        assert fn.__annotations__


async def test_send_calls_client_and_confirms_with_id(client, tools):
    result = await tools["send_telegram_message"](peer_id=7, text="hello")
    assert client.sent == [(7, "hello")]
    assert "42" in result  # id отправленного сообщения — пруф для модели


async def test_history_is_formatted_for_the_model(client, tools):
    client.history_items = [
        Message(id=1, dialog_id=7, sender_id=7, out=False, text="hi",
                date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        Message(id=2, dialog_id=7, sender_id=1, out=True, text=None,
                media=MediaRef(kind="photo"),
                date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
    ]
    result = await tools["read_telegram_history"](peer_id=7, limit=10)
    assert "← [1] hi" in result
    assert "→ [2] <media>" in result


async def test_empty_history_says_so(client, tools):
    result = await tools["read_telegram_history"](peer_id=7)
    assert result.strip()  # не пустая строка — модель должна понять, что сообщений нет
    assert "no messages" in result.lower()


async def test_dialogs_listed_with_id_title_username(client, tools):
    client.dialog_items = [
        Dialog(id=7, title="Ann", username="ann", unread=2),
        Dialog(id=8, title="Bob"),
    ]
    result = await tools["list_telegram_dialogs"]()
    assert "7" in result and "Ann" in result and "@ann" in result
    assert "8" in result and "Bob" in result


async def test_empty_dialogs_says_so(client, tools):
    result = await tools["list_telegram_dialogs"]()
    assert "no dialogs" in result.lower()


# --- Цикл 110: factory-инструменты (interop) регистрируются только при TG_FACTORY_URL ---


class FakeFactory:
    """Stand-in FactoryClient — records calls, no httpx."""

    def __init__(self, base_url=None, password=None, **kw):
        self.base_url = base_url
        self.password = password
        self.searches = []
        self.created = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def search_messages(self, identifier, query, limit=50, **kw):
        self.searches.append((identifier, query, limit, kw))
        return [{"id": 1, "text": f"{query}!"}]

    async def create_task(self, type, payload):
        self.created.append((type, payload))
        return "task-xyz"


def test_factory_tools_absent_without_url(client):
    tools = {fn.__name__: fn for fn in make_telegram_tools(client, factory_url=None)}
    assert "factory_search" not in tools
    assert "factory_create_task" not in tools


def test_factory_tools_present_with_url(client):
    tools = {fn.__name__: fn for fn in make_telegram_tools(
        client, factory_url="http://factory.local", factory_password="pw")}
    assert "factory_search" in tools
    assert "factory_create_task" in tools


async def test_factory_search_forwards_params(client, monkeypatch):
    import tg_messenger.agent.tools as tools_mod

    fakes = []

    def _factory(base_url, password, **kw):
        f = FakeFactory(base_url=base_url, password=password)
        fakes.append(f)
        return f

    monkeypatch.setattr(tools_mod, "_make_factory_client", _factory)
    tools = {fn.__name__: fn for fn in make_telegram_tools(
        client, factory_url="http://factory.local", factory_password="pw")}
    result = await tools["factory_search"](query="спб", identifier="@chan", limit=5)
    assert "спб" in result
    (f,) = fakes
    assert f.base_url == "http://factory.local"
    assert f.searches[0][0] == "@chan"
    assert f.searches[0][1] == "спб"
    assert f.searches[0][2] == 5


async def test_factory_create_task_forwards(client, monkeypatch):
    import tg_messenger.agent.tools as tools_mod

    fakes = []
    monkeypatch.setattr(tools_mod, "_make_factory_client",
                        lambda base_url, password, **kw: fakes.append(FakeFactory(base_url=base_url)) or fakes[-1])
    tools = {fn.__name__: fn for fn in make_telegram_tools(
        client, factory_url="http://f", factory_password="pw")}
    result = await tools["factory_create_task"](type="dm_reply", payload={"peer": 7, "text": "hi"})
    assert "task-xyz" in result
    (f,) = fakes
    assert f.created[0][0] == "dm_reply"
