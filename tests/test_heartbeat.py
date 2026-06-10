"""HeartbeatService (#19) — пинги по расписанию с шаблонами и предохранителями.

CORE-сервис над client+storage (DeletionWatcher/Moderation паттерн): для каждого
включённого плана раз в N часов отправляет случайный шаблон (или текст от инжектируемого
text_provider) с jitter, уважая quiet hours, max_per_day и предохранитель
«наше последнее сообщение без ответа». БЕЗ LLM-стека (text_provider — обычный Callable),
поэтому importorskip НЕ нужен — зелёно на голом [dev]. clock И rng инжектятся, тесты БЕЗ
реального sleep и детерминированы (filterwarnings=error).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from tg_messenger.core.heartbeat import (
    HeartbeatPlan,
    HeartbeatService,
    add_plan,
    is_due,
    list_enabled_plans,
    list_plans,
    register_heartbeat_migrations,
    remove_plan,
)
from tg_messenger.core.models import Message
from tg_messenger.core.storage import Storage

PEER = 7
OTHER = 9


def _msg(text="hi", *, out=False, dialog_id=PEER, msg_id=1):
    return Message(
        id=msg_id, dialog_id=dialog_id, sender_id=(1 if out else dialog_id), out=out,
        text=text, date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# --- цикл 100: storage слой ----------------------------------------------------------


async def _storage(tmp_path):
    storage = Storage(tmp_path / "hb.db")
    register_heartbeat_migrations(storage)
    await storage.connect()
    return storage


async def test_migration_creates_tables(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await storage.fetchall("SELECT * FROM heartbeat_plans")
        await storage.fetchall("SELECT * FROM heartbeat_log")
    finally:
        await storage.close()


async def test_add_list_remove_roundtrip(tmp_path):
    storage = await _storage(tmp_path)
    try:
        plan = HeartbeatPlan(
            peer=PEER, templates=["ping", "yo"], interval_hours=24.0,
            jitter_minutes=30.0, quiet_start=23, quiet_end=8, max_per_day=1,
        )
        await add_plan(storage, plan)
        plans = await list_plans(storage)
        assert len(plans) == 1
        got = plans[0]
        assert got.peer == PEER
        assert got.templates == ["ping", "yo"]
        assert got.interval_hours == 24.0
        assert got.jitter_minutes == 30.0
        assert got.quiet_start == 23
        assert got.quiet_end == 8
        assert got.max_per_day == 1
        assert got.enabled is True

        await remove_plan(storage, PEER)
        assert await list_plans(storage) == []
    finally:
        await storage.close()


async def test_add_plan_upsert(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["a"], interval_hours=1.0))
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["b"], interval_hours=2.0))
        plans = await list_plans(storage)
        assert len(plans) == 1
        assert plans[0].templates == ["b"]
        assert plans[0].interval_hours == 2.0
    finally:
        await storage.close()


async def test_list_enabled_filters_disabled(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["a"], interval_hours=1.0))
        await add_plan(storage, HeartbeatPlan(
            peer=OTHER, templates=["b"], interval_hours=1.0, enabled=False))
        enabled = await list_enabled_plans(storage)
        assert [p.peer for p in enabled] == [PEER]
    finally:
        await storage.close()


# --- цикл 101: расчёт «пора» (is_due) -------------------------------------------------
#
# now — Unix-таймстамп (стенные часы). quiet_start/quiet_end — локальные часы;
# is_due использует datetime.fromtimestamp(now) (локальная TZ) для проверки quiet-окна.


def _plan(**kw):
    base = dict(peer=PEER, templates=["ping"], interval_hours=24.0, jitter_minutes=0.0)
    base.update(kw)
    return HeartbeatPlan(**base)


def test_due_when_never_pinged():
    plan = _plan(last_ping_at=None)
    assert is_due(plan, now=1000.0, rng=lambda a, b: 0.0) is True


def test_not_due_before_interval():
    # last ping 1h ago, interval 24h → not due
    last = 1000.0
    now = last + 3600.0
    plan = _plan(last_ping_at=last)
    assert is_due(plan, now=now, rng=lambda a, b: 0.0) is False


def test_due_after_interval():
    last = 1000.0
    now = last + 24 * 3600.0 + 1
    plan = _plan(last_ping_at=last)
    assert is_due(plan, now=now, rng=lambda a, b: 0.0) is True


def test_jitter_pushes_due_later_deterministic():
    # interval 24h, jitter 60min; rng returns the max (+60min) → not yet due at +24h
    last = 1000.0
    now = last + 24 * 3600.0 + 1
    plan = _plan(last_ping_at=last, jitter_minutes=60.0)
    assert is_due(plan, now=now, rng=lambda a, b: b) is False
    # but due once the jittered offset elapses
    now2 = last + 24 * 3600.0 + 60 * 60.0 + 1
    assert is_due(plan, now=now2, rng=lambda a, b: b) is True


def test_quiet_hours_block_overnight_window():
    # quiet 23:00–08:00; pick a now whose LOCAL hour is 2am → not due even if interval passed
    two_am = datetime(2024, 1, 2, 2, 0).timestamp()  # local naive → local 02:00
    plan = _plan(last_ping_at=None, quiet_start=23, quiet_end=8)
    assert is_due(plan, now=two_am, rng=lambda a, b: 0.0) is False
    # noon → outside quiet window → due
    noon = datetime(2024, 1, 2, 12, 0).timestamp()
    assert is_due(plan, now=noon, rng=lambda a, b: 0.0) is True


def test_max_per_day_exhausted_not_due():
    now = 1000.0
    plan = _plan(last_ping_at=None, max_per_day=2, pings_today=2, day_start=now - 100.0)
    assert is_due(plan, now=now, rng=lambda a, b: 0.0) is False
    # a fresh day (day_start older than 24h) resets the count
    plan2 = _plan(last_ping_at=None, max_per_day=2, pings_today=2, day_start=now - 25 * 3600.0)
    assert is_due(plan2, now=now, rng=lambda a, b: 0.0) is True


# --- сервис / предохранитель «наше без ответа» + run-цикл ----------------------------


class FakeHbClient:
    """Core-client stub: records sends, serves a canned history tail."""

    def __init__(self, history_tail=None):
        self.sent: list[tuple] = []
        # last message of the dialog (the safety reads history(peer, limit=1))
        self._history_tail = history_tail

    def set_history_tail(self, msg):
        self._history_tail = msg

    async def history(self, peer, limit=1):
        if self._history_tail is None:
            return []
        return [self._history_tail]

    async def send_text(self, peer, text, reply_to=None, schedule=None):
        self.sent.append((peer, text))
        return _msg(text=text, out=True, dialog_id=peer, msg_id=999)


def _mk_service(tmp_path, *, client=None, rng=None, clock=None, text_provider=None):
    storage = Storage(tmp_path / "hb.db")
    register_heartbeat_migrations(storage)
    client = client or FakeHbClient()
    t = {"now": 0.0}
    svc = HeartbeatService(
        client, storage,
        clock=clock or (lambda: t["now"]),
        rng=rng or (lambda a, b: 0.0),
        text_provider=text_provider,
    )
    return svc, client, storage, t


# --- цикл 102: предохранитель «наше последнее сообщение без ответа» ---


async def test_skip_when_last_message_is_ours(tmp_path, caplog):
    svc, client, storage, t = _mk_service(tmp_path)
    await storage.connect()
    try:
        client.set_history_tail(_msg("our ping", out=True))
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["ping"], interval_hours=24.0))
        with caplog.at_level(logging.INFO):
            await svc.process_plans(now=t["now"])
        assert client.sent == []
        assert any("without a reply" in r.message or "без ответа" in r.message
                   for r in caplog.records)
    finally:
        await storage.close()


async def test_send_when_last_message_is_incoming(tmp_path):
    svc, client, storage, t = _mk_service(tmp_path)
    await storage.connect()
    try:
        client.set_history_tail(_msg("their reply", out=False))
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["ping"], interval_hours=24.0))
        await svc.process_plans(now=t["now"])
        assert client.sent == [(PEER, "ping")]
    finally:
        await storage.close()


async def test_send_when_no_history(tmp_path):
    # empty history (never talked) → safety doesn't block
    svc, client, storage, t = _mk_service(tmp_path)
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["ping"], interval_hours=24.0))
        await svc.process_plans(now=t["now"])
        assert client.sent == [(PEER, "ping")]
    finally:
        await storage.close()


# --- цикл 103: run-цикл / шаблоны / text_provider ---


async def test_template_chosen_via_rng(tmp_path):
    # rng returns index 1 → second template
    svc, client, storage, t = _mk_service(tmp_path, rng=lambda a, b: 1)
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(
            peer=PEER, templates=["first", "second"], interval_hours=24.0))
        await svc.process_plans(now=t["now"])
        assert client.sent == [(PEER, "second")]
    finally:
        await storage.close()


async def test_text_provider_overrides_template(tmp_path):
    async def provider(peer):
        return f"hi #{peer}"

    svc, client, storage, t = _mk_service(tmp_path, text_provider=provider)
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["tpl"], interval_hours=24.0))
        await svc.process_plans(now=t["now"])
        assert client.sent == [(PEER, "hi #7")]
    finally:
        await storage.close()


async def test_text_provider_error_falls_back_to_template(tmp_path, caplog):
    async def boom(peer):
        raise RuntimeError("provider down")

    svc, client, storage, t = _mk_service(tmp_path, text_provider=boom)
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["tpl"], interval_hours=24.0))
        with caplog.at_level(logging.WARNING):
            await svc.process_plans(now=t["now"])
        assert client.sent == [(PEER, "tpl")]
    finally:
        await storage.close()


async def test_send_error_keeps_loop_alive(tmp_path, caplog):
    class BoomClient(FakeHbClient):
        async def send_text(self, peer, text, reply_to=None, schedule=None):
            raise RuntimeError("network down")

    svc, client, storage, t = _mk_service(tmp_path, client=BoomClient())
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["a"], interval_hours=24.0))
        await add_plan(storage, HeartbeatPlan(peer=OTHER, templates=["b"], interval_hours=24.0))
        with caplog.at_level(logging.ERROR):
            await svc.process_plans(now=t["now"])  # must not raise
        assert any("heartbeat" in r.message.lower() for r in caplog.records)
    finally:
        await storage.close()


async def test_tick_updates_last_ping_and_count(tmp_path):
    svc, client, storage, t = _mk_service(tmp_path)
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(
            peer=PEER, templates=["ping"], interval_hours=24.0, max_per_day=5))
        await svc.process_plans(now=1000.0)
        plan = (await list_plans(storage))[0]
        assert plan.last_ping_at == 1000.0
        assert plan.pings_today == 1
        assert plan.day_start == 1000.0
        # immediately ticking again → not due (interval not elapsed)
        await svc.process_plans(now=1100.0)
        assert client.sent == [(PEER, "ping")]
    finally:
        await storage.close()


async def test_series_of_ticks_sends_on_each_interval(tmp_path):
    svc, client, storage, t = _mk_service(tmp_path)
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(
            peer=PEER, templates=["ping"], interval_hours=24.0, max_per_day=10))
        day = 24 * 3600.0
        await svc.process_plans(now=0.0)
        await svc.process_plans(now=day + 1)
        await svc.process_plans(now=2 * day + 2)
        assert client.sent == [(PEER, "ping")] * 3
    finally:
        await storage.close()


async def test_journal_records_sent_ping(tmp_path):
    svc, client, storage, t = _mk_service(tmp_path)
    await storage.connect()
    try:
        await add_plan(storage, HeartbeatPlan(peer=PEER, templates=["ping"], interval_hours=24.0))
        await svc.process_plans(now=1000.0)
        rows = await storage.fetchall("SELECT peer, text FROM heartbeat_log")
        assert rows == [(PEER, "ping")]
    finally:
        await storage.close()
