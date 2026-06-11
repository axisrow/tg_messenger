"""GhostwriteEngine (#18) — auto-replies in the owner's style in EXPLICITLY enabled DMs.

The second stage of the suggester (#17): where ``tg-messenger suggest`` drafts text for a
human to review, ``tg-messenger ghostwrite`` actually SENDS it — so it is destructive and
hedged with hard safeties. ModerationEngine/DeletionWatcher pattern:

- ``run()`` fans out two consumers via ``asyncio.gather`` (NOT TaskGroup — keeps Ctrl+C
  clean): ``listen()`` (incoming DMs) drives the auto-reply; ``listen_outgoing()`` detects a
  human stepping in and auto-pauses the dialog.
- Like #17/#16 this module NEVER imports the LLM stack — the ``Suggester`` is injected (it,
  in turn, has ``suggest_fn`` injected; the single ``init_chat_model`` import lives in
  ``factory.py``). So the engine and its storage are fully testable on a bare ``[dev]``
  install — no langchain, no network.

Safeties (the heart of the issue):
1. **Per-dialog allowlist** in SQLite (``ghostwrite_dialogs``) — separate from the agent's
   ``TG_AGENT_ALLOWLIST``. ``*`` ("everyone") is forbidden by design (enforced at the CLI).
2. **Per-dialog hourly rate limit** (``max_per_hour``, sliding in-memory window, injected
   ``clock``) — exceeded → skip + warning, nothing sent.
3. **``pause-all``** kill switch (``paused_until`` = far future on every enabled dialog).
4. **Auto-pause on human intervention** — see ``on_outgoing``.
5. **dry-run by default** (``enforce=False``): "would …" log instead of a send.
6. **Journal** of every auto-reply in ``ghostwrite_log``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timezone

from tg_messenger.core.models import Message, OutgoingEvent

logger = logging.getLogger(__name__)

# pause-all / kill switch: a paused_until so far ahead no realistic clock un-pauses it.
PAUSE_FOREVER = 1e19

DEFAULT_MAX_PER_HOUR = 6
DEFAULT_PAUSE_ON_HUMAN_SEC = 86_400  # a human reply pauses the dialog for a day
RATE_WINDOW_SEC = 3600.0
DEFAULT_OWN_CACHE_SIZE = 1000
DEFAULT_MAX_RATE_DIALOGS = 10_000


# --- storage (#13): per-dialog allowlist + auto-reply journal --------------------

# Registered via storage.register_migrations BEFORE connect(); engine and CLI share the
# schema. dialog_id is the PK so enable_dialog is an upsert. paused_until is a
# Unix timestamp from time.time() (NULL = not paused).
GHOSTWRITE_MIGRATIONS = [
    "CREATE TABLE ghostwrite_dialogs ("
    " dialog_id INTEGER PRIMARY KEY,"
    " enabled INTEGER NOT NULL,"
    " paused_until REAL)",
    "CREATE TABLE ghostwrite_log ("
    " dialog_id INTEGER NOT NULL,"
    " message_id INTEGER,"
    " reply TEXT NOT NULL,"
    " dry_run INTEGER NOT NULL,"
    " ts TEXT NOT NULL)",
]


def register_ghostwrite_migrations(storage) -> None:
    """Register the ghostwrite tables on a Storage (call before ``connect()``)."""
    storage.register_migrations(GHOSTWRITE_MIGRATIONS)


async def enable_dialog(storage, dialog_id: int) -> None:
    """Turn ghostwrite ON for a dialog (upsert; clears any existing pause)."""
    await storage.execute(
        "INSERT INTO ghostwrite_dialogs (dialog_id, enabled, paused_until) "
        "VALUES (?, 1, NULL) "
        "ON CONFLICT(dialog_id) DO UPDATE SET enabled = 1, paused_until = NULL",
        (int(dialog_id),),
    )


async def disable_dialog(storage, dialog_id: int) -> None:
    """Turn ghostwrite OFF for a dialog (row kept, enabled = 0)."""
    await storage.execute(
        "INSERT INTO ghostwrite_dialogs (dialog_id, enabled, paused_until) "
        "VALUES (?, 0, NULL) "
        "ON CONFLICT(dialog_id) DO UPDATE SET enabled = 0",
        (int(dialog_id),),
    )


async def list_enabled(storage) -> list[int]:
    """All dialog ids where ghostwrite is enabled (ordered for determinism)."""
    rows = await storage.fetchall(
        "SELECT dialog_id FROM ghostwrite_dialogs WHERE enabled = 1 ORDER BY dialog_id"
    )
    return [row[0] for row in rows]


async def pause_dialog(storage, dialog_id: int, *, paused_until: float) -> None:
    """Pause an (enabled) dialog until ``paused_until`` on the engine's clock."""
    await storage.execute(
        "UPDATE ghostwrite_dialogs SET paused_until = ? WHERE dialog_id = ?",
        (float(paused_until), int(dialog_id)),
    )


