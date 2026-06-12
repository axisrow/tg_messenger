from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from tg_messenger.agent.outbound import (
    OutboundTranslator,
    detect_script_lang,
    get_dialog_lang,
    is_outbound_enabled,
    set_dialog_lang,
    set_outbound_enabled,
)
from tg_messenger.agent.suggest import StyleProfile, register_suggest_migrations, save_style_profile
from tg_messenger.agent.translate import set_user_lang
from tg_messenger.core.models import Message
from tg_messenger.core.storage import Storage


def _msg(mid: int, text: str, *, out: bool = False) -> Message:
    return Message(
        id=mid,
        dialog_id=7,
        sender_id=1 if out else 7,
        out=out,
        text=text,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class HistoryStore:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def history(self, dialog_id, limit=30):
        self.calls.append((dialog_id, limit))
        return self.messages


async def _storage(tmp_path):
    storage = Storage(tmp_path / "outbound.db")
    register_suggest_migrations(storage)
    await storage.connect()
    return storage


async def test_dialog_lang_and_enabled_kv(tmp_path):
    storage = await _storage(tmp_path)
    try:
        assert await get_dialog_lang(storage, 7) is None
        assert await is_outbound_enabled(storage, 7) is True
        await set_dialog_lang(storage, 7, "en", source="manual")
        await set_outbound_enabled(storage, 7, False)
        assert (await get_dialog_lang(storage, 7)).lang == "en"
        assert (await get_dialog_lang(storage, 7)).source == "manual"
        assert await is_outbound_enabled(storage, 7) is False
        await set_dialog_lang(storage, 7, None)
        await set_outbound_enabled(storage, 7, True)
        assert await get_dialog_lang(storage, 7) is None
        assert await is_outbound_enabled(storage, 7) is True
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["привет", "как дела"], None),
        (["привіт", "як справи"], None),
        (["안녕하세요"], "ko"),
        (["こんにちは"], "ja"),
        (["今日は東京"], "ja"),
        (["你好"], "zh"),
        (["東京大学"], "zh"),
        (["hello world"], None),
        (["123 😀"], None),
    ],
)
def test_detect_script_lang(texts, expected):
    assert detect_script_lang(texts) == expected


