from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tg_messenger.agent.translate import (
    Translator,
    get_known_langs,
    get_translate_mode,
    get_unknown_langs,
    get_user_lang,
    needs_translation,
    resolve_skip_only,
    set_known_langs,
    set_translate_mode,
    set_unknown_langs,
    set_user_lang,
    translate_model_from_env,
)
from tg_messenger.core.message_store import (
    MessageStore,
    get_message_translation,
    register_message_store_migrations,
)
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


async def test_set_user_lang_validates_code(tmp_path):
    storage = Storage(tmp_path / "t.db")
    await storage.connect()
    try:
        await set_user_lang(storage, "EN")
        assert await get_user_lang(storage, {}) == "en"
        with pytest.raises(ValueError, match="invalid language code"):
            await set_user_lang(storage, "english")
        with pytest.raises(ValueError, match="invalid language code"):
            await set_user_lang(storage, "fr")
        assert await get_user_lang(storage, {}) == "en"
    finally:
        await storage.close()


async def test_get_user_lang_warns_for_unsupported_stored_or_env_code(tmp_path, caplog):
    storage = Storage(tmp_path / "t.db")
    await storage.connect()
    try:
        await storage.set_value("user_lang", "fr")
        with caplog.at_level("WARNING", logger="tg_messenger.agent.translate"):
            assert await get_user_lang(storage, {"TG_USER_LANG": "de"}) is None
        assert any("unsupported stored user language code" in rec.message for rec in caplog.records)
        assert all("fr" not in rec.message for rec in caplog.records)

        await storage.execute("DELETE FROM kv WHERE key = ?", ("user_lang",))
        caplog.clear()
        with caplog.at_level("WARNING", logger="tg_messenger.agent.translate"):
            assert await get_user_lang(storage, {"TG_USER_LANG": "de"}) is None
        assert any("unsupported TG_USER_LANG value" in rec.message for rec in caplog.records)
        assert all("de" not in rec.message for rec in caplog.records)
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

    async def translate_fn(batch, lang, skip=(), only=()):
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

    async def translate_fn(batch, lang, skip=(), only=()):
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


async def test_translator_caches_missing_model_response_ids_as_null(tmp_path):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    calls = []

    async def translate_fn(batch, lang, skip=(), only=()):
        calls.append((list(batch), lang))
        return {}

    store = MessageStore(client=object(), storage=storage)
    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await store.connect()
        first = await translator.translate_history(7, [_msg(1, "hello")])
        second = await translator.translate_history(7, [_msg(1, "hello")])
    finally:
        await store.close()

    assert first[0].translated_text is None
    assert second[0].translated_text is None
    assert calls == [([(1, "hello")], "ru")]


async def test_translator_skips_outgoing_messages(tmp_path):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    calls = []

    async def translate_fn(batch, lang, skip=(), only=()):
        calls.append((list(batch), lang))
        return {mid: "ru:own" for mid, _ in batch}

    store = MessageStore(client=object(), storage=storage)
    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await store.connect()
        result = await translator.translate_history(
            7,
            [
                Message(
                    id=1,
                    dialog_id=7,
                    sender_id=1,
                    out=True,
                    text="hello",
                    date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                )
            ],
        )
    finally:
        await store.close()

    assert result[0].translated_text is None
    assert calls == []


async def test_translator_retranslates_when_source_text_changes(tmp_path):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    calls = []

    async def translate_fn(batch, lang, skip=(), only=()):
        calls.append((list(batch), lang))
        return {mid: f"ru:{text}" for mid, text in batch}

    store = MessageStore(client=object(), storage=storage)
    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await store.connect()
        first = await translator.translate_history(7, [_msg(1, "hello")])
        await store.ingest(_msg(1, "updated"))
        second = await translator.translate_history(7, [_msg(1, "updated")])
    finally:
        await store.close()

    assert first[0].translated_text == "ru:hello"
    assert second[0].translated_text == "ru:updated"
    assert calls == [([(1, "hello")], "ru"), ([(1, "updated")], "ru")]


async def test_translator_failure_is_logged_and_unraised(tmp_path, caplog):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)

    async def boom(batch, lang, skip=(), only=()):
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


# --- translation mode / known-unknown languages -----------------------------------------


async def _connected_store(tmp_path):
    storage = Storage(tmp_path / "t.db")
    register_message_store_migrations(storage)
    store = MessageStore(client=object(), storage=storage)
    await store.connect()
    return storage, store


async def test_translate_mode_defaults_from_user_lang(tmp_path):
    storage, store = await _connected_store(tmp_path)
    try:
        # no stored mode + no target → off
        assert await get_translate_mode(storage, {}) == "off"
        # no stored mode + a target language → all_unknown (back-compat with the old single-lang setup)
        assert await get_translate_mode(storage, {"TG_USER_LANG": "ru"}) == "all_unknown"
        await set_translate_mode(storage, "skip_known")
        assert await get_translate_mode(storage, {}) == "skip_known"
    finally:
        await store.close()


