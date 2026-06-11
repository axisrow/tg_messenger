"""Ghostwrite (#18, этап 2 суфлёра) — авто-ответы в стиле владельца в явно включённых DM.

Suggester инжектируется фейком (как suggest_fn в #17), Storage — настоящий tmp-SQLite:
никакого LLM-стека и сети, поэтому importorskip НЕ нужен — тесты зелёные на голом [dev].
clock инжектится — никакого реального sleep (filterwarnings=error).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from tg_messenger.agent import ghostwrite as gw
from tg_messenger.agent.ghostwrite import (
    PAUSE_FOREVER,
    GhostwriteEngine,
    disable_dialog,
    enable_dialog,
    is_active,
    list_enabled,
    pause_all,
    pause_dialog,
    register_ghostwrite_migrations,
    resume_dialog,
)
from tg_messenger.core.models import IncomingEvent, Message, OutgoingEvent, User
from tg_messenger.core.storage import Storage

DIALOG = 7
OTHER = 9


def _imsg(text="hi", *, dialog_id=DIALOG, sender_id=DIALOG, msg_id=1):
    return Message(
        id=msg_id, dialog_id=dialog_id, sender_id=sender_id, out=False,
        text=text, date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _omsg(text="hi", *, dialog_id=DIALOG, msg_id=1):
    return Message(
        id=msg_id, dialog_id=dialog_id, sender_id=1, out=True,
        text=text, date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# --- Цикл A: storage слой ----------------------------------------------------------


async def _storage(tmp_path):
    storage = Storage(tmp_path / "gw.db")
    register_ghostwrite_migrations(storage)
    await storage.connect()
    return storage


async def test_migration_creates_tables(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await storage.fetchall("SELECT * FROM ghostwrite_dialogs")
        await storage.fetchall("SELECT * FROM ghostwrite_log")
    finally:
        await storage.close()


async def test_enable_disable_list(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await enable_dialog(storage, DIALOG)
        await enable_dialog(storage, OTHER)
        assert sorted(await list_enabled(storage)) == [DIALOG, OTHER]
        await disable_dialog(storage, OTHER)
        assert await list_enabled(storage) == [DIALOG]
    finally:
        await storage.close()


async def test_enable_is_idempotent(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await enable_dialog(storage, DIALOG)
        await enable_dialog(storage, DIALOG)  # upsert, not a duplicate row
        assert await list_enabled(storage) == [DIALOG]
    finally:
        await storage.close()


async def test_is_active_true_when_enabled_not_paused(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await enable_dialog(storage, DIALOG)
        assert await is_active(storage, DIALOG, now=0.0) is True
        # not enabled at all
        assert await is_active(storage, OTHER, now=0.0) is False
    finally:
        await storage.close()


async def test_disabled_dialog_not_active(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await enable_dialog(storage, DIALOG)
        await disable_dialog(storage, DIALOG)
        assert await is_active(storage, DIALOG, now=0.0) is False
    finally:
        await storage.close()


async def test_pause_then_resume(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await enable_dialog(storage, DIALOG)
        await pause_dialog(storage, DIALOG, paused_until=100.0)
        assert await is_active(storage, DIALOG, now=50.0) is False   # within pause
        assert await is_active(storage, DIALOG, now=150.0) is True   # pause elapsed
        await pause_dialog(storage, DIALOG, paused_until=100.0)
        await resume_dialog(storage, DIALOG)
        assert await is_active(storage, DIALOG, now=50.0) is True    # pause cleared
    finally:
        await storage.close()


async def test_pause_unknown_dialog_is_noop_not_enable(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await pause_dialog(storage, DIALOG, paused_until=100.0)
        assert await list_enabled(storage) == []
        assert await is_active(storage, DIALOG, now=0.0) is False
    finally:
        await storage.close()


async def test_pause_all(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await enable_dialog(storage, DIALOG)
        await enable_dialog(storage, OTHER)
        await pause_all(storage)
        assert await is_active(storage, DIALOG, now=1e18) is False
        assert await is_active(storage, OTHER, now=1e18) is False
        # PAUSE_FOREVER is far enough that no realistic clock un-pauses it
        assert PAUSE_FOREVER > 1e18
    finally:
        await storage.close()


# --- движок: фейки -----------------------------------------------------------------


class FakeSuggester:
    """Stub over the #17 Suggester: records suggest() calls, returns a canned reply."""

    def __init__(self, reply="auto reply"):
        self.reply = reply
        self.calls: list[int] = []
        self.raise_on: int | None = None

    async def suggest(self, dialog_id: int) -> str:
        self.calls.append(dialog_id)
        if self.raise_on == dialog_id:
            raise RuntimeError("suggest boom")
        return self.reply


