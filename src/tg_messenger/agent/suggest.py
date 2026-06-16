"""Suggester (#17) — a draft reply in the style of past conversations.

DELIBERATELY a human-in-the-loop draft only: it produces text for a person to
review/edit/send — full automation is a separate issue (#18 ghostwrite). This
module is part of the agent layer but, like the orchestrator, NEVER imports the
LLM stack: the model contact (``suggest_fn``) is injected (factory.py owns the
single ``init_chat_model`` import). So Suggester, the style profile and its
storage are all testable on a bare ``[dev]`` install — no langchain, no network.

The style profile is built from the contact's history with pure functions
(``build_style_profile``) and persisted per dialog in SQLite (#13). Learning is
an EXPLICIT per-peer command (``learn``), never a background scan — one history
pass on demand keeps it flood-safe.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from tg_messenger.core.models import Message, MessageReadEvent

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 30
MAX_PROFILE_EXAMPLES = 10

# a cheap emoji detector: most pictographic / symbol / transport / flag ranges.
# Good enough for an aggregate frequency — we don't need grapheme-perfect counts.
_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF),  # symbols & pictographs, emoticons, transport, supplemental
    (0x2600, 0x27BF),    # misc symbols + dingbats
    (0x2190, 0x21FF),    # arrows
    (0x1F1E6, 0x1F1FF),  # regional indicators (flags)
)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def _count_emoji(text: str) -> int:
    return sum(1 for ch in text if _is_emoji(ch))


class StyleProfile(BaseModel):
    """Aggregate stylistic fingerprint of OUR replies to one contact.

    Stored per dialog as JSON; injected into ``suggest_fn`` so the draft echoes
    the user's own voice. An empty history yields a zero/empty stub.
    """

    avg_length: float = 0.0
    emoji_freq: float = 0.0  # emoji chars per own message (mean)
    greetings: list[str] = []
    signatures: list[str] = []
    examples: list[str] = []  # up to 10 characteristic replies


class ContextMessage(BaseModel):
    """A history line handed to ``suggest_fn`` (own/peer marked by ``out``)."""

    out: bool
    text: str


# первые слова, по которым эвристика узнаёт приветствие/подпись в своих сообщениях
_GREETING_WORDS = frozenset({
    "hi", "hey", "hello", "yo", "привет", "здравствуй", "здравствуйте", "хай", "доброе",
})
_SIGNATURE_WORDS = frozenset({
    "bye", "cya", "later", "thanks", "thank", "пока", "спасибо", "до", "удачи",
})


def build_style_profile(messages: list[Message]) -> StyleProfile:
    """Build a :class:`StyleProfile` from a dialog history (chronological).

    Aggregates run over ALL own (``out=True``) messages; the ``examples`` are the
    subset of own replies that come immediately AFTER an incoming message (a real
    reply, not a monologue line). Greetings/signatures are a first/last-word
    heuristic. Empty history → a zero/empty stub.
    """
    own = [m for m in messages if m.out and (m.text or "").strip()]
    if not own:
        return StyleProfile()

    lengths = [len(m.text or "") for m in own]
    avg_length = sum(lengths) / len(own)
    emoji_freq = sum(_count_emoji(m.text or "") for m in own) / len(own)

    greetings: list[str] = []
    signatures: list[str] = []
    for m in own:
        words = (m.text or "").strip().split()
        if not words:
            continue
        first = words[0].lower().strip("!?.,")
        last = words[-1].lower().strip("!?.,")
        if first in _GREETING_WORDS and first not in greetings:
            greetings.append(first)
        if last in _SIGNATURE_WORDS and last not in signatures:
            signatures.append(last)

    # examples: own replies that directly follow an incoming message
    examples: list[str] = []
    for prev, cur in zip(messages, messages[1:]):
        if cur.out and not prev.out and (cur.text or "").strip():
            text = cur.text or ""
            if text not in examples:
                examples.append(text)
            if len(examples) >= MAX_PROFILE_EXAMPLES:
                break

    return StyleProfile(
        avg_length=avg_length,
        emoji_freq=emoji_freq,
        greetings=greetings,
        signatures=signatures,
        examples=examples,
    )


# --- storage (#13): per-dialog style profiles ------------------------------------

SUGGEST_MIGRATIONS = [
    "CREATE TABLE style_profiles ("
    " dialog_id INTEGER PRIMARY KEY,"
    " profile TEXT NOT NULL)",
]


def register_suggest_migrations(storage) -> None:
    """Register the suggester's table on a Storage (call BEFORE ``connect()``)."""
    storage.register_migrations(SUGGEST_MIGRATIONS)


