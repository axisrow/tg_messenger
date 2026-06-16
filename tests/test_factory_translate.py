"""Structured-output translation in agent/factory.py: probe, runtime fallback, kv method cache.

The LLM stack is faked — models expose ``with_structured_output(schema, method=...)`` returning a
stub whose ``ainvoke`` yields a TranslationBatch / dict / raises, mirroring how json_schema and
json_mode behave on real (and z.ai-style) endpoints. No network, no real LLM.
"""

from __future__ import annotations

import pytest

# the factory imports deepagents/langchain at module load — skip the whole file without the extra
pytest.importorskip("deepagents")
pytest.importorskip("langchain")

from tg_messenger.agent import factory as f  # noqa: E402
from tg_messenger.agent.factory import TranslationBatch, TranslationItem  # noqa: E402
from tg_messenger.agent.translate import get_cached_method  # noqa: E402
from tg_messenger.core.message_store import register_message_store_migrations  # noqa: E402
from tg_messenger.core.storage import Storage  # noqa: E402


class _Structured:
    """A with_structured_output result: ainvoke returns a fixed value or raises it."""

    def __init__(self, result, counter, tag):
        self._result = result
        self._counter = counter
        self._tag = tag

    async def ainvoke(self, messages):
        self._counter[self._tag] = self._counter.get(self._tag, 0) + 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeModel:
    """A model whose json_schema / json_mode structured outputs are configured independently."""

    def __init__(self, *, schema_result, json_result, counter=None, raise_on_schema_build=False):
        self._schema_result = schema_result
        self._json_result = json_result
        self._counter = counter if counter is not None else {}
        self._raise_on_schema_build = raise_on_schema_build

    def with_structured_output(self, schema, method):
        if method == "json_schema" and self._raise_on_schema_build:
            raise RuntimeError("provider rejects json_schema")
        result = self._schema_result if method == "json_schema" else self._json_result
        return _Structured(result, self._counter, method)


_GOOD = TranslationBatch(items=[TranslationItem(id=1, translation="привет")])
_EMPTY = TranslationBatch(items=[])


async def test_probe_returns_json_schema_when_supported():
    model = FakeModel(schema_result=_GOOD, json_result=_GOOD)
    assert await f.probe_structured_method(model) == "json_schema"


async def test_probe_falls_back_when_schema_empty_or_raises():
    # z.ai-style: HTTP 200 but ignores the schema → empty/garbage, no exception
    assert await f.probe_structured_method(FakeModel(schema_result=_EMPTY, json_result=_GOOD)) == "json_mode"
    # provider raises on the schema call
    err = FakeModel(schema_result=RuntimeError("ignored"), json_result=_GOOD)
    assert await f.probe_structured_method(err) == "json_mode"
    # provider refuses to even build a json_schema structured model
    refuse = FakeModel(schema_result=_GOOD, json_result=_GOOD, raise_on_schema_build=True)
    assert await f.probe_structured_method(refuse) == "json_mode"


async def test_translate_fn_json_schema_path():
    model = FakeModel(schema_result=_GOOD, json_result=_GOOD)
    fn = f.make_translate_fn(model, "json_schema")
    assert await fn([(1, "hola")], "ru") == {1: "привет"}


async def test_translate_fn_runtime_fallback_to_json_mode():
    # json_schema returns empty at runtime (stale probe) → retry json_mode as a safety net
    counter: dict[str, int] = {}
    model = FakeModel(schema_result=_EMPTY, json_result=_GOOD, counter=counter)
    fn = f.make_translate_fn(model, "json_schema")
    assert await fn([(1, "hola")], "ru") == {1: "привет"}
    assert counter.get("json_schema") == 1 and counter.get("json_mode") == 1


async def test_translate_fn_json_mode_no_fallback():
    # when method is already json_mode there is no second attempt
    counter: dict[str, int] = {}
    model = FakeModel(schema_result=_GOOD, json_result=_EMPTY, counter=counter)
    fn = f.make_translate_fn(model, "json_mode")
    assert await fn([(1, "hola")], "ru") == {}
    assert counter.get("json_mode") == 1 and "json_schema" not in counter


async def test_resolve_translate_method_caches_in_kv(tmp_path):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    counter: dict[str, int] = {}
    model = FakeModel(schema_result=_GOOD, json_result=_GOOD, counter=counter)
    try:
        await storage.connect()
        m1 = await f.resolve_translate_method(storage, "openai:glm", model)
        assert m1 == "json_schema"
        assert await get_cached_method(storage, "openai:glm") == "json_schema"
        probes_after_first = counter.get("json_schema", 0)
        # second resolve reads the cache — no new probe
        m2 = await f.resolve_translate_method(storage, "openai:glm", model)
        assert m2 == "json_schema"
        assert counter.get("json_schema", 0) == probes_after_first
    finally:
        await storage.close()


# --- #143: suggester model-override factory (probe builds a suggest_fn for a model name) ---


def test_make_suggest_fn_factory_rejects_empty_name():
    build = f.make_suggest_fn_factory()
    with pytest.raises(ValueError):
        build("")


def test_make_suggest_fn_factory_wraps_init_errors(monkeypatch):
    def boom(name):
        raise RuntimeError("no such provider")

    monkeypatch.setattr(f, "init_chat_model", boom)
    build = f.make_suggest_fn_factory()
    with pytest.raises(ValueError) as exc:
        build("bogus:model")
    assert "bogus:model" in str(exc.value)


def test_make_suggest_fn_factory_builds_callable(monkeypatch):
    monkeypatch.setattr(f, "init_chat_model", lambda name: object())
    build = f.make_suggest_fn_factory()
    fn = build("openai:gpt-4o")
    assert callable(fn)
