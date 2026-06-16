"""Циклы 91–94: Суфлёр (#17) — черновик ответа в стиле прошлых переписок.

LLM-стек НЕ импортируется: suggest_fn инжектируется фейком (как chat_fn в
orchestrator), поэтому importorskip НЕ нужен — тесты зелёные на голом [dev].
Storage — настоящий tmp-SQLite (без сети). Профиль строится чистыми функциями.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tg_messenger.agent.suggest import (
    DEFAULT_HISTORY_LIMIT,
    StyleProfile,
    Suggester,
    build_style_profile,
    get_suggest_settings,
    last_read_key,
    load_last_read,
    load_style_profile,
    record_last_read,
    register_suggest_migrations,
    save_style_profile,
    set_suggest_settings,
    watch_read_receipts,
)
from tg_messenger.core.models import Message, MessageReadEvent
from tg_messenger.core.storage import Storage


def _msg(id, *, out, text, sender_id=None):
    return Message(
        id=id,
        dialog_id=42,
        sender_id=(1 if out else 2) if sender_id is None else sender_id,
        out=out,
        date=datetime(2024, 1, 1, 12, id, tzinfo=timezone.utc),
        text=text,
    )


class FakeClient:
    """Минимальный стенд: history по диалогу + запись send_text."""

    def __init__(self, history):
        self._history = history
        self.sent: list[dict] = []
        self.history_calls: list[tuple] = []

    async def history(self, peer, limit=50, offset_id=0):
        self.history_calls.append((peer, limit))
        return list(self._history)

    async def send_text(self, peer, text, reply_to=None):
        self.sent.append({"peer": peer, "text": text})
        return _msg(999, out=True, text=text)


def make_suggest_fn(reply="draft reply"):
    calls = []

    async def suggest_fn(context, profile):
        calls.append({"context": list(context), "profile": profile})
        return reply

    return suggest_fn, calls


# --- цикл 91: Suggester с инжектированным suggest_fn ---


async def test_suggest_collects_history_in_order_and_returns_text():
    history = [
        _msg(1, out=False, text="hi"),
        _msg(2, out=True, text="hello there"),
        _msg(3, out=False, text="how are you?"),
    ]
    client = FakeClient(history)
    fn, calls = make_suggest_fn("I am fine!")
    suggester = Suggester(client=client, suggest_fn=fn, history_limit=30)

    draft = await suggester.suggest(42)

    assert draft == "I am fine!"
    # history запрошена по этому диалогу
    assert client.history_calls == [(42, 30)]
    # контекст передан хронологически, с разметкой свой/чужой
    ctx = calls[0]["context"]
    assert [c.out for c in ctx] == [False, True, False]
    assert [c.text for c in ctx] == ["hi", "hello there", "how are you?"]


async def test_suggest_passes_profile_when_storage_present(tmp_path):
    history = [_msg(1, out=False, text="hi")]
    client = FakeClient(history)
    fn, calls = make_suggest_fn()
    storage = Storage(tmp_path / "s.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        profile = StyleProfile(avg_length=12.0, emoji_freq=0.5, examples=["yo"])
        await save_style_profile(storage, 42, profile)
        suggester = Suggester(client=client, suggest_fn=fn, storage=storage)
        await suggester.suggest(42)
    finally:
        await storage.close()
    assert calls[0]["profile"] == profile


async def test_suggest_profile_is_none_without_storage():
    client = FakeClient([_msg(1, out=False, text="hi")])
    fn, calls = make_suggest_fn()
    suggester = Suggester(client=client, suggest_fn=fn)
    await suggester.suggest(42)
    assert calls[0]["profile"] is None


# --- цикл 92: построение стилевого профиля ---


def test_build_style_profile_aggregates_and_picks_examples():
    msgs = [
        _msg(1, out=False, text="hey"),
        _msg(2, out=True, text="Hi! how's it going 😀"),   # свой ответ после входящего
        _msg(3, out=True, text="just chilling"),           # свой, но не сразу после входящего
        _msg(4, out=False, text="cool"),
        _msg(5, out=True, text="yeah 👍"),                  # свой ответ после входящего
    ]
    profile = build_style_profile(msgs)
    # агрегаты по ВСЕМ своим сообщениям
    assert profile.avg_length > 0
    assert profile.emoji_freq > 0  # есть эмодзи в своих
    # примеры — свои ответы, идущие сразу после входящего
    assert "Hi! how's it going 😀" in profile.examples
    assert "yeah 👍" in profile.examples
    assert "just chilling" not in profile.examples


def test_build_style_profile_empty_history_is_stub():
    profile = build_style_profile([])
    assert profile.avg_length == 0.0
    assert profile.emoji_freq == 0.0
    assert profile.examples == []
    assert profile.greetings == []
    assert profile.signatures == []


def test_build_style_profile_caps_examples_at_10():
    msgs = []
    mid = 0
    for _ in range(15):
        mid += 1
        msgs.append(_msg(mid, out=False, text="q"))
        mid += 1
        msgs.append(_msg(mid, out=True, text=f"a{mid}"))
    profile = build_style_profile(msgs)
    assert len(profile.examples) <= 10


# --- цикл 93: хранение профиля ---


async def test_save_and_load_style_profile_roundtrip(tmp_path):
    storage = Storage(tmp_path / "p.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        profile = StyleProfile(
            avg_length=20.0, emoji_freq=0.3,
            greetings=["hi"], signatures=["bye"], examples=["yo", "sup"],
        )
        await save_style_profile(storage, 42, profile)
        loaded = await load_style_profile(storage, 42)
        assert loaded == profile
    finally:
        await storage.close()


async def test_load_missing_profile_returns_none(tmp_path):
    storage = Storage(tmp_path / "p.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        assert await load_style_profile(storage, 999) is None
    finally:
        await storage.close()


async def test_save_style_profile_overwrites(tmp_path):
    storage = Storage(tmp_path / "p.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        await save_style_profile(storage, 42, StyleProfile(avg_length=1.0))
        await save_style_profile(storage, 42, StyleProfile(avg_length=2.0))
        loaded = await load_style_profile(storage, 42)
        assert loaded.avg_length == 2.0
    finally:
        await storage.close()


# --- цикл 94: деградация ---


async def test_suggest_works_when_profile_is_none_in_storage(tmp_path):
    """Storage есть, но профиля для диалога нет — suggest всё равно работает."""
    client = FakeClient([_msg(1, out=False, text="hi")])
    fn, calls = make_suggest_fn()
    storage = Storage(tmp_path / "p.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        suggester = Suggester(client=client, suggest_fn=fn, storage=storage)
        draft = await suggester.suggest(42)
        assert draft == "draft reply"
        assert calls[0]["profile"] is None
    finally:
        await storage.close()


async def test_suggest_fn_error_propagates_and_is_logged(caplog):
    client = FakeClient([_msg(1, out=False, text="hi")])

    async def boom(context, profile):
        raise RuntimeError("llm down")

    suggester = Suggester(client=client, suggest_fn=boom)
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="llm down"):
            await suggester.suggest(42)
    assert any("suggest" in r.message.lower() for r in caplog.records)


async def test_learn_builds_and_saves_profile(tmp_path):
    history = [
        _msg(1, out=False, text="hey"),
        _msg(2, out=True, text="hi there 😀"),
    ]
    client = FakeClient(history)
    fn, _ = make_suggest_fn()
    storage = Storage(tmp_path / "p.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        suggester = Suggester(client=client, suggest_fn=fn, storage=storage)
        profile = await suggester.learn(42)
        loaded = await load_style_profile(storage, 42)
        assert loaded == profile
        assert profile.examples == ["hi there 😀"]
    finally:
        await storage.close()


# --- цикл 98: фиксация last_read из listen_reads (outbox=True) ---


async def test_record_last_read_persists_outbox_receipt(tmp_path):
    storage = Storage(tmp_path / "r.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        ev = MessageReadEvent(dialog_id=7, max_id=42, outbox=True)
        await record_last_read(storage, ev)
        assert await load_last_read(storage, 7) == 42
        # ключ — стабильный, для отладки/совместимости
        assert last_read_key(7) == "last_read_7"
    finally:
        await storage.close()


async def test_record_last_read_ignores_inbox_receipt(tmp_path):
    """outbox=False — это МЫ прочитали чужое; суфлёру нужен только outbox."""
    storage = Storage(tmp_path / "r.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        await record_last_read(storage, MessageReadEvent(dialog_id=7, max_id=10, outbox=False))
        assert await load_last_read(storage, 7) is None
    finally:
        await storage.close()


class _ReadsClient:
    def __init__(self, events):
        self._events = events

    async def listen_reads(self):
        for ev in self._events:
            yield ev


async def test_watch_read_receipts_drains_and_records(tmp_path):
    storage = Storage(tmp_path / "r.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        client = _ReadsClient([
            MessageReadEvent(dialog_id=7, max_id=5, outbox=True),
            MessageReadEvent(dialog_id=7, max_id=9, outbox=True),
            MessageReadEvent(dialog_id=8, max_id=3, outbox=False),  # ignored
        ])
        await watch_read_receipts(client, storage)
        assert await load_last_read(storage, 7) == 9
        assert await load_last_read(storage, 8) is None
    finally:
        await storage.close()


# --- #143: live settings (enabled / history / model), persisted in kv ---


async def test_suggest_settings_defaults(tmp_path):
    storage = Storage(tmp_path / "set.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        settings = await get_suggest_settings(storage)
        assert settings == {"enabled": True, "history": DEFAULT_HISTORY_LIMIT, "model": None}
    finally:
        await storage.close()


async def test_suggest_settings_roundtrip_and_clear_model(tmp_path):
    storage = Storage(tmp_path / "set.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        await set_suggest_settings(storage, enabled=False, history=15, model="openai:gpt-4o")
        assert await get_suggest_settings(storage) == {
            "enabled": False, "history": 15, "model": "openai:gpt-4o",
        }
        # blank model clears the override (falls back to env/default)
        await set_suggest_settings(storage, enabled=True, history=10, model=None)
        assert await get_suggest_settings(storage) == {
            "enabled": True, "history": 10, "model": None,
        }
    finally:
        await storage.close()


async def test_set_suggest_settings_rejects_bad_history(tmp_path):
    storage = Storage(tmp_path / "set.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        with pytest.raises(ValueError):
            await set_suggest_settings(storage, enabled=True, history=0, model=None)
    finally:
        await storage.close()


async def test_suggest_disabled_returns_empty(tmp_path):
    storage = Storage(tmp_path / "set.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        client = FakeClient([_msg(1, out=False, text="hi")])
        fn, calls = make_suggest_fn("DRAFT")
        suggester = Suggester(client=client, suggest_fn=fn, storage=storage)
        await set_suggest_settings(storage, enabled=False, history=10, model=None)
        assert await suggester.suggest(42) == ""
        # disabled → no LLM call, no history fetch
        assert calls == []
        assert client.history_calls == []
        # re-enabling restores the draft
        await set_suggest_settings(storage, enabled=True, history=10, model=None)
        assert await suggester.suggest(42) == "DRAFT"
    finally:
        await storage.close()


async def test_stored_history_overrides_constructor(tmp_path):
    storage = Storage(tmp_path / "set.db")
    register_suggest_migrations(storage)
    await storage.connect()
    try:
        client = FakeClient([_msg(1, out=False, text="hi")])
        fn, _ = make_suggest_fn("DRAFT")
        # constructor says 30, but a stored value wins at runtime
        suggester = Suggester(client=client, suggest_fn=fn, storage=storage, history_limit=30)
        await set_suggest_settings(storage, enabled=True, history=7, model=None)
        await suggester.suggest(42)
        assert client.history_calls == [(42, 7)]
    finally:
        await storage.close()


def test_model_swap_seam():
    client = FakeClient([])
    fn, _ = make_suggest_fn("base")

    # without a factory the swap is unsupported and raises a clear error
    plain = Suggester(client=client, suggest_fn=fn)
    assert plain.supports_model_swap is False
    with pytest.raises(RuntimeError):
        plain.build_suggest_fn("openai:gpt-4o")

    # with a factory, build + set swaps the model contact in place
    def factory(name):
        async def built(context, profile):
            return f"M:{name}"
        return built

    sug = Suggester(client=client, suggest_fn=fn, suggest_fn_factory=factory)
    assert sug.supports_model_swap is True
    sug.set_suggest_fn(sug.build_suggest_fn("openai:gpt-4o"))


# --- #143 review: clearing the model override reverts to the default suggest_fn ---


def test_reset_suggest_fn_reverts_to_default():
    client = FakeClient([])

    async def default_fn(ctx, prof):
        return "DEFAULT"

    def factory(name):
        async def overridden(ctx, prof):
            return f"OVERRIDE:{name}"
        return overridden

    sug = Suggester(client=client, suggest_fn=default_fn, suggest_fn_factory=factory)
    # swap to an override, then clear → back to the default, not the override
    sug.set_suggest_fn(sug.build_suggest_fn("openai:gpt-4o"))
    sug.reset_suggest_fn()
    assert sug._suggest_fn is default_fn