async def save_style_profile(storage, dialog_id: int, profile: StyleProfile) -> None:
    """Insert/replace the style profile for a dialog (upsert on ``dialog_id``)."""
    await storage.execute(
        "INSERT INTO style_profiles (dialog_id, profile) VALUES (?, ?) "
        "ON CONFLICT(dialog_id) DO UPDATE SET profile = excluded.profile",
        (int(dialog_id), profile.model_dump_json()),
    )


async def load_style_profile(storage, dialog_id: int) -> StyleProfile | None:
    """Return the stored profile for a dialog, or None if there is none."""
    row = await storage.fetchone(
        "SELECT profile FROM style_profiles WHERE dialog_id = ?", (int(dialog_id),)
    )
    if row is None:
        return None
    return StyleProfile.model_validate_json(row[0])


# --- read receipts (#17, cycle 98 / #145 auto-nudge): "they've seen our message" --
#
# The OUTBOX receipt (the contact read our messages up to max_id) is persisted in
# the kv table together with WHEN it was read (#145), so the nudge logic can fire
# "they read it and went quiet for N hours" — still a DRAFT only (a human reviews
# and sends), consistent with the suggester's never-auto-send contract.

# default quiet window before a nudge is proposed (the contact read but didn't reply)
DEFAULT_NUDGE_AFTER_SEC = 24 * 3600


def last_read_key(dialog_id: int) -> str:
    return f"last_read_{int(dialog_id)}"


def read_at_key(dialog_id: int) -> str:
    return f"read_at_{int(dialog_id)}"


async def record_last_read(storage, event: MessageReadEvent, *, clock=None) -> None:
    """Persist an OUTBOX read receipt (they read our messages up to ``max_id``).

    Also stamps WHEN it was read (``read_at_<dialog>``) for the #145 nudge window.
    ``clock`` is injected (``time.time`` default) so tests never use real time.
    """
    if not event.outbox:
        return  # inbox = WE read theirs — not the signal the suggester cares about
    if clock is None:
        import time
        clock = time.time
    await storage.set_value(last_read_key(event.dialog_id), int(event.max_id))
    await storage.set_value(read_at_key(event.dialog_id), float(clock()))


async def load_last_read(storage, dialog_id: int) -> int | None:
    """Return the last ``max_id`` the contact has read of our messages, or None."""
    value = await storage.get_value(last_read_key(dialog_id))
    return int(value) if value is not None else None


async def load_read_at(storage, dialog_id: int) -> float | None:
    """Return WHEN the contact last read our messages (Unix seconds), or None."""
    value = await storage.get_value(read_at_key(dialog_id))
    return float(value) if value is not None else None


async def clear_last_read(storage, dialog_id: int) -> None:
    """Drop the stored outbox receipt for ``dialog_id`` (both kv keys).

    Used when the contact has replied so the aged receipt no longer warrants a
    nudge — keeping it would make ``nudge_candidates`` re-fetch history every run
    (flood discipline, #145). Safe: the keys are only re-stamped by a FRESH
    outbox receipt (``record_last_read``), which is exactly when nudging matters
    again.
    """
    # one statement so the two keys clear atomically (no half-cleared window)
    await storage.execute(
        "DELETE FROM kv WHERE key IN (?, ?)",
        (last_read_key(dialog_id), read_at_key(dialog_id)),
    )


async def watch_read_receipts(client, storage, *, clock=None) -> None:
    """Drain ``client.listen_reads()`` and record every outbox receipt.

    Best-effort and non-fatal: one bad event is logged and the loop continues.
    """
    async for event in client.listen_reads():
        try:
            await record_last_read(storage, event, clock=clock)
        except Exception:
            logger.exception("failed to record read receipt for dialog %s",
                             getattr(event, "dialog_id", "?"))


def should_nudge(
    history: list[Message],
    *,
    last_read_id: int | None,
    read_at: float | None,
    now: float,
    after_sec: float = DEFAULT_NUDGE_AFTER_SEC,
) -> bool:
    """Pure: does this dialog warrant a nudge draft right now?

    True only when ALL hold: the dialog's last message is OURS (``out=True`` — the
    contact hasn't replied), the contact has READ it (``last_read_id`` reaches that
    message's id), and the read happened at least ``after_sec`` ago. A None receipt
    or read time → no nudge (we only act on a confirmed, aged read).
    """
    if not history or last_read_id is None or read_at is None:
        return False
    last = history[-1]
    if not last.out:
        return False  # they replied (or spoke last) — nothing to nudge
    if last_read_id < last.id:
        return False  # our latest message hasn't been read yet
    return (now - read_at) >= after_sec


