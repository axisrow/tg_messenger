from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tg_messenger.agent.translate import (
    Translator,
    get_user_lang,
    needs_translation,
    set_user_lang,
    translate_model_from_env,
)
from tg_messenger.core.message_store import MessageStore, register_message_store_migrations
from tg_messenger.core.models import Message
from tg_messenger.core.storage import Storage


def _msg(mid: int, text: str) -> Message:
    return Message(
        id=mid,
        dialog_id=7,
        sender_id=7,
        out=False,
        text=text,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [
        ("привет", "ru", True),
        ("hello", "ru", True),
        ("你好", "ru", True),
        ("123 😀", "ru", False),
        ("hello", "en", True),
        ("hola", "en", True),
    ],
)
def test_needs_translation_script_heuristic(text, lang, expected):
    assert needs_translation(text, lang) is expected


async def test_user_lang_kv_overrides_env_and_clear(tmp_path):
    storage = Storage(tmp_path / "t.db")
    await storage.connect()
    try:
        assert await get_user_lang(storage, {"TG_USER_LANG": "ru"}) == "ru"
        await set_user_lang(storage, "en")
        assert await get_user_lang(storage, {"TG_USER_LANG": "ru"}) == "en"
        await set_user_lang(storage, None)
        assert await get_user_lang(storage, {"TG_USER_LANG": "ru"}) == "ru"
    finally:
        await storage.close()


def test_translate_model_from_env_prefers_translate_model():
    assert translate_model_from_env({"TG_TRANSLATE_MODEL": "openai:x", "TG_AGENT_MODEL": "openai:y"}) == "openai:x"
    assert translate_model_from_env({"TG_AGENT_MODEL": "openai:y"}) == "openai:y"
    assert translate_model_from_env({}) is None


async def test_translator_batches_and_caches(tmp_path):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    calls = []

    async def translate_fn(batch, lang):
        calls.append((list(batch), lang))
        return {mid: (None if text == "привет" else f"ru:{text}") for mid, text in batch}

    store = MessageStore(client=object(), storage=storage)
    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"}, batch_size=2)
    try:
        await store.connect()
        messages = [_msg(1, "hello"), _msg(2, "world"), _msg(3, "привет")]
        first = await translator.translate_history(7, messages)
        second = await translator.translate_history(7, messages)
    finally:
        await store.close()

    assert [m.translated_text for m in first] == ["ru:hello", "ru:world", None]
    assert [m.translated_text for m in second] == ["ru:hello", "ru:world", None]
    assert calls == [([(1, "hello"), (2, "world")], "ru"), ([(3, "привет")], "ru")]


async def test_translator_does_not_cache_same_script_text_as_already_translated(tmp_path):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    calls = []

    async def translate_fn(batch, lang):
        calls.append((list(batch), lang))
        return {mid: "hello" for mid, _ in batch}

    store = MessageStore(client=object(), storage=storage)
    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "en"})
    try:
        await store.connect()
        first = await translator.translate_history(7, [_msg(1, "hola")])
        second = await translator.translate_history(7, [_msg(1, "hola")])
    finally:
        await store.close()

    assert first[0].translated_text == "hello"
    assert second[0].translated_text == "hello"
    assert calls == [([(1, "hola")], "en")]


async def test_translator_failure_is_logged_and_unraised(tmp_path, caplog):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)

    async def boom(batch, lang):
        raise RuntimeError("translator down")

    store = MessageStore(client=object(), storage=storage)
    translator = Translator(storage=storage, translate_fn=boom, env={"TG_USER_LANG": "ru"})
    try:
        await store.connect()
        with caplog.at_level("ERROR", logger="tg_messenger.agent.translate"):
            result = await translator.translate_history(7, [_msg(1, "hello")])
    finally:
        await store.close()

    assert result[0].translated_text is None
    assert any("translation batch failed" in rec.message for rec in caplog.records)
