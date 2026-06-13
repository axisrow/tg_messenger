"""Tests for OutboundSendCoordinator (#73) — UI-agnostic outbound orchestration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from tg_messenger.agent.outbound_coordinator import OutboundSendCoordinator
from tg_messenger.core.models import Message


def _msg(mid: int = 100, text: str = "translated") -> Message:
    return Message(
        id=mid, dialog_id=7, sender_id=1, out=True, text=text,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class FakeOutbound:
    """Stand-in for OutboundTranslator: prepare_variants returns a canned result."""

    def __init__(self, target_lang="en", variants=None, error=None, block=None):
        self.target_lang = target_lang
        self.variants = variants if variants is not None else ["v1", "v2"]
        self.error = error
        self.block = block  # an asyncio.Event to block on, for timeout/race tests
        self.prepare_calls = []

    async def prepare_variants(self, dialog_id, draft_text, *, telegram_lang_code=None):
        self.prepare_calls.append((dialog_id, draft_text, telegram_lang_code))
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error
        if self.target_lang is None:
            return None, []
        return self.target_lang, list(self.variants)


class FakeStore:
    """Stand-in for MessageStore: records record_outgoing calls; carries a storage."""

    def __init__(self, storage):
        self.storage = storage
        self.recorded = []

    async def record_outgoing(self, dialog_id, message, *, source_text, source_lang):
        self.recorded.append((dialog_id, message.id, source_text, source_lang))


class FakeStorage:
    """KV-only storage stub for user_lang lookups."""

    def __init__(self, user_lang="ru"):
        self._values = {"user_lang": user_lang} if user_lang else {}

    async def get_value(self, key):
        return self._values.get(key)


def _coordinator(*, outbound=None, store=None, user_lang="ru", env=None, clock=None):
    storage = FakeStorage(user_lang=user_lang)
    outbound = outbound or FakeOutbound()
    store = store or FakeStore(storage)
    t = {"now": 1000.0}
    return OutboundSendCoordinator(
        outbound=outbound,
        store=store,
        storage=storage,
        env=env if env is not None else {},
        clock=clock or (lambda: t["now"]),
    ), t


# --- prepare ----------------------------------------------------------------


async def test_prepare_blank_draft_is_invalid_empty():
    coord, _ = _coordinator()
    result = await coord.prepare(7, "   ")
    assert result.status == "invalid_empty"


async def test_prepare_no_outbound_is_disabled():
    storage = FakeStorage()
    coord = OutboundSendCoordinator(
        outbound=None, store=FakeStore(storage), storage=storage, env={},
    )
    result = await coord.prepare(7, "hello")
    assert result.status == "disabled"


async def test_prepare_not_applicable_when_target_is_none():
    coord, _ = _coordinator(outbound=FakeOutbound(target_lang=None))
    result = await coord.prepare(7, "hello")
    assert result.status == "not_applicable"


async def test_prepare_ready_returns_token_and_variants():
    coord, _ = _coordinator(outbound=FakeOutbound(target_lang="en", variants=["a", "b"]))
    result = await coord.prepare(7, "привет", telegram_lang_code="en")
    assert result.status == "ready"
    assert result.target_lang == "en"
    assert result.variants == ["a", "b"]
    assert result.token  # an opaque token was issued


async def test_prepare_reads_history_once_via_prepare_variants():
    ob = FakeOutbound()
    coord, _ = _coordinator(outbound=ob)
    await coord.prepare(7, "hello")
    assert len(ob.prepare_calls) == 1


async def test_prepare_error_becomes_error_result_and_logs(caplog):
    coord, _ = _coordinator(outbound=FakeOutbound(error=RuntimeError("llm down")))
    with caplog.at_level(logging.WARNING):
        result = await coord.prepare(7, "hello")
    assert result.status == "error"
    assert any("outbound" in r.message.lower() for r in caplog.records)


async def test_prepare_timeout_becomes_error_result():
    gate = asyncio.Event()  # never set → prepare_variants blocks → timeout
    coord = OutboundSendCoordinator(
        outbound=FakeOutbound(block=gate),
        store=FakeStore(FakeStorage()),
        storage=FakeStorage(),
        env={},
        timeout=0.01,
    )
    result = await coord.prepare(7, "hello")
    assert result.status == "error"


# --- send_variant -----------------------------------------------------------


async def test_send_variant_sends_records_and_consumes_token():
    store = FakeStore(FakeStorage(user_lang="ru"))
    coord, _ = _coordinator(store=store)
    ready = await coord.prepare(7, "привет", owner_id="c1")
    sent = []

    async def send_fn(peer, text):
        sent.append((peer, text))
        return _msg(text=text)

    msg = await coord.send_variant(7, ready.token, ready.variants[0], send_fn, owner_id="c1")
    assert sent == [(7, ready.variants[0])]
    # source recorded once, with the original draft and the user lang
    assert store.recorded == [(7, msg.id, "привет", "ru")]
    # the returned message carries the original beneath the sent variant
    assert msg.translated_text == "привет"
    # token consumed: a second use fails
    with pytest.raises(Exception):
        await coord.send_variant(7, ready.token, ready.variants[0], send_fn, owner_id="c1")


async def test_send_variant_does_not_mutate_the_sent_message():
    # #73 review nit: _record_source returns a copy with translated_text — it must NOT
    # mutate the Message the send_fn returned (which a UI may hold/cache elsewhere).
    store = FakeStore(FakeStorage(user_lang="ru"))
    coord, _ = _coordinator(store=store)
    ready = await coord.prepare(7, "привет", owner_id="c1")
    original = _msg(text=ready.variants[0])

    async def send_fn(peer, text):
        return original

    returned = await coord.send_variant(7, ready.token, ready.variants[0], send_fn, owner_id="c1")
    assert original.translated_text is None  # caller's object untouched
    assert returned is not original
    assert returned.translated_text == "привет"


async def test_send_variant_invalid_token_does_not_send():
    coord, _ = _coordinator()
    sent = []

    async def send_fn(peer, text):
        sent.append((peer, text))
        return _msg()

    with pytest.raises(Exception):
        await coord.send_variant(7, "bogus-token", "v1", send_fn, owner_id="c1")
    assert sent == []


async def test_send_variant_failure_restores_token_for_retry():
    coord, _ = _coordinator()
    ready = await coord.prepare(7, "привет", owner_id="c1")
    attempts = []

    async def failing(peer, text):
        attempts.append(text)
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        await coord.send_variant(7, ready.token, ready.variants[0], failing, owner_id="c1")

    # the token survives the failure → a retry can still send
    async def ok(peer, text):
        attempts.append(text)
        return _msg(text=text)

    msg = await coord.send_variant(7, ready.token, ready.variants[0], ok, owner_id="c1")
    assert msg.id == 100
    assert len(attempts) == 2  # one failed, one succeeded


async def test_send_variant_concurrent_double_submit_sends_once():
    coord, _ = _coordinator()
    ready = await coord.prepare(7, "привет", owner_id="c1")
    sent = []
    gate = asyncio.Event()

    async def slow_send(peer, text):
        sent.append((peer, text))
        await gate.wait()
        return _msg(text=text)

    # two concurrent submits of the same token: mark-sending must let only one through
    task_a = asyncio.create_task(
        coord.send_variant(7, ready.token, ready.variants[0], slow_send, owner_id="c1")
    )
    task_b = asyncio.create_task(
        coord.send_variant(7, ready.token, ready.variants[0], slow_send, owner_id="c1")
    )
    await asyncio.sleep(0)  # let both start; one should reject before sending
    gate.set()
    results = await asyncio.gather(task_a, task_b, return_exceptions=True)
    successes = [r for r in results if isinstance(r, Message)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(sent) == 1, sent  # exactly one network send
    assert len(successes) == 1 and len(failures) == 1


async def test_send_variant_wrong_owner_rejected():
    coord, _ = _coordinator()
    ready = await coord.prepare(7, "привет", owner_id="c1")

    async def send_fn(peer, text):
        return _msg(text=text)

    with pytest.raises(Exception):
        await coord.send_variant(7, ready.token, ready.variants[0], send_fn, owner_id="other")


async def test_send_variant_wrong_dialog_rejected():
    coord, _ = _coordinator()
    ready = await coord.prepare(7, "привет", owner_id="c1")

    async def send_fn(peer, text):
        return _msg(text=text)

    with pytest.raises(Exception):
        await coord.send_variant(999, ready.token, ready.variants[0], send_fn, owner_id="c1")


async def test_send_variant_non_variant_text_rejected():
    coord, _ = _coordinator()
    ready = await coord.prepare(7, "привет", owner_id="c1")

    async def send_fn(peer, text):
        return _msg(text=text)

    with pytest.raises(Exception):
        await coord.send_variant(7, ready.token, "not-a-variant", send_fn, owner_id="c1")


# --- send_original ----------------------------------------------------------


async def test_send_original_sends_without_recording_source():
    store = FakeStore(FakeStorage())
    coord, _ = _coordinator(store=store)
    sent = []

    async def send_fn(peer, text):
        sent.append((peer, text))
        return _msg(text=text)

    msg = await coord.send_original(7, "original text", send_fn)
    assert sent == [(7, "original text")]
    assert store.recorded == []  # no source recording for an original send
    assert msg.text == "original text"


# --- token store bounds -----------------------------------------------------


async def test_token_store_is_lru_bounded():
    coord = OutboundSendCoordinator(
        outbound=FakeOutbound(),
        store=FakeStore(FakeStorage()),
        storage=FakeStorage(),
        env={},
        token_max=3,
    )
    tokens = []
    for i in range(5):
        r = await coord.prepare(7, f"draft {i}")
        tokens.append(r.token)
    # at most token_max live tokens; the oldest were evicted
    assert coord._token_count() <= 3
