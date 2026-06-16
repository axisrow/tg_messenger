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


# --- diagnostics (#144): why the suggester is OFF --------------------------------


def suggester_disabled_reason(*, env=None) -> str | None:
    """Why the reply suggester would be OFF, or None when it should work.

    A cheap, side-effect-free check (no client, no Storage, no network) shared by
    the CLI wiring (for a loud startup log) and the UIs (to show the user WHY the 💡
    draft feature is silent). Distinguishes the two real cases — the [agent] extra
    missing vs ``TG_AGENT_MODEL`` unset/malformed — so the message is actionable,
    not a blank "no hint ever appears". The LLM stack is imported LAZILY here so this
    module stays import-light on a bare ``[dev]`` install.
    """
    try:
        import tg_messenger.agent.factory  # noqa: F401  (presence of the [agent] extra)
    except ImportError:
        return 'the [agent] extra is not installed — pip install "tg-messenger[agent]"'
    from tg_messenger.agent.config import AgentConfig

    try:
        AgentConfig.from_env(env, require_allowlist=False)
    except ValueError as exc:
        return str(exc)
    return None


# --- live settings (#143): enable / history / model, persisted in kv -------------
#
# Mirrors the inbound-translation settings pattern: the three knobs live in the
# per-profile SQLite ``kv`` table and are read at runtime so a change in the UI
# applies WITHOUT a process restart. Env (``TG_SUGGEST_HISTORY`` / ``TG_AGENT_MODEL``)
# remains the default; a stored value overrides it.

SUGGEST_ENABLED_KEY = "suggest_enabled"
SUGGEST_HISTORY_KEY = "suggest_history"
SUGGEST_MODEL_KEY = "suggest_model"


def _coerce_history(value, *, default: int = DEFAULT_HISTORY_LIMIT) -> int:
    """Parse a history-limit value; reject non-positive/non-integer (fail-fast)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"suggest history limit must be an integer, got {value!r}") from None
    if parsed < 1:
        raise ValueError("suggest history limit must be a positive integer.")
    return parsed


async def get_suggest_settings(storage, *, default_history: int = DEFAULT_HISTORY_LIMIT) -> dict:
    """Return the stored suggester settings (with defaults filled in).

    Keys: ``enabled`` (bool), ``history`` (int), ``model`` (str | None — the
    override on top of ``TG_AGENT_MODEL``, None when unset).
    """
    enabled = await storage.get_value(SUGGEST_ENABLED_KEY)
    history = await storage.get_value(SUGGEST_HISTORY_KEY)
    model = await storage.get_value(SUGGEST_MODEL_KEY)
    return {
        "enabled": True if enabled is None else bool(enabled),
        "history": default_history if history is None else _coerce_history(history),
        "model": (str(model).strip() or None) if model is not None else None,
    }


async def set_suggest_settings(
    storage, *, enabled: bool, history: int, model: str | None
) -> None:
    """Persist the suggester settings (validates history before writing)."""
    history = _coerce_history(history)
    await storage.set_value(SUGGEST_ENABLED_KEY, bool(enabled))
    await storage.set_value(SUGGEST_HISTORY_KEY, history)
    value = (model or "").strip() or None
    if value is None:
        await storage.execute("DELETE FROM kv WHERE key = ?", (SUGGEST_MODEL_KEY,))
    else:
        await storage.set_value(SUGGEST_MODEL_KEY, value)


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
        suggest_fn_factory=None,
    ):
        self._client = client
        self._suggest_fn = suggest_fn
        # the original (env/default-model) contact — restored when a model override
        # is CLEARED, so clearing live-reverts to the default instead of leaving the
        # previously-overridden model active in-process (#143 review).
        self._default_suggest_fn = suggest_fn
        self._storage = storage
        self._history_limit = history_limit
        # builds a fresh suggest_fn for a model name (factory.py owns init_chat_model);
        # None when no live model swap is wired (tests, bare installs).
        self._suggest_fn_factory = suggest_fn_factory

    async def _effective_history(self) -> int:
        """The history limit to use now: a stored value overrides the constructor."""
        if self._storage is None:
            return self._history_limit
        value = await self._storage.get_value(SUGGEST_HISTORY_KEY)
        return self._history_limit if value is None else _coerce_history(value)

    async def _is_enabled(self) -> bool:
        if self._storage is None:
            return True
        value = await self._storage.get_value(SUGGEST_ENABLED_KEY)
        return True if value is None else bool(value)

    async def _context(self, dialog_id: int) -> list[ContextMessage]:
        history = await self._client.history(dialog_id, await self._effective_history())
        return [ContextMessage(out=bool(m.out), text=m.text or "") for m in history]

    async def suggest(self, dialog_id: int) -> str:
        """Draft a reply for ``dialog_id``; returns the text (may be empty).

        Returns ``""`` when the suggester is disabled in settings — UIs already
        treat an empty draft as "no hint".
        """
        if not await self._is_enabled():
            return ""
        context = await self._context(dialog_id)
        profile = None
        if self._storage is not None:
            profile = await load_style_profile(self._storage, dialog_id)
        try:
            return await self._suggest_fn(context, profile)
        except Exception:
            logger.exception("suggest failed for dialog %s", dialog_id)
            raise

    def set_suggest_fn(self, suggest_fn) -> None:
        """Swap the model contact in place (used after a live model-override change)."""
        self._suggest_fn = suggest_fn

    def reset_suggest_fn(self) -> None:
        """Revert to the default (env/``TG_AGENT_MODEL``) contact — used when the
        model override is cleared, so live drafts stop using the old override."""
        self._suggest_fn = self._default_suggest_fn

    @property
    def supports_model_swap(self) -> bool:
        return self._suggest_fn_factory is not None

    def build_suggest_fn(self, model_name: str):
        """Build (and validate) a suggest_fn for ``model_name`` via the injected factory.

        Raises ``RuntimeError`` if no factory was wired (no LLM stack), or whatever
        the factory raises for an unusable model name.
        """
        if self._suggest_fn_factory is None:
            raise RuntimeError("model override needs the [agent] extra — no factory wired")
        return self._suggest_fn_factory(model_name)

    async def learn(self, dialog_id: int) -> StyleProfile:
        """Build and persist the style profile from one history pass (explicit)."""
        if self._storage is None:
            raise RuntimeError("learn() needs a Storage — none was wired")
        history = await self._client.history(dialog_id, await self._effective_history())
        profile = build_style_profile(history)
        await save_style_profile(self._storage, dialog_id, profile)
        return profile