# --- Suggester (#17) -------------------------------------------------------------


class Suggester:
    """Produce a draft reply for a dialog (human reviews it; never auto-sends).

    Pulls the recent history, marks own/peer turns, loads the contact's style
    profile if a Storage is wired, and asks the injected ``suggest_fn`` for text.
    A failing ``suggest_fn`` is logged (``logger.exception``) and re-raised — UIs
    surface the error rather than silently showing nothing.
    """

    def __init__(
        self,
        *,
        client,
        suggest_fn,
        storage=None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ):
        self._client = client
        self._suggest_fn = suggest_fn
        self._storage = storage
        self._history_limit = history_limit

    async def _context(self, dialog_id: int) -> list[ContextMessage]:
        history = await self._client.history(dialog_id, self._history_limit)
        return [ContextMessage(out=bool(m.out), text=m.text or "") for m in history]

    async def suggest(self, dialog_id: int) -> str:
        """Draft a reply for ``dialog_id``; returns the text (may be empty)."""
        context = await self._context(dialog_id)
        profile = None
        if self._storage is not None:
            profile = await load_style_profile(self._storage, dialog_id)
        try:
            return await self._suggest_fn(context, profile)
        except Exception:
            logger.exception("suggest failed for dialog %s", dialog_id)
            raise

    async def learn(self, dialog_id: int) -> StyleProfile:
        """Build and persist the style profile from one history pass (explicit)."""
        if self._storage is None:
            raise RuntimeError("learn() needs a Storage — none was wired")
        history = await self._client.history(dialog_id, self._history_limit)
        profile = build_style_profile(history)
        await save_style_profile(self._storage, dialog_id, profile)
        return profile

    async def nudge_candidates(
        self, dialog_ids, *, now: float, after_sec: float = DEFAULT_NUDGE_AFTER_SEC,
    ) -> list[dict]:
        """For the given dialogs, return those the contact read but left unanswered.

        DRAFT-only (#145): for each dialog whose last (own) message was read ≥
        ``after_sec`` ago and never replied to, build a nudge draft. ``dialog_ids``
        is an ALREADY-loaded list (the caller passes the cached DM list — no extra
        dialog fetch, flood discipline). Needs a Storage (the read receipts live
        there); returns ``[{dialog_id, read_at, draft}]``, empty when none qualify.
        """
        if self._storage is None:
            raise RuntimeError("nudge_candidates() needs a Storage — none was wired")
        out: list[dict] = []
        for dialog_id in dialog_ids:
            # flood discipline: check the CHEAP local receipt (SQLite kv) FIRST and skip
            # before any network read. Most DMs have no stored receipt and never qualify,
            # so fetching history for all of them would be an unbounded read storm over the
            # whole DM list — the network history() is gated behind the aged-receipt test.
            last_read_id = await load_last_read(self._storage, dialog_id)
            read_at = await load_read_at(self._storage, dialog_id)
            if last_read_id is None or read_at is None or (now - read_at) < after_sec:
                continue
            history = await self._client.history(dialog_id, self._history_limit)
            if not should_nudge(
                history, last_read_id=last_read_id, read_at=read_at,
                now=now, after_sec=after_sec,
            ):
                # The aged receipt is SPENT whenever the fetched history shows it can't
                # produce a nudge for the current tail — drop it so we don't re-fetch
                # history for this dialog every run (it's re-stamped only by a fresh
                # outbox receipt, exactly when nudging matters again). #145 flood
                # discipline. Two spent shapes:
                #   - the contact REPLIED (tail is incoming), or
                #   - we sent a NEWER message the contact hasn't read yet
                #     (tail is ours but last_read_id < tail.id) — the stored receipt
                #     predates it, so it can't nudge until that newer message is read.
                if history and (not history[-1].out or last_read_id < history[-1].id):
                    await clear_last_read(self._storage, dialog_id)
                continue
            try:
                draft = await self.suggest(dialog_id)
            except Exception:
                logger.exception("nudge draft failed for dialog %s", dialog_id)
                continue
            out.append({"dialog_id": dialog_id, "read_at": read_at, "draft": draft})
        return out