async def resume_dialog(storage, dialog_id: int) -> None:
    """Clear a dialog's pause (no-op on its enabled flag)."""
    await storage.execute(
        "UPDATE ghostwrite_dialogs SET paused_until = NULL WHERE dialog_id = ?",
        (int(dialog_id),),
    )


async def pause_all(storage) -> None:
    """Kill switch: pause every enabled dialog far into the future."""
    await storage.execute(
        "UPDATE ghostwrite_dialogs SET paused_until = ? WHERE enabled = 1",
        (PAUSE_FOREVER,),
    )


async def is_active(storage, dialog_id: int, *, now: float) -> bool:
    """True if ghostwrite is enabled for ``dialog_id`` and not currently paused."""
    row = await storage.fetchone(
        "SELECT enabled, paused_until FROM ghostwrite_dialogs WHERE dialog_id = ?",
        (int(dialog_id),),
    )
    if row is None or not row[0]:
        return False
    paused_until = row[1]
    return paused_until is None or now >= paused_until


# --- GhostwriteEngine (#18) ------------------------------------------------------


class GhostwriteEngine:
    """Auto-reply in the owner's style to incoming DMs from explicitly enabled dialogs.

    ModerationEngine pattern: ``run()`` fans out via ``asyncio.gather`` (NOT TaskGroup —
    Ctrl+C). Destructive (sends from your account) so **dry-run is the default** — a reply
    fires only under ``enforce=True``; otherwise the engine logs "would …" and records
    ``dry_run=1``. A per-dialog hourly window (bounded OrderedDict, injected ``clock``)
    caps the rate; a bounded cache of the engine's own sent ids lets ``on_outgoing`` tell
    the engine's replies apart from a human stepping in.
    """

    def __init__(
        self,
        client,
        suggester,
        storage,
        *,
        enforce: bool = False,
        clock: Callable[[], float] = time.time,
        max_per_hour: int = DEFAULT_MAX_PER_HOUR,
        pause_on_human_sec: int = DEFAULT_PAUSE_ON_HUMAN_SEC,
        own_cache_size: int = DEFAULT_OWN_CACHE_SIZE,
        max_rate_dialogs: int = DEFAULT_MAX_RATE_DIALOGS,
    ):
        self._client = client
        self._suggester = suggester
        self._storage = storage
        self._enforce = enforce
        self._clock = clock
        self._max_per_hour = max_per_hour
        self._pause_on_human_sec = pause_on_human_sec
        self._own_cache_size = own_cache_size
        self._max_rate_dialogs = max_rate_dialogs
        self._enabled_dialogs: set[int] | None = None
        self._sending: defaultdict[int, list[str]] = defaultdict(list)
        # dialog_id -> sliding window of recent auto-reply times (bounded)
        self._rate: OrderedDict[int, deque[float]] = OrderedDict()
        # (dialog_id, message_id) we sent ourselves — recognise our own outgoing echo.
        # the value is an unused sentinel True (NOT None — pop(key, None) must distinguish
        # a present own-id from a miss).
        self._own_sent: OrderedDict[tuple[int, int], bool] = OrderedDict()

    async def run(self) -> None:
        # gather, не TaskGroup: TaskGroup оборачивает KeyboardInterrupt
        # в BaseExceptionGroup и ломает Ctrl+C-обработку в CLI
        await self._ensure_enabled_dialogs()
        await asyncio.gather(self._consume_incoming(), self._consume_outgoing())

    async def _ensure_enabled_dialogs(self) -> set[int]:
        if self._enabled_dialogs is None:
            self._enabled_dialogs = set(await list_enabled(self._storage))
        return self._enabled_dialogs

    async def _is_enabled_dialog(self, dialog_id: int) -> bool:
        enabled = await self._ensure_enabled_dialogs()
        if dialog_id in enabled:
            return True
        row = await self._storage.fetchone(
            "SELECT 1 FROM ghostwrite_dialogs WHERE dialog_id = ? AND enabled = 1",
            (int(dialog_id),),
        )
        if row is None:
            return False
        enabled.add(int(dialog_id))
        return True

    async def _consume_incoming(self) -> None:
        async for ev in self._client.listen():
            try:
                await self.process_incoming(ev.message)
            except Exception:
                logger.exception(
                    "ghostwrite failed to process message in dialog %s",
                    getattr(ev, "dialog_id", "?"),
                )

    async def _consume_outgoing(self) -> None:
        async for ev in self._client.listen_outgoing():
            try:
                await self.on_outgoing(ev)
            except Exception:
                logger.exception(
                    "ghostwrite failed to handle outgoing message in dialog %s",
                    getattr(ev, "dialog_id", "?"),
                )

    async def process_incoming(self, message: Message) -> None:
        """Auto-reply to one incoming message if its dialog is active and within rate."""
        if message.out:
            return  # defence in depth: listen() is incoming-only, but never reply to self
        dialog_id = message.dialog_id
        now = self._clock()
        if not await is_active(self._storage, dialog_id, now=now):
            return  # not enabled, or paused — the suggester is never even asked
        if self._over_rate_limit(dialog_id, now):
            logger.warning(
                "ghostwrite skip in dialog %s: per-hour limit (%s) reached",
                dialog_id, self._max_per_hour,
            )
            return
        try:
            reply = await self._suggester.suggest(dialog_id)
        except Exception:
            logger.exception("ghostwrite suggester failed for dialog %s", dialog_id)
            return  # engine survives — the loop keeps going
        if not (reply or "").strip():
            logger.info("ghostwrite skip in dialog %s: empty draft", dialog_id)
            return
        await self._dispatch(dialog_id, message, reply)

    def _over_rate_limit(self, dialog_id: int, now: float) -> bool:
        """Record an attempt, then report if the trailing hour already hit ``max_per_hour``.

        Called BEFORE the suggester so a maxed-out dialog never costs an LLM call. The
        attempt is recorded only while the dialog is under the limit.
        """
        window = self._rate.get(dialog_id)
        if window is None:
            window = deque()
            self._rate[dialog_id] = window
        self._rate.move_to_end(dialog_id)
        while window and now - window[0] > RATE_WINDOW_SEC:
            window.popleft()
        while len(self._rate) > self._max_rate_dialogs:
            self._rate.popitem(last=False)
        if len(window) >= self._max_per_hour:
            return True
        window.append(now)
        return False

    async def _dispatch(self, dialog_id: int, message: Message, reply: str) -> None:
        """Send (or, in dry-run, skip) one auto-reply; journal it; never kill the engine."""
        if not await is_active(self._storage, dialog_id, now=self._clock()):
            logger.info("ghostwrite skip dialog %s: became inactive while drafting", dialog_id)
            return
        if not self._enforce:
            logger.info("would auto-reply in dialog %s (message %s): %r",
                        dialog_id, message.id, reply)
            await self._journal(dialog_id, message.id, reply, dry_run=True)
            return
        try:
            self._sending[int(dialog_id)].append(reply)
            try:
                sent = await self._client.send_text(dialog_id, reply)
                self._remember_own(dialog_id, getattr(sent, "id", None))
            finally:
                pending = self._sending.get(int(dialog_id), [])
                if reply in pending:
                    pending.remove(reply)
                if not pending:
                    self._sending.pop(int(dialog_id), None)
        except Exception:
            logger.exception("ghostwrite failed to send reply in dialog %s", dialog_id)
            return
        await self._journal(dialog_id, message.id, reply, dry_run=False)

    def _remember_own(self, dialog_id: int, message_id) -> None:
        if message_id is None:
            return
        key = (int(dialog_id), int(message_id))
        self._own_sent[key] = True
        self._own_sent.move_to_end(key)
        while len(self._own_sent) > self._own_cache_size:
            self._own_sent.popitem(last=False)

    async def on_outgoing(self, event: OutgoingEvent) -> None:
        """A human stepped in: pause the dialog so the engine stops talking over them.

        ``listen_outgoing`` also echoes the engine's OWN sends — those are matched against
        the bounded own-id cache (DeletionWatcher pattern) and must NOT trigger a pause.
        Outgoing in a non-ghostwrite dialog is irrelevant and ignored.
        """
        dialog_id = event.dialog_id
        message = event.message
        key = (int(dialog_id), int(message.id))
        pending_sends = self._sending.get(int(dialog_id), [])
        if message.text in pending_sends:
            return  # our own in-flight send — not a human
        if self._own_sent.pop(key, None) is not None:
            return  # our own reply — not a human
        if not await self._is_enabled_dialog(dialog_id):
            return  # not a ghostwrite dialog — nothing to pause
        now = self._clock()
        if not await is_active(self._storage, dialog_id, now=now):
            return  # already paused/disabled — do not shorten an existing pause
        paused_until = now + self._pause_on_human_sec
        await pause_dialog(self._storage, dialog_id, paused_until=paused_until)
        logger.info(
            "ghostwrite paused dialog %s for %ss — a human replied",
            dialog_id, self._pause_on_human_sec,
        )

    async def _journal(self, dialog_id: int, message_id: int, reply: str,
                       *, dry_run: bool) -> None:
        try:
            await self._storage.execute(
                "INSERT INTO ghostwrite_log (dialog_id, message_id, reply, dry_run, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(dialog_id), int(message_id), reply, int(dry_run),
                 datetime.now(timezone.utc).isoformat()),
            )
        except Exception:
            logger.exception("failed to write ghostwrite log entry")