class FakeGwClient:
    """Core-client stub: records sends, returns a Message with a fresh id."""

    def __init__(self):
        self.sent: list[tuple] = []
        self._next_id = 1000

    async def get_me(self):
        return User(id=1, first_name="Me")

    async def send_text(self, peer, text, reply_to=None):
        self._next_id += 1
        self.sent.append((peer, text))
        return _omsg(text=text, dialog_id=peer, msg_id=self._next_id)


def _mk_engine(
    tmp_path,
    *,
    enforce=False,
    suggester=None,
    max_per_hour=10,
    clock=None,
    own_cache_size=1000,
    max_rate_dialogs=10_000,
):
    storage = Storage(tmp_path / "gw.db")
    register_ghostwrite_migrations(storage)
    client = FakeGwClient()
    suggester = suggester or FakeSuggester()
    t = {"now": 0.0}
    engine = GhostwriteEngine(
        client, suggester, storage,
        enforce=enforce, max_per_hour=max_per_hour,
        clock=clock or (lambda: t["now"]),
        own_cache_size=own_cache_size,
        max_rate_dialogs=max_rate_dialogs,
    )
    return engine, client, suggester, storage, t


# --- Цикл B: движок dry-run --------------------------------------------------------


async def test_default_clock_is_wall_time_for_persistent_pauses(tmp_path):
    storage = Storage(tmp_path / "gw.db")
    register_ghostwrite_migrations(storage)
    engine = GhostwriteEngine(FakeGwClient(), FakeSuggester(), storage)
    assert engine._clock is time.time


async def test_dry_run_calls_suggester_not_send(tmp_path, caplog):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=False)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        with caplog.at_level(logging.INFO, logger="tg_messenger.agent.ghostwrite"):
            await engine.process_incoming(_imsg())
        assert suggester.calls == [DIALOG]      # suggester WAS asked
        assert client.sent == []                # but nothing sent (dry-run)
        assert any("would" in r.message for r in caplog.records)
        log = await storage.fetchall("SELECT dialog_id, reply, dry_run FROM ghostwrite_log")
        assert log == [(DIALOG, "auto reply", 1)]
    finally:
        await storage.close()


async def test_disabled_dialog_does_not_call_suggester(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=False)
    await storage.connect()
    try:
        # DIALOG never enabled
        await engine.process_incoming(_imsg())
        assert suggester.calls == []
        assert client.sent == []
    finally:
        await storage.close()


# --- Цикл C: enforce + лимит/час ---------------------------------------------------


