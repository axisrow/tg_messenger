"""ModerationEngine — auto-moderation rules over the core client (DeletionWatcher pattern).

A service that listens to ``listen_all()`` + ``listen_chat_actions()``, applies the
first matching rule per chat, and journals every decision. Destructive actions
(delete/mute/ban) are gated behind ``enforce=True`` — **dry-run is the default**, so a
mis-written rule logs "would …" instead of acting. Rules live in SQLite (#13); the engine
is built test-first and never touches the network directly (it calls core client methods,
which carry flood-wait retry for free).
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel, field_validator

from tg_messenger.core.models import ChatActionEvent, Message

logger = logging.getLogger(__name__)

# a cheap link detector — http(s), t.me, www, or channel @mentions count as links
_LINK_RE = re.compile(r"(https?://|t\.me/|www\.|(?<!\w)@[A-Za-z0-9_]{5,})", re.IGNORECASE)


class RuleConditions(BaseModel):
    """AND-combined match conditions; every set field must hold for the rule to fire."""

    pattern: str | None = None              # regex over message text
    has_link: bool = False
    is_forward: bool = False
    from_new_member_within_sec: int | None = None
    max_messages_per_minute: int | None = None

    @field_validator("pattern")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern: {exc}") from exc
        return v


class RuleActions(BaseModel):
    delete: bool = False
    mute_sec: int | None = None
    ban: bool = False
    warn_text: str | None = None

    @field_validator("mute_sec")
    @classmethod
    def _valid_mute_duration(cls, v: int | None) -> int | None:
        if v is not None and v < 30:
            raise ValueError("mute_sec must be at least 30 seconds")
        return v


class ModerationRule(BaseModel):
    chat_id: int
    name: str
    enabled: bool = True
    conditions: RuleConditions = RuleConditions()
    actions: RuleActions = RuleActions()


def rule_matches(
    conditions: RuleConditions,
    message: Message,
    *,
    is_new_member: bool,
    is_forward: bool = False,
    over_rate_limit: bool = False,
) -> bool:
    """True if EVERY set condition holds for ``message`` (AND semantics).

    ``is_forward``/``is_new_member``/``over_rate_limit`` are computed by the engine
    (they depend on event context the Message model doesn't carry) and passed in.
    """
    text = message.text or ""
    if conditions.pattern is not None and not re.search(conditions.pattern, text):
        return False
    if conditions.has_link and not _LINK_RE.search(text):
        return False
    if conditions.is_forward and not is_forward:
        return False
    if conditions.from_new_member_within_sec is not None and not is_new_member:
        return False
    if conditions.max_messages_per_minute is not None and not over_rate_limit:
        return False
    return True


# --- storage (#13): rules + decision journal -------------------------------------

# Registered via storage.register_migrations BEFORE connect() — the engine and CLI
# share the same versioned schema. PRIMARY KEY (chat_id, name) makes add_rule an upsert.
MODERATION_MIGRATIONS = [
    "CREATE TABLE moderation_rules ("
    " chat_id INTEGER NOT NULL,"
    " name TEXT NOT NULL,"
    " enabled INTEGER NOT NULL,"
    " conditions TEXT NOT NULL,"
    " actions TEXT NOT NULL,"
    " PRIMARY KEY (chat_id, name))",
    "CREATE TABLE moderation_log ("
    " chat_id INTEGER NOT NULL,"
    " message_id INTEGER,"
    " rule_name TEXT NOT NULL,"
    " action TEXT NOT NULL,"
    " dry_run INTEGER NOT NULL,"
    " ts TEXT NOT NULL)",
]


def register_moderation_migrations(storage) -> None:
    """Register the moderator's tables on a Storage (call before ``connect()``)."""
    storage.register_migrations(MODERATION_MIGRATIONS)


async def add_rule(storage, rule: ModerationRule) -> None:
    """Insert/replace a rule (upsert on the ``(chat_id, name)`` primary key)."""
    await storage.execute(
        "INSERT INTO moderation_rules (chat_id, name, enabled, conditions, actions) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, name) DO UPDATE SET "
        "enabled = excluded.enabled, conditions = excluded.conditions, actions = excluded.actions",
        (
            rule.chat_id,
            rule.name,
            int(rule.enabled),
            rule.conditions.model_dump_json(),
            rule.actions.model_dump_json(),
        ),
    )


async def list_rules(storage, chat_id: int | None = None) -> list[ModerationRule]:
    """All rules, or only those for ``chat_id``; ordered by (chat_id, name) for determinism."""
    if chat_id is None:
        rows = await storage.fetchall(
            "SELECT chat_id, name, enabled, conditions, actions FROM moderation_rules "
            "ORDER BY chat_id, name"
        )
    else:
        rows = await storage.fetchall(
            "SELECT chat_id, name, enabled, conditions, actions FROM moderation_rules "
            "WHERE chat_id = ? ORDER BY name",
            (int(chat_id),),
        )
    return [
        ModerationRule(
            chat_id=row[0],
            name=row[1],
            enabled=bool(row[2]),
            conditions=RuleConditions.model_validate_json(row[3]),
            actions=RuleActions.model_validate_json(row[4]),
        )
        for row in rows
    ]


async def remove_rule(storage, chat_id: int, name: str) -> int:
    return await storage.execute(
        "DELETE FROM moderation_rules WHERE chat_id = ? AND name = ?", (int(chat_id), name)
    )


# --- ModerationEngine (#16) ------------------------------------------------------

DEFAULT_MEMBER_CACHE_SIZE = 2000
DEFAULT_RATE_CACHE_SIZE = 2000
RATE_WINDOW_SEC = 60.0


async def check_admin_rights(client, storage) -> dict[int, bool]:
    """For every chat that has rules, check whether we have the rights they need.

    Returns ``{chat_id: can_run_rules}``. Chats where we lack rights are logged
    (``logger.warning``) — the engine simply finds no enabled rules there at
    runtime if the caller disables them; this never raises.
    """
    rules_by_chat: dict[int, list[ModerationRule]] = {}
    for rule in await list_rules(storage):
        rules_by_chat.setdefault(rule.chat_id, []).append(rule)
    result: dict[int, bool] = {}
    for chat_id, rules in sorted(rules_by_chat.items()):
        needs_delete = any(rule.enabled and rule.actions.delete for rule in rules)
        needs_ban = any(
            rule.enabled and (rule.actions.ban or rule.actions.mute_sec is not None)
            for rule in rules
        )
        rights = await client.moderation_rights(chat_id)
        ok = (not needs_delete or rights["delete_messages"]) and (
            not needs_ban or rights["ban_users"]
        )
        result[chat_id] = ok
        if not ok:
            logger.warning(
                "no admin rights in chat %s — its rules will not be enforced", chat_id
            )
    return result


class ModerationEngine:
    """Listens to messages + chat actions, applies the first matching rule, journals it.

    DeletionWatcher pattern: ``run()`` fans out two consumers via ``asyncio.gather``
    (NOT TaskGroup — keeps Ctrl+C clean). Destructive by nature, so **dry-run is the
    default** — actions only fire when ``enforce=True``; otherwise the engine logs
    "would …" and records ``dry_run=1``. Bounded caches (OrderedDict / deque per key)
    track new members and message rates; ``clock`` is injected so tests never sleep.
    """

    def __init__(
        self,
        client,
        storage,
        *,
        enforce: bool = False,
        clock: Callable[[], float] = time.monotonic,
        member_cache_size: int = DEFAULT_MEMBER_CACHE_SIZE,
        rate_cache_size: int = DEFAULT_RATE_CACHE_SIZE,
    ):
        self._client = client
        self._storage = storage
        self._enforce = enforce
        self._clock = clock
        # chats where we lack admin rights — rules there are skipped (set via disable_chats)
        self._blocked_chats: set[int] = set()
        self._member_cache_size = member_cache_size
        self._rate_cache_size = rate_cache_size
        # (chat_id, user_id) -> monotonic join time
        self._joined: OrderedDict[tuple[int, int], float] = OrderedDict()
        # (chat_id, sender_id) -> sliding window of recent message times
        self._rate: OrderedDict[tuple[int, int], deque[float]] = OrderedDict()
        # Rules are process-local for the running moderator. CLI add/remove commands
        # happen before starting a fresh engine; the hot message path should not hit
        # SQLite for every incoming message in active groups.
        self._rules_by_chat: dict[int, list[ModerationRule]] | None = None

    def disable_chats(self, chat_ids) -> None:
        """Mark chats we can't moderate (no admin rights) so their rules are skipped."""
        self._blocked_chats.update(int(c) for c in chat_ids)

    async def load_rules(self) -> None:
        """Load moderation rules into memory for the current engine run."""
        rules_by_chat: dict[int, list[ModerationRule]] = {}
        for rule in await list_rules(self._storage):
            rules_by_chat.setdefault(rule.chat_id, []).append(rule)
        self._rules_by_chat = rules_by_chat

    async def _rules_for_chat(self, chat_id: int) -> list[ModerationRule]:
        if self._rules_by_chat is None:
            await self.load_rules()
        return list((self._rules_by_chat or {}).get(chat_id, ()))

    async def run(self) -> None:
        import asyncio

        # gather, не TaskGroup: TaskGroup оборачивает KeyboardInterrupt
        # в BaseExceptionGroup и ломает Ctrl+C-обработку в CLI
        await asyncio.gather(self._consume_messages(), self._consume_chat_actions())

    async def _consume_messages(self) -> None:
        async for ev in self._client.listen_all():
            try:
                await self.process_message(ev.message)
            except Exception:
                logger.exception("moderation failed to process message in dialog %s",
                                 getattr(ev, "dialog_id", "?"))

    async def _consume_chat_actions(self) -> None:
        async for ev in self._client.listen_chat_actions():
            try:
                self.on_chat_action(ev)
            except Exception:
                logger.exception("moderation failed to handle chat action")

    def on_chat_action(self, ev: ChatActionEvent) -> None:
        """Record a join so ``from_new_member_within_sec`` can be evaluated later."""
        if ev.kind != "join" or ev.user is None:
            return
        key = (ev.dialog_id, ev.user.id)
        self._joined[key] = self._clock()
        self._joined.move_to_end(key)
        while len(self._joined) > self._member_cache_size:
            self._joined.popitem(last=False)

    def _is_new_member(self, chat_id: int, user_id: int, within_sec: int) -> bool:
        joined_at = self._joined.get((chat_id, user_id))
        if joined_at is None:
            return False
        return (self._clock() - joined_at) <= within_sec

    def _record_rate_window(self, chat_id: int, sender_id: int) -> int:
        """Record this message once, then return the sender's trailing 60s count."""
        now = self._clock()
        key = (chat_id, sender_id)
        window = self._rate.get(key)
        if window is None:
            window = deque()
            self._rate[key] = window
        self._rate.move_to_end(key)
        window.append(now)
        while window and now - window[0] > RATE_WINDOW_SEC:
            window.popleft()
        while len(self._rate) > self._rate_cache_size:
            self._rate.popitem(last=False)
        return len(window)

    async def process_message(self, message: Message) -> None:
        """Apply the first matching enabled rule for the message's chat (if any)."""
        chat_id = message.dialog_id
        if chat_id in self._blocked_chats:
            return  # no admin rights here — nothing to do
        rules = await self._rules_for_chat(chat_id)
        rate_count: int | None = None
        if any(r.enabled and r.conditions.max_messages_per_minute is not None for r in rules):
            rate_count = self._record_rate_window(chat_id, message.sender_id)
        for rule in rules:
            if not rule.enabled:
                continue
            cond = rule.conditions
            is_new_member = False
            if cond.from_new_member_within_sec is not None:
                is_new_member = self._is_new_member(
                    chat_id, message.sender_id, cond.from_new_member_within_sec
                )
            over_rate = False
            if cond.max_messages_per_minute is not None:
                over_rate = (rate_count or 0) >= cond.max_messages_per_minute
            if rule_matches(
                cond, message,
                is_new_member=is_new_member,
                is_forward=message.is_forward,
                over_rate_limit=over_rate,
            ):
                await self._apply(rule, message)
                return  # first matching rule wins

    async def _apply(self, rule: ModerationRule, message: Message) -> None:
        chat_id = message.dialog_id
        actions = rule.actions
        if actions.warn_text is not None:
            await self._do(rule, message, "warn",
                           lambda: self._client.send_text(chat_id, actions.warn_text,
                                                          reply_to=message.id))
        if actions.mute_sec is not None:
            await self._do(rule, message, "mute",
                           lambda: self._client.mute_user(chat_id, message.sender_id,
                                                          actions.mute_sec))
        if actions.ban:
            await self._do(rule, message, "ban",
                           lambda: self._client.ban_user(chat_id, message.sender_id))
        if actions.delete:
            await self._do(rule, message, "delete",
                           lambda: self._client.delete_messages(chat_id, [message.id]))

    async def _do(self, rule: ModerationRule, message: Message, action: str, call) -> None:
        """Run (or, in dry-run, skip) one action; journal it; never let it kill the engine."""
        if not self._enforce:
            logger.info("would %s in chat %s (rule %r, message %s)",
                        action, message.dialog_id, rule.name, message.id)
            await self._journal(rule, message, action, dry_run=True)
            return
        try:
            await call()
        except Exception:
            logger.exception("moderation action %s failed in chat %s (rule %r)",
                             action, message.dialog_id, rule.name)
            return  # engine survives — other actions/messages keep going
        await self._journal(rule, message, action, dry_run=False)

    async def _journal(self, rule: ModerationRule, message: Message, action: str,
                       *, dry_run: bool) -> None:
        try:
            await self._storage.execute(
                "INSERT INTO moderation_log "
                "(chat_id, message_id, rule_name, action, dry_run, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (message.dialog_id, message.id, rule.name, action, int(dry_run),
                 datetime.now(timezone.utc).isoformat()),
            )
        except Exception:
            logger.exception("failed to write moderation log entry")
