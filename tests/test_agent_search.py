"""Цикл 11: build_search_fn — фабрика веб-поиска, lazy-import по провайдеру.

Реальные SDK подменяются фейковыми модулями в sys.modules — сети нет.
"""

import sys
import types

import pytest

from tg_messenger.agent.search import build_search_fn

# --- duckduckgo (без ключа) ---

def _fake_ddgs_module(calls):
    mod = types.ModuleType("ddgs")

    class DDGS:
        def text(self, query, max_results=5):
            calls.append((query, max_results))
            return [{"title": "Python", "href": "https://python.org", "body": "the language"}]

    mod.DDGS = DDGS
    return mod


async def test_duckduckgo_needs_no_key_and_formats_results(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "ddgs", _fake_ddgs_module(calls))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    fn = build_search_fn("duckduckgo")
    result = await fn("python language", max_results=3)
    assert calls == [("python language", 3)]
    assert "Python" in result and "https://python.org" in result and "the language" in result


# --- tavily ---

def _fake_tavily_module(calls):
    mod = types.ModuleType("tavily")

    class TavilyClient:
        def __init__(self, api_key):
            calls.append(("init", api_key))

        def search(self, query, max_results=5):
            calls.append(("search", query, max_results))
            return {"results": [{"title": "T", "url": "https://t.io", "content": "tavily text"}]}

    mod.TavilyClient = TavilyClient
    return mod


async def test_tavily_uses_key_from_env(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tavily", _fake_tavily_module(calls))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    fn = build_search_fn("tavily")
    result = await fn("q")
    assert ("init", "tvly-secret") in calls
    assert "tavily text" in result and "https://t.io" in result


def test_tavily_without_key_fails_fast(monkeypatch):
    monkeypatch.setitem(sys.modules, "tavily", _fake_tavily_module([]))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        build_search_fn("tavily")


def test_missing_sdk_gives_pip_hint(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    monkeypatch.setitem(sys.modules, "tavily", None)  # import tavily -> ImportError
    with pytest.raises(ValueError, match="pip install"):
        build_search_fn("tavily")


# --- serpdive ---

def _fake_serpdive_module(calls):
    mod = types.ModuleType("serpdive")

    class _Result:
        def __init__(self, title, url, content):
            self.title, self.url, self.content = title, url, content

    class _Response:
        results = [
            _Result("S", "https://s.io", "serpdive text"),
            _Result(None, "https://no-title.io", "titleless page"),
        ]

    class SerpDive:
        def __init__(self, api_key=None):
            calls.append(("init", api_key))

        def search(self, query, max_results=5):
            calls.append(("search", query, max_results))
            return _Response()

    mod.SerpDive = SerpDive
    return mod


async def test_serpdive_uses_key_from_env_and_falls_back_to_url_title(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "serpdive", _fake_serpdive_module(calls))
    monkeypatch.setenv("SERPDIVE_API_KEY", "sd_live_secret")
    fn = build_search_fn("serpdive")
    result = await fn("q", max_results=2)
    assert ("init", "sd_live_secret") in calls
    assert ("search", "q", 2) in calls
    assert "serpdive text" in result and "https://s.io" in result
    # результат без title показывает URL вместо пустой строки
    assert result.count("https://no-title.io") == 2


def test_serpdive_without_key_fails_fast(monkeypatch):
    monkeypatch.setitem(sys.modules, "serpdive", _fake_serpdive_module([]))
    monkeypatch.delenv("SERPDIVE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="SERPDIVE_API_KEY"):
        build_search_fn("serpdive")


# --- exa ---

def _fake_exa_module(calls):
    mod = types.ModuleType("exa_py")

    class _R:
        def __init__(self):
            self.title, self.url, self.text = "E", "https://e.io", "exa text"

    class Exa:
        def __init__(self, api_key):
            calls.append(("init", api_key))

        def search_and_contents(self, query, num_results=5, text=True):
            calls.append(("search", query, num_results))
            return types.SimpleNamespace(results=[_R()])

    mod.Exa = Exa
    return mod


async def test_exa_uses_key_from_env(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "exa_py", _fake_exa_module(calls))
    monkeypatch.setenv("EXA_API_KEY", "exa-secret")
    fn = build_search_fn("exa")
    result = await fn("q", max_results=2)
    assert ("init", "exa-secret") in calls
    assert ("search", "q", 2) in calls
    assert "exa text" in result


def test_exa_without_key_fails_fast(monkeypatch):
    monkeypatch.setitem(sys.modules, "exa_py", _fake_exa_module([]))
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="EXA_API_KEY"):
        build_search_fn("exa")


# --- brave ---

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def make_fake_async_client():
    """Одноразовый класс-фейк httpx.AsyncClient: свой список requests на каждый тест."""

    class _FakeAsyncClient:
        requests: list = []

        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            _FakeAsyncClient.requests.append({"url": url, "params": params, "headers": headers})
            return _FakeResponse(
                {"web": {"results": [{"title": "B", "url": "https://b.io",
                                      "description": "brave text"}]}}
            )

    return _FakeAsyncClient


async def test_brave_calls_rest_api_with_token(monkeypatch):
    import httpx

    fake_client_cls = make_fake_async_client()
    monkeypatch.setattr(httpx, "AsyncClient", fake_client_cls)
    monkeypatch.setenv("BRAVE_API_KEY", "brave-secret")
    fn = build_search_fn("brave")
    result = await fn("q", max_results=4)
    (req,) = fake_client_cls.requests
    assert "api.search.brave.com" in req["url"]
    assert req["params"]["q"] == "q" and req["params"]["count"] == 4
    assert req["headers"]["X-Subscription-Token"] == "brave-secret"
    assert "brave text" in result and "https://b.io" in result


def test_brave_without_key_fails_fast(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="BRAVE_API_KEY"):
        build_search_fn("brave")


# --- общее ---

def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown"):
        build_search_fn("yahoo")


def test_search_fn_is_a_named_documented_tool(monkeypatch):
    monkeypatch.setitem(sys.modules, "ddgs", _fake_ddgs_module([]))
    fn = build_search_fn("duckduckgo")
    # имя и docstring видны модели как схема инструмента
    assert fn.__name__ == "web_search"
    assert fn.__doc__ and fn.__doc__.strip()


async def test_duckduckgo_lazy_results_are_consumed_inside_worker_thread(monkeypatch):
    # ddgs может вернуть генератор — исчерпать его нужно в to_thread, не в event loop
    import threading

    seen_threads = []
    mod = types.ModuleType("ddgs")

    class DDGS:
        def text(self, query, max_results=5):
            def gen():
                seen_threads.append(threading.current_thread())
                yield {"title": "T", "href": "https://t", "body": "b"}

            return gen()

    mod.DDGS = DDGS
    monkeypatch.setitem(sys.modules, "ddgs", mod)
    fn = build_search_fn("duckduckgo")
    result = await fn("q")
    assert "https://t" in result
    assert seen_threads and all(t is not threading.main_thread() for t in seen_threads)