async def test_enforce_sends_and_journals(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        await engine.process_incoming(_imsg())
        assert client.sent == [(DIALOG, "auto reply")]
        log = await storage.fetchall("SELECT dialog_id, reply, dry_run FROM ghostwrite_log")
        assert log == [(DIALOG, "auto reply", 0)]
    finally:
        await storage.close()


async def test_max_per_hour_skips_and_warns(tmp_path, caplog):
    engine, client, suggester, storage, t = _mk_engine(
        tmp_path, enforce=True, max_per_hour=2,
    )
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        with caplog.at_level(logging.WARNING, logger="tg_messenger.agent.ghostwrite"):
            for i in range(3):
                t["now"] = float(i)
                await engine.process_incoming(_imsg(msg_id=i + 1))
        # only the first 2 within the hour are sent; the 3rd is skipped
        assert client.sent == [(DIALOG, "auto reply"), (DIALOG, "auto reply")]
        assert any("per-hour" in r.message or "rate" in r.message.lower()
                   for r in caplog.records)
    finally:
        await storage.close()


async def test_rate_window_slides_after_an_hour(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(
        tmp_path, enforce=True, max_per_hour=1,
    )
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        t["now"] = 0.0
        await engine.process_incoming(_imsg(msg_id=1))
        t["now"] = 4000.0  # >3600s later — the old send slid out of the window
        await engine.process_incoming(_imsg(msg_id=2))
        assert client.sent == [(DIALOG, "auto reply"), (DIALOG, "auto reply")]
    finally:
        await storage.close()


async def test_rate_window_not_bounded_by_own_sent_cache(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(
        tmp_path, enforce=True, max_per_hour=1, own_cache_size=1,
    )
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        await enable_dialog(storage, OTHER)
        t["now"] = 0.0
        await engine.process_incoming(_imsg(dialog_id=DIALOG, msg_id=1))
        t["now"] = 1.0
        await engine.process_incoming(_imsg(dialog_id=OTHER, msg_id=2))
        t["now"] = 2.0
        await engine.process_incoming(_imsg(dialog_id=DIALOG, msg_id=3))
        assert client.sent == [(DIALOG, "auto reply"), (OTHER, "auto reply")]
    finally:
        await storage.close()


async def test_not_active_sender_skipped(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        # message from a dialog we never enabled
        await engine.process_incoming(_imsg(dialog_id=OTHER, sender_id=OTHER))
        assert suggester.calls == []
        assert client.sent == []
    finally:
        await storage.close()


async def test_dispatch_rechecks_pause_after_suggester_await(tmp_path, caplog):
    storage = Storage(tmp_path / "gw.db")
    register_ghostwrite_migrations(storage)

    class PausingSuggester:
        async def suggest(self, dialog_id: int) -> str:
            await pause_dialog(storage, dialog_id, paused_until=100.0)
            return "late reply"

    client = FakeGwClient()
    t = {"now": 0.0}
    engine = GhostwriteEngine(
        client, PausingSuggester(), storage, enforce=True, clock=lambda: t["now"],
    )
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        with caplog.at_level(logging.INFO, logger="tg_messenger.agent.ghostwrite"):
            await engine.process_incoming(_imsg())
        assert client.sent == []
        assert await storage.fetchall("SELECT * FROM ghostwrite_log") == []
        assert any("became inactive" in r.message for r in caplog.records)
    finally:
        await storage.close()


# --- Цикл D: авто-пауза при человеке -----------------------------------------------


async def test_human_outgoing_pauses_dialog(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        t["now"] = 0.0
        # человек написал из приложения — НЕ движок
        await engine.on_outgoing(OutgoingEvent(dialog_id=DIALOG, message=_omsg(msg_id=55)))
        # диалог на паузе → не активен сейчас, активен после pause_on_human_sec
        assert await is_active(storage, DIALOG, now=0.0) is False
        assert await is_active(storage, DIALOG, now=engine._pause_on_human_sec + 1) is True
    finally:
        await storage.close()


async def test_engine_own_send_does_not_pause(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        await engine.process_incoming(_imsg())           # engine sends → id remembered
        sent_id = client._next_id
        # listen_outgoing echoes the engine's OWN message — must NOT pause
        await engine.on_outgoing(
            OutgoingEvent(dialog_id=DIALOG, message=_omsg(msg_id=sent_id))
        )
        assert await is_active(storage, DIALOG, now=0.0) is True
    finally:
        await storage.close()


async def test_inflight_own_send_echo_does_not_pause(tmp_path):
    class EchoBeforeAckClient(FakeGwClient):
        def __init__(self):
            super().__init__()
            self.engine: GhostwriteEngine | None = None

        async def send_text(self, peer, text, reply_to=None):
            self._next_id += 1
            msg = _omsg(text=text, dialog_id=peer, msg_id=self._next_id)
            assert self.engine is not None
            await self.engine.on_outgoing(OutgoingEvent(dialog_id=peer, message=msg))
            self.sent.append((peer, text))
            return msg

    storage = Storage(tmp_path / "gw.db")
    register_ghostwrite_migrations(storage)
    client = EchoBeforeAckClient()
    t = {"now": 0.0}
    engine = GhostwriteEngine(
        client, FakeSuggester(), storage, enforce=True, clock=lambda: t["now"],
    )
    client.engine = engine
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        await engine.process_incoming(_imsg())
        assert client.sent == [(DIALOG, "auto reply")]
        assert await is_active(storage, DIALOG, now=0.0) is True
    finally:
        await storage.close()


async def test_outgoing_in_non_ghostwrite_dialog_ignored(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        # OTHER is not enabled — a human message there is irrelevant, no row created
        await engine.on_outgoing(OutgoingEvent(dialog_id=OTHER, message=_omsg(dialog_id=OTHER)))
        assert await list_enabled(storage) == []
    finally:
        await storage.close()


async def test_outgoing_uses_cached_enabled_dialogs(tmp_path, monkeypatch):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=True)
    calls = {"count": 0}

    async def fake_list_enabled(storage):
        calls["count"] += 1
        return [DIALOG]

    monkeypatch.setattr(gw, "list_enabled", fake_list_enabled)
    await storage.connect()
    try:
        await engine.on_outgoing(OutgoingEvent(dialog_id=OTHER, message=_omsg(dialog_id=OTHER, msg_id=1)))
        await engine.on_outgoing(OutgoingEvent(dialog_id=OTHER, message=_omsg(dialog_id=OTHER, msg_id=2)))
        assert calls["count"] == 1
    finally:
        await storage.close()


async def test_outgoing_refreshes_runtime_enabled_dialog_on_cache_miss(tmp_path):
    engine, client, suggester, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        assert await engine._ensure_enabled_dialogs() == {DIALOG}
        await enable_dialog(storage, OTHER)
        t["now"] = 0.0
        await engine.on_outgoing(
            OutgoingEvent(dialog_id=OTHER, message=_omsg(dialog_id=OTHER, msg_id=5))
        )
        assert OTHER in await engine._ensure_enabled_dialogs()
        assert await is_active(storage, OTHER, now=0.0) is False
        assert await is_active(storage, OTHER, now=engine._pause_on_human_sec + 1) is True
    finally:
        await storage.close()


# --- Цикл E: деградация/ошибки -----------------------------------------------------


async def test_suggester_error_keeps_engine_alive(tmp_path, caplog):
    suggester = FakeSuggester()
    suggester.raise_on = DIALOG
    engine, client, _s, storage, t = _mk_engine(tmp_path, enforce=True, suggester=suggester)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        with caplog.at_level(logging.ERROR, logger="tg_messenger.agent.ghostwrite"):
            await engine.process_incoming(_imsg())  # must not raise
        assert client.sent == []
        assert any(r.exc_info for r in caplog.records)
    finally:
        await storage.close()


async def test_empty_suggestion_not_sent(tmp_path):
    suggester = FakeSuggester(reply="   ")  # whitespace-only → treated as empty
    engine, client, _s, storage, t = _mk_engine(tmp_path, enforce=True, suggester=suggester)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        await engine.process_incoming(_imsg())
        assert client.sent == []
        log = await storage.fetchall("SELECT * FROM ghostwrite_log")
        assert log == []  # nothing journalled for an empty draft
    finally:
        await storage.close()


# --- run(): gather, not TaskGroup (Ctrl+C clean) ----------------------------------


async def test_run_fans_out_both_consumers(tmp_path):
    """run() fans out listen() (auto-reply) + listen_outgoing() (auto-pause) via gather.

    Both streams end (finite generators) so run() returns; this asserts the wiring
    without racing a human pause against the incoming auto-reply.
    """

    class StreamClient(FakeGwClient):
        def __init__(self):
            super().__init__()
            self.outgoing_seen = False

        async def listen(self):
            yield IncomingEvent(dialog_id=DIALOG, message=_imsg())

        async def listen_outgoing(self):
            self.outgoing_seen = True
            yield OutgoingEvent(dialog_id=OTHER, message=_omsg(dialog_id=OTHER, msg_id=999))

    storage = Storage(tmp_path / "gw.db")
    register_ghostwrite_migrations(storage)
    await storage.connect()
    try:
        await enable_dialog(storage, DIALOG)
        client = StreamClient()
        engine = GhostwriteEngine(client, FakeSuggester(), storage, enforce=True)
        await engine.run()
        # the incoming message was auto-replied to
        assert client.sent == [(DIALOG, "auto reply")]
        assert client.outgoing_seen is True
    finally:
        await storage.close()