async def test_dialog_lang_uses_detector_for_cyrillic_before_caching(tmp_path):
    storage = await _storage(tmp_path)
    calls = []

    async def detect(texts):
        calls.append(list(texts))
        return "uk"

    outbound = OutboundTranslator(
        store=HistoryStore([_msg(1, "привіт"), _msg(2, "як справи")]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        assert await outbound.dialog_lang(7) == "uk"
        assert await outbound.dialog_lang(7) == "uk"
    finally:
        await storage.close()
    assert calls == [["привіт", "як справи"]]


async def test_dialog_lang_uses_detector_for_han_before_caching(tmp_path):
    storage = await _storage(tmp_path)
    calls = []

    async def detect(texts):
        calls.append(list(texts))
        return "ja"

    outbound = OutboundTranslator(
        store=HistoryStore([_msg(1, "東京大学")]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        assert await outbound.dialog_lang(7) == "ja"
        assert await outbound.dialog_lang(7) == "ja"
    finally:
        await storage.close()
    assert calls == [["東京大学"]]


async def test_dialog_lang_uses_llm_for_latin_and_caches(tmp_path):
    storage = await _storage(tmp_path)
    calls = []

    async def detect(texts):
        calls.append(list(texts))
        return "en"

    outbound = OutboundTranslator(
        store=HistoryStore([_msg(1, "hello"), _msg(2, "how are you")]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        assert await outbound.dialog_lang(7) == "en"
        assert await outbound.dialog_lang(7) == "en"
    finally:
        await storage.close()
    assert calls == [["hello", "how are you"]]


async def test_dialog_lang_detector_exception_returns_none(tmp_path, caplog):
    storage = await _storage(tmp_path)

    async def detect(texts):
        raise RuntimeError("detector down")

    outbound = OutboundTranslator(
        store=HistoryStore([_msg(1, "hello")]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        with caplog.at_level(logging.ERROR, logger="tg_messenger.agent.outbound"):
            assert await outbound.dialog_lang(7) is None
    finally:
        await storage.close()
    assert any("dialog language detection failed" in rec.message for rec in caplog.records)


async def test_dialog_lang_invalid_detector_code_warns(tmp_path, caplog):
    storage = await _storage(tmp_path)

    async def detect(texts):
        return "english"

    outbound = OutboundTranslator(
        store=HistoryStore([_msg(1, "hello")]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.outbound"):
            assert await outbound.dialog_lang(7) is None
    finally:
        await storage.close()
    assert any("invalid code" in rec.message for rec in caplog.records)


async def test_applies_truth_table_and_groups(tmp_path):
    storage = await _storage(tmp_path)
    await set_user_lang(storage, "ru")
    await set_dialog_lang(storage, -100200, "en", source="manual")
    outbound = OutboundTranslator(
        store=HistoryStore([]),
        storage=storage,
        variants_fn=None,
    )
    try:
        assert await outbound.applies(-100200, "привет") == "en"
        assert await outbound.applies(-100200, "hello") is None
        await set_outbound_enabled(storage, -100200, False)
        assert await outbound.applies(-100200, "привет") is None
    finally:
        await storage.close()


async def test_applies_latin_user_to_latin_dialog_uses_exact_detection(tmp_path):
    storage = await _storage(tmp_path)
    await set_user_lang(storage, "en")
    await set_dialog_lang(storage, 7, "es", source="manual")
    calls = []

    async def detect(texts):
        calls.append(list(texts))
        return "en"

    outbound = OutboundTranslator(
        store=HistoryStore([]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        assert await outbound.applies(7, "hello") == "es"
    finally:
        await storage.close()
    assert calls == [["hello"]]


async def test_applies_suppresses_latin_draft_when_detector_matches_dialog(tmp_path):
    storage = await _storage(tmp_path)
    await set_user_lang(storage, "en")
    await set_dialog_lang(storage, 7, "es", source="manual")
    calls = []

    async def detect(texts):
        calls.append(list(texts))
        return "es"

    outbound = OutboundTranslator(
        store=HistoryStore([]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        assert await outbound.applies(7, "hola") is None
    finally:
        await storage.close()
    assert calls == [["hola"]]


async def test_applies_cyrillic_user_to_non_cyrillic_dialog_uses_exact_detection(tmp_path):
    storage = await _storage(tmp_path)
    await set_user_lang(storage, "uk")
    await set_dialog_lang(storage, 7, "en", source="manual")
    calls = []

    async def detect(texts):
        calls.append(list(texts))
        return "uk"

    outbound = OutboundTranslator(
        store=HistoryStore([]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        assert await outbound.applies(7, "привіт") == "en"
    finally:
        await storage.close()
    assert calls == [["привіт"]]


async def test_applies_han_user_to_non_han_dialog_uses_exact_detection(tmp_path):
    storage = await _storage(tmp_path)
    await set_user_lang(storage, "ja")
    await set_dialog_lang(storage, 7, "en", source="manual")
    calls = []

    async def detect(texts):
        calls.append(list(texts))
        return "ja"

    outbound = OutboundTranslator(
        store=HistoryStore([]),
        storage=storage,
        variants_fn=None,
        detect_lang_fn=detect,
    )
    try:
        assert await outbound.applies(7, "東京大学") == "en"
    finally:
        await storage.close()
    assert calls == [["東京大学"]]


async def test_applies_storage_error_returns_none(caplog):
    class BrokenStorage:
        async def get_value(self, key):
            raise RuntimeError("storage down")

    outbound = OutboundTranslator(
        store=HistoryStore([]),
        storage=BrokenStorage(),
        variants_fn=None,
    )
    with caplog.at_level(logging.ERROR, logger="tg_messenger.agent.outbound"):
        assert await outbound.applies(7, "hello") is None
    assert any("outbound translation applicability failed" in rec.message for rec in caplog.records)


async def test_variants_passes_profile_and_context(tmp_path):
    storage = await _storage(tmp_path)
    await save_style_profile(storage, 7, StyleProfile(avg_length=4.0, examples=["ok"]))
    calls = []

    async def variants(draft, target_lang, profile, context):
        calls.append((draft, target_lang, profile, context))
        return [" hi ", "", "hello", "hey", "ignored"]

    outbound = OutboundTranslator(
        store=HistoryStore([_msg(1, "hello"), _msg(2, "ок", out=True)]),
        storage=storage,
        variants_fn=variants,
    )
    try:
        result = await outbound.variants(7, "привет", "en")
    finally:
        await storage.close()
    assert result == ["hi", "hello", "hey"]
    draft, target, profile, context = calls[0]
    assert draft == "привет"
    assert target == "en"
    assert profile.examples == ["ok"]
    assert [m.text for m in context] == ["hello", "ок"]


async def test_variants_all_empty_raises(tmp_path):
    storage = await _storage(tmp_path)

    async def variants(draft, target_lang, profile, context):
        return ["", "   ", None]

    outbound = OutboundTranslator(
        store=HistoryStore([]),
        storage=storage,
        variants_fn=variants,
    )
    try:
        with pytest.raises(ValueError, match="no variants"):
            await outbound.variants(7, "привет", "en")
    finally:
        await storage.close()
