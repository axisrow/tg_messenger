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


def _first_word(text: str) -> str:
    stripped = text.strip().lower().lstrip("!?.,")
    return stripped.split()[0] if stripped.split() else ""


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


# --- read receipts (#17, cycle 98): "they've seen our message up to N" ----------
#
# v1 only RECORDS the outbox receipt (the other party read our messages up to
# max_id) in the kv table — the suggester does not act on it yet (no auto-nudge).
# It's a stored signal a future ghostwrite (#18) can build on.


def last_read_key(dialog_id: int) -> str:
    return f"last_read_{int(dialog_id)}"


async def record_last_read(storage, event: MessageReadEvent) -> None:
    """Persist an OUTBOX read receipt (they read our messages up to ``max_id``)."""
    if not event.outbox:
        return  # inbox = WE read theirs — not the signal the suggester cares about
    await storage.set_value(last_read_key(event.dialog_id), int(event.max_id))


async def load_last_read(storage, dialog_id: int) -> int | None:
    """Return the last ``max_id`` the contact has read of our messages, or None."""
    value = await storage.get_value(last_read_key(dialog_id))
    return int(value) if value is not None else None


async def watch_read_receipts(client, storage) -> None:
    """Drain ``client.listen_reads()`` and record every outbox receipt.

    Best-effort and non-fatal: one bad event is logged and the loop continues.
    """
    async for event in client.listen_reads():
        try:
            await record_last_read(storage, event)
        except Exception:
            logger.exception("failed to record read receipt for dialog %s",
                             getattr(event, "dialog_id", "?"))


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
