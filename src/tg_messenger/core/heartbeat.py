"""HeartbeatService (#19) — пинги по расписанию с шаблонами и предохранителями.

A CORE service above the client (DeletionWatcher / ModerationEngine pattern): for every
enabled plan it sends, once every ``interval_hours`` (plus a random ``jitter_minutes``), a
randomly chosen template — or, if an (injected) ``text_provider`` is given, its text — to a
peer, journaling each ping. It NEVER imports the LLM stack: ``text_provider`` is a plain
``Callable`` (the суфлёр hook), so the whole module is testable on a bare ``[dev]`` install.

``run()`` fans out one consumer via ``asyncio.gather`` (NOT TaskGroup — keeps Ctrl+C clean):
a slow tick loop that calls :meth:`process_plans` once a minute. The single tick is exposed
as a method so tests drive it directly with an injected ``clock`` — никакого реального sleep.

Safeties (all mandatory):
1. **interval_hours + jitter_minutes** — never ping more often than scheduled; jitter
   (``rng`` injected, deterministic in tests) spreads pings so they don't look robotic.
2. **quiet hours** (``quiet_start``/``quiet_end``, LOCAL hours) — no ping inside the window.
3. **max_per_day** — a daily cap; ``pings_today`` resets when ``day_start`` is over 24h old.
4. **«наше последнее сообщение без ответа»** — if the dialog's last message is OURS
   (``out=True``), the собеседник hasn't replied yet, so we skip (don't nag).

clock И rng инжектятся; every network call goes through the core client (flood-retry free).

TZ-допущение: ``now`` (a Unix timestamp on the wall clock) is mapped to a LOCAL hour via
``datetime.fromtimestamp(now)`` for the quiet-hours check; ``quiet_start``/``quiet_end`` are
local-time hours (0–23).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_JITTER_MINUTES = 0.0
DEFAULT_MAX_PER_DAY = 1
TICK_INTERVAL_SEC = 60.0
DAY_SEC = 24 * 3600.0


# --- модель плана --------------------------------------------------------------------


class HeartbeatPlan(BaseModel):
    """A per-peer scheduled-ping plan (one row of ``heartbeat_plans``)."""

    peer: int
    templates: list[str]
    interval_hours: float = DEFAULT_INTERVAL_HOURS
    jitter_minutes: float = DEFAULT_JITTER_MINUTES
    quiet_start: int | None = None  # local hour [0..23] the quiet window opens
    quiet_end: int | None = None    # local hour [0..23] the quiet window closes
    enabled: bool = True
    last_ping_at: float | None = None
    max_per_day: int = DEFAULT_MAX_PER_DAY
    pings_today: int = 0
    day_start: float | None = None  # Unix ts the current daily counter started

    @field_validator("templates")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("a heartbeat plan needs at least one template")
        return v


# --- storage (#13): планы + журнал ---------------------------------------------------

# Registered via storage.register_migrations BEFORE connect(); service and CLI share the
# schema. peer is the PK so add_plan is an upsert. templates is a JSON list.
HEARTBEAT_MIGRATIONS = [
    "CREATE TABLE heartbeat_plans ("
    " peer INTEGER PRIMARY KEY,"
    " templates TEXT NOT NULL,"
    " interval_hours REAL NOT NULL,"
    " jitter_minutes REAL NOT NULL,"
    " quiet_start INTEGER,"
    " quiet_end INTEGER,"
    " enabled INTEGER NOT NULL,"
    " last_ping_at REAL,"
    " max_per_day INTEGER NOT NULL,"
    " pings_today INTEGER NOT NULL,"
    " day_start REAL)",
    "CREATE TABLE heartbeat_log ("
    " peer INTEGER NOT NULL,"
    " text TEXT NOT NULL,"
    " ts TEXT NOT NULL)",
]


def register_heartbeat_migrations(storage) -> None:
    """Register the heartbeat tables on a Storage (call before ``connect()``)."""
    storage.register_migrations(HEARTBEAT_MIGRATIONS)


_COLUMNS = (
    "peer, templates, interval_hours, jitter_minutes, quiet_start, quiet_end, "
    "enabled, last_ping_at, max_per_day, pings_today, day_start"
)


def _row_to_plan(row) -> HeartbeatPlan:
    return HeartbeatPlan(
        peer=row[0],
        templates=json.loads(row[1]),
        interval_hours=row[2],
        jitter_minutes=row[3],
        quiet_start=row[4],
        quiet_end=row[5],
        enabled=bool(row[6]),
        last_ping_at=row[7],
        max_per_day=row[8],
        pings_today=row[9],
        day_start=row[10],
    )


async def add_plan(storage, plan: HeartbeatPlan) -> None:
    """Insert/replace a plan (upsert on the ``peer`` primary key)."""
    await storage.execute(
        "INSERT INTO heartbeat_plans "
        "(peer, templates, interval_hours, jitter_minutes, quiet_start, quiet_end, "
        " enabled, last_ping_at, max_per_day, pings_today, day_start) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(peer) DO UPDATE SET "
        "templates = excluded.templates, interval_hours = excluded.interval_hours, "
        "jitter_minutes = excluded.jitter_minutes, quiet_start = excluded.quiet_start, "
        "quiet_end = excluded.quiet_end, enabled = excluded.enabled, "
        "last_ping_at = excluded.last_ping_at, max_per_day = excluded.max_per_day, "
        "pings_today = excluded.pings_today, day_start = excluded.day_start",
        (
            plan.peer,
            json.dumps(plan.templates),
            plan.interval_hours,
            plan.jitter_minutes,
            plan.quiet_start,
            plan.quiet_end,
            int(plan.enabled),
            plan.last_ping_at,
            plan.max_per_day,
            plan.pings_today,
            plan.day_start,
        ),
    )


async def list_plans(storage) -> list[HeartbeatPlan]:
    """All plans, ordered by peer for determinism."""
    rows = await storage.fetchall(
        f"SELECT {_COLUMNS} FROM heartbeat_plans ORDER BY peer"
    )
    return [_row_to_plan(row) for row in rows]


async def list_enabled_plans(storage) -> list[HeartbeatPlan]:
    """Only enabled plans, ordered by peer."""
    rows = await storage.fetchall(
        f"SELECT {_COLUMNS} FROM heartbeat_plans WHERE enabled = 1 ORDER BY peer"
    )
    return [_row_to_plan(row) for row in rows]


async def remove_plan(storage, peer: int) -> None:
    await storage.execute("DELETE FROM heartbeat_plans WHERE peer = ?", (int(peer),))


# --- расчёт «пора» (чистая функция) --------------------------------------------------


def _in_quiet_window(local_hour: int, start: int, end: int) -> bool:
    """True if ``local_hour`` falls in the [start, end) quiet window (wraps midnight)."""
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    # overnight window, e.g. 23 → 8
    return local_hour >= start or local_hour < end


def is_due(plan: HeartbeatPlan, *, now: float, rng: Callable[[float, float], float]) -> bool:
    """Whether ``plan`` should ping at ``now`` (Unix ts), honouring every safety.

    ``rng(a, b)`` is injected (deterministic in tests). It is consulted for the jitter
    offset only — quiet hours / max_per_day are pure comparisons.
    """
    # quiet hours (local time)
    if plan.quiet_start is not None and plan.quiet_end is not None:
        local_hour = datetime.fromtimestamp(now).hour
        if _in_quiet_window(local_hour, plan.quiet_start, plan.quiet_end):
            return False

    # max_per_day — resets when the daily window is older than 24h
    if plan.day_start is not None and (now - plan.day_start) < DAY_SEC:
        if plan.pings_today >= plan.max_per_day:
            return False

    # interval + jitter
    if plan.last_ping_at is None:
        return True
    jitter_sec = 0.0
    if plan.jitter_minutes > 0:
        jitter_sec = rng(0.0, plan.jitter_minutes * 60.0)
    due_at = plan.last_ping_at + plan.interval_hours * 3600.0 + jitter_sec
    return now >= due_at


# --- HeartbeatService ----------------------------------------------------------------


class HeartbeatService:
    """Scheduled-ping service over the core client + storage.

    ``run()`` is a thin loop (``asyncio.gather`` of one ticker, NOT TaskGroup — Ctrl+C)
    that calls :meth:`process_plans` once a minute. The tick is its own method so tests
    drive it with an injected ``clock`` — никакого реального sleep. ``rng`` (jitter +
    template choice) is injected too, so a tick is fully deterministic in tests.

    ``text_provider`` (optional) generates the ping text per peer (the суфлёр hook); its
    failure logs a warning and falls back to a random template — a heartbeat is never lost
    to a flaky provider.
    """

    def __init__(
        self,
        client,
        storage,
        *,
        clock: Callable[[], float] = time.time,
        rng: Callable[[float, float], float] | None = None,
        text_provider: Callable[[int], Awaitable[str]] | None = None,
    ):
        self._client = client
        self._storage = storage
        self._clock = clock
        self._rng = rng or random.uniform
        self._text_provider = text_provider

    async def run(self) -> None:
        # gather, не TaskGroup: TaskGroup оборачивает KeyboardInterrupt
        # в BaseExceptionGroup и ломает Ctrl+C-обработку в CLI
        await asyncio.gather(self._tick_loop())

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self.process_plans(now=self._clock())
            except Exception:
                logger.exception("heartbeat tick failed — loop continues")
            await asyncio.sleep(TICK_INTERVAL_SEC)

    async def process_plans(self, *, now: float) -> None:
        """One tick: send a ping for every enabled plan that's due and unblocked."""
        plans = await list_enabled_plans(self._storage)
        for plan in plans:
            try:
                await self._maybe_ping(plan, now=now)
            except Exception:
                logger.exception(
                    "heartbeat failed to process plan for peer %s — loop continues", plan.peer
                )

    async def _maybe_ping(self, plan: HeartbeatPlan, *, now: float) -> None:
        if not is_due(plan, now=now, rng=self._rng):
            return
        # safety: «наше последнее сообщение без ответа» — don't nag if they haven't replied
        if await self._last_is_ours(plan.peer):
            logger.info(
                "heartbeat skip peer %s: our last message is still without a reply", plan.peer
            )
            return
        text = await self._choose_text(plan)
        await self._client.send_text(plan.peer, text)
        await self._record_ping(plan, now=now, text=text)
        await self._journal(plan.peer, text)

    async def _last_is_ours(self, peer: int) -> bool:
        tail = await self._client.history(peer, limit=1)
        if not tail:
            return False
        return bool(getattr(tail[-1], "out", False))

    async def _choose_text(self, plan: HeartbeatPlan) -> str:
        if self._text_provider is not None:
            try:
                text = await self._text_provider(plan.peer)
                if (text or "").strip():
                    return text
                logger.warning(
                    "heartbeat text_provider returned empty for peer %s — using a template",
                    plan.peer,
                )
            except Exception:
                logger.warning(
                    "heartbeat text_provider failed for peer %s — falling back to a template",
                    plan.peer, exc_info=True,
                )
        # upper bound = len, int() truncates to 0..len-1 uniformly; min() guards
        # the rng returning the bound itself (uniform(0, n-1) + int() would give
        # the last template a ~zero share)
        count = len(plan.templates)
        idx = min(int(self._rng(0, count)), count - 1) if count > 1 else 0
        return plan.templates[idx]

    async def _record_ping(self, plan: HeartbeatPlan, *, now: float, text: str) -> None:
        # daily counter: reset if the window is over 24h old (or never started)
        if plan.day_start is None or (now - plan.day_start) >= DAY_SEC:
            day_start = now
            pings_today = 1
        else:
            day_start = plan.day_start
            pings_today = plan.pings_today + 1
        await self._storage.execute(
            "UPDATE heartbeat_plans SET last_ping_at = ?, pings_today = ?, day_start = ? "
            "WHERE peer = ?",
            (now, pings_today, day_start, plan.peer),
        )

    async def _journal(self, peer: int, text: str) -> None:
        try:
            await self._storage.execute(
                "INSERT INTO heartbeat_log (peer, text, ts) VALUES (?, ?, ?)",
                (int(peer), text, datetime.now(timezone.utc).isoformat()),
            )
        except Exception:
            logger.exception("failed to write heartbeat log entry")