async def test_off_mode_returns_originals_without_calling_llm(tmp_path):
    storage, store = await _connected_store(tmp_path)
    calls = []

    async def translate_fn(batch, lang, skip=(), only=()):
        calls.append((list(batch), lang, list(skip), list(only)))
        return {mid: f"x:{text}" for mid, text in batch}

    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await set_translate_mode(storage, "off")
        result = await translator.translate_history(7, [_msg(1, "hello")])
    finally:
        await store.close()

    assert result[0].translated_text is None
    assert calls == []


async def test_all_unknown_skips_known_langs_plus_target(tmp_path):
    storage, store = await _connected_store(tmp_path)
    seen = {}

    async def translate_fn(batch, lang, skip=(), only=()):
        seen["skip"] = list(skip)
        seen["only"] = list(only)
        return {mid: f"ru:{text}" for mid, text in batch}

    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await set_translate_mode(storage, "all_unknown")
        await set_known_langs(storage, "en")
        await translator.translate_history(7, [_msg(1, "hallo")])
    finally:
        await store.close()

    # all_unknown → skip = known ∪ {target}; no whitelist
    assert set(seen["skip"]) == {"en", "ru"}
    assert seen["only"] == []


async def test_skip_known_passes_known_list(tmp_path):
    storage, store = await _connected_store(tmp_path)
    seen = {}

    async def translate_fn(batch, lang, skip=(), only=()):
        seen["skip"] = list(skip)
        seen["only"] = list(only)
        return {mid: f"ru:{text}" for mid, text in batch}

    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await set_translate_mode(storage, "skip_known")
        await set_known_langs(storage, "ru, en")
        await translator.translate_history(7, [_msg(1, "hallo")])
    finally:
        await store.close()

    assert seen["skip"] == ["ru", "en"]
    assert seen["only"] == []


async def test_only_unknown_passes_whitelist(tmp_path):
    storage, store = await _connected_store(tmp_path)
    seen = {}

    async def translate_fn(batch, lang, skip=(), only=()):
        seen["skip"] = list(skip)
        seen["only"] = list(only)
        return {mid: f"ru:{text}" for mid, text in batch}

    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await set_translate_mode(storage, "only_unknown")
        await set_unknown_langs(storage, "ja, ko")
        await translator.translate_history(7, [_msg(1, "こんにちは")])
    finally:
        await store.close()

    assert seen["only"] == ["ja", "ko"]
    assert seen["skip"] == []


async def test_resolve_skip_only_per_mode(tmp_path):
    storage, store = await _connected_store(tmp_path)
    try:
        await set_known_langs(storage, "ru")
        await set_unknown_langs(storage, "ja")
        await set_translate_mode(storage, "all_unknown")
        assert await resolve_skip_only(storage, "en", {}) == (["ru", "en"], [])
        await set_translate_mode(storage, "skip_known")
        assert await resolve_skip_only(storage, "en", {}) == (["ru"], [])
        await set_translate_mode(storage, "only_unknown")
        assert await resolve_skip_only(storage, "en", {}) == ([], ["ja"])
    finally:
        await store.close()


async def test_changing_mode_clears_translation_cache(tmp_path):
    storage, store = await _connected_store(tmp_path)
    calls = []

    async def translate_fn(batch, lang, skip=(), only=()):
        calls.append(list(batch))
        return {mid: f"ru:{text}" for mid, text in batch}

    translator = Translator(storage=storage, translate_fn=translate_fn, env={"TG_USER_LANG": "ru"})
    try:
        await set_translate_mode(storage, "all_unknown")
        first = await translator.translate_history(7, [_msg(1, "hallo")])
        assert first[0].translated_text == "ru:hallo"
        # the row is now cached
        assert await get_message_translation(storage, 7, 1, "ru", source_text="hallo") is not None
        # changing the mode must invalidate the cache → next read re-calls the LLM
        await set_known_langs(storage, "en")
        assert await get_message_translation(storage, 7, 1, "ru", source_text="hallo") is None
        await translator.translate_history(7, [_msg(1, "hallo")])
    finally:
        await store.close()

    assert calls == [[(1, "hallo")], [(1, "hallo")]]


async def test_lang_list_setters_validate_and_dedupe(tmp_path):
    storage, store = await _connected_store(tmp_path)
    try:
        await set_known_langs(storage, "ru, en, ru")
        assert await get_known_langs(storage) == ["ru", "en"]
        with pytest.raises(ValueError, match="invalid language code"):
            await set_known_langs(storage, "ru, fr")
        # the failed write left the previous value intact
        assert await get_known_langs(storage) == ["ru", "en"]
        await set_unknown_langs(storage, ["ja", "ja", "ko"])
        assert await get_unknown_langs(storage) == ["ja", "ko"]
    finally:
        await store.close()
