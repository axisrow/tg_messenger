"""ModerationEngine — rule models, matching, dry-run, enforce, storage, CLI.

A service above core (DeletionWatcher pattern): listens to messages + chat actions,
applies the first matching rule per chat, journals everything. Destructive by nature,
so **dry-run is the default** — actions only fire under enforce=True. No real network,
no real sleep (clock injected); rule storage on tmp SQLite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tg_messenger.core.models import ChatActionEvent, Message, User
from tg_messenger.core.moderation import (
    ModerationEngine,
    ModerationRule,
    RuleActions,
    RuleConditions,
    add_rule,
    list_rules,
    register_moderation_migrations,
    remove_rule,
    rule_matches,
)
from tg_messenger.core.storage import Storage


def _msg(text=None, *, is_forward=False, sender_id=7):
    return Message(
        id=1, dialog_id=-100200, sender_id=sender_id, out=False,
        text=text, date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# --- цикл 83: модели + матчинг ---

def test_rule_minimal_defaults():
    rule = ModerationRule(chat_id=-100200, name="r1")
    assert rule.enabled is True
    assert rule.conditions.pattern is None
    assert rule.actions.delete is False


def test_pattern_matches_text():
    cond = RuleConditions(pattern=r"\bspam\b")
    assert rule_matches(cond, _msg("this is spam here"), is_new_member=False)
    assert not rule_matches(cond, _msg("hammer"), is_new_member=False)


def test_invalid_regex_rejected_fail_fast():
    with pytest.raises(ValidationError):
        RuleConditions(pattern="(unclosed")


def test_has_link_condition():
    cond = RuleConditions(has_link=True)
    assert rule_matches(cond, _msg("visit https://example.com now"), is_new_member=False)
    assert not rule_matches(cond, _msg("no links here"), is_new_member=False)


def test_is_forward_condition():
    cond = RuleConditions(is_forward=True)
    msg = _msg("forwarded")
    assert rule_matches(cond, msg, is_new_member=False, is_forward=True)
    assert not rule_matches(cond, msg, is_new_member=False, is_forward=False)


def test_and_semantics_all_conditions_required():
    # both pattern AND has_link must hold
    cond = RuleConditions(pattern="buy", has_link=True)
    assert rule_matches(cond, _msg("buy at https://x.com"), is_new_member=False)
    assert not rule_matches(cond, _msg("buy now"), is_new_member=False)  # no link
    assert not rule_matches(cond, _msg("see https://x.com"), is_new_member=False)  # no pattern


def test_new_member_condition():
    cond = RuleConditions(from_new_member_within_sec=60)
    msg = _msg("hi")
    assert rule_matches(cond, msg, is_new_member=True)
    assert not rule_matches(cond, msg, is_new_member=False)


def test_empty_conditions_matches_everything():
    # a rule with no conditions matches any message (acts on all)
    cond = RuleConditions()
    assert rule_matches(cond, _msg("anything"), is_new_member=False)


def test_rule_actions_defaults():
    a = RuleActions()
    assert a.delete is False and a.mute_sec is None and a.ban is False and a.warn_text is None


# --- цикл 84: хранение правил (storage) ---

def _rule(chat_id=-100200, name="r1", **kw):
    return ModerationRule(
        chat_id=chat_id, name=name,
        conditions=kw.pop("conditions", RuleConditions(pattern="spam")),
        actions=kw.pop("actions", RuleActions(delete=True)),
        **kw,
    )


async def _storage(tmp_path):
    storage = Storage(tmp_path / "mod.db")
    register_moderation_migrations(storage)
    await storage.connect()
    return storage


async def test_add_list_roundtrip(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await add_rule(storage, _rule())
        rules = await list_rules(storage)
        assert len(rules) == 1
        r = rules[0]
        assert r.chat_id == -100200 and r.name == "r1"
        assert r.conditions.pattern == "spam"
        assert r.actions.delete is True
    finally:
        await storage.close()


async def test_add_rule_upserts_same_pk(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await add_rule(storage, _rule(actions=RuleActions(delete=True)))
        await add_rule(storage, _rule(actions=RuleActions(ban=True)))  # same (chat_id, name)
        rules = await list_rules(storage)
        assert len(rules) == 1
        assert rules[0].actions.ban is True and rules[0].actions.delete is False
    finally:
        await storage.close()


async def test_list_rules_filtered_by_chat_id(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await add_rule(storage, _rule(chat_id=-100200, name="a"))
        await add_rule(storage, _rule(chat_id=-100999, name="b"))
        only = await list_rules(storage, chat_id=-100200)
        assert [r.name for r in only] == ["a"]
    finally:
        await storage.close()


async def test_remove_rule(tmp_path):
    storage = await _storage(tmp_path)
    try:
        await add_rule(storage, _rule(name="a"))
        await add_rule(storage, _rule(name="b"))
        await remove_rule(storage, -100200, "a")
        rules = await list_rules(storage)
        assert [r.name for r in rules] == ["b"]
    finally:
        await storage.close()


async def test_migration_applied(tmp_path):
    storage = await _storage(tmp_path)
    try:
        # both tables exist — a query against each succeeds
        await storage.fetchall("SELECT * FROM moderation_rules")
        await storage.fetchall("SELECT * FROM moderation_log")
    finally:
        await storage.close()


# --- циклы 85-87: ModerationEngine (новички, флуд, dry-run, enforce) ---

ADMIN = User(id=1, first_name="Me")


class FakeModClient:
    """Core-client stub: records destructive actions; bus-free (engine drives via methods)."""

    def __init__(self):
        self.deleted: list = []
        self.muted: list = []
        self.banned: list = []
        self.sent: list = []
        self.fail_action: str | None = None  # name of action that raises

    async def get_me(self):
        return ADMIN

    async def delete_messages(self, peer, message_ids, revoke=True):
        if self.fail_action == "delete":
            raise RuntimeError("delete boom")
        self.deleted.append((peer, list(message_ids)))

    async def mute_user(self, peer, user_id, until_sec):
        if self.fail_action == "mute":
            raise RuntimeError("mute boom")
        self.muted.append((peer, user_id, until_sec))

    async def ban_user(self, peer, user_id):
        if self.fail_action == "ban":
            raise RuntimeError("ban boom")
        self.banned.append((peer, user_id))

    async def send_text(self, peer, text, reply_to=None):
        if self.fail_action == "warn":
            raise RuntimeError("warn boom")
        self.sent.append((peer, text, reply_to))


CHAT = -100200


def _mk_engine(tmp_path, *, enforce=False, clock=None):
    storage = Storage(tmp_path / "mod.db")
    register_moderation_migrations(storage)
    client = FakeModClient()
    t = {"now": 0.0}
    engine = ModerationEngine(
        client, storage, enforce=enforce,
        clock=clock or (lambda: t["now"]),
    )
    return engine, client, storage, t


def _imsg(text="hi", *, sender_id=7, msg_id=1, is_forward=False):
    return Message(
        id=msg_id, dialog_id=CHAT, sender_id=sender_id, out=False,
        text=text, date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


async def test_new_member_match_within_window(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        rule = ModerationRule(
            chat_id=CHAT, name="newbie",
            conditions=RuleConditions(from_new_member_within_sec=60),
            actions=RuleActions(delete=True),
        )
        await add_rule(storage, rule)
        # user joins at t=0
        engine.on_chat_action(ChatActionEvent(
            dialog_id=CHAT, kind="join", user=User(id=7, first_name="New"),
        ))
        # message at t=30 → within window → matches
        t["now"] = 30.0
        await engine.process_message(_imsg(sender_id=7))
        assert client.deleted == [(CHAT, [1])]
    finally:
        await storage.close()


async def test_new_member_no_match_after_window(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="newbie",
            conditions=RuleConditions(from_new_member_within_sec=60),
            actions=RuleActions(delete=True),
        ))
        engine.on_chat_action(ChatActionEvent(
            dialog_id=CHAT, kind="join", user=User(id=7, first_name="New"),
        ))
        t["now"] = 120.0  # past the 60s window
        await engine.process_message(_imsg(sender_id=7))
        assert client.deleted == []
    finally:
        await storage.close()


async def test_rate_limit_window_fires(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="flood",
            conditions=RuleConditions(max_messages_per_minute=3),
            actions=RuleActions(delete=True),
        ))
        # 3 messages within the same minute → the 3rd hits the limit
        for i in range(1, 4):
            t["now"] = float(i)
            await engine.process_message(_imsg(sender_id=7, msg_id=i))
        assert client.deleted == [(CHAT, [3])]  # only the 3rd is over the limit
    finally:
        await storage.close()


async def test_rate_limit_window_slides(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="flood",
            conditions=RuleConditions(max_messages_per_minute=2),
            actions=RuleActions(delete=True),
        ))
        t["now"] = 0.0
        await engine.process_message(_imsg(sender_id=7, msg_id=1))
        t["now"] = 100.0  # >60s later — old message slid out of the window
        await engine.process_message(_imsg(sender_id=7, msg_id=2))
        assert client.deleted == []  # only 1 in the trailing 60s
    finally:
        await storage.close()


async def test_dry_run_does_not_call_client(tmp_path, caplog):
    import logging
    engine, client, storage, t = _mk_engine(tmp_path, enforce=False)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="spam",
            conditions=RuleConditions(pattern="spam"),
            actions=RuleActions(delete=True, ban=True),
        ))
        with caplog.at_level(logging.INFO, logger="tg_messenger.core.moderation"):
            await engine.process_message(_imsg(text="this is spam"))
        assert client.deleted == [] and client.banned == []
        assert any("would" in r.message for r in caplog.records)
        log = await storage.fetchall("SELECT rule_name, action, dry_run FROM moderation_log")
        assert log and all(row[2] == 1 for row in log)  # dry_run=1
        assert {row[1] for row in log} == {"delete", "ban"}
    finally:
        await storage.close()


async def test_enforce_delete_calls_client_and_logs(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="spam",
            conditions=RuleConditions(pattern="spam"),
            actions=RuleActions(delete=True),
        ))
        await engine.process_message(_imsg(text="spam", msg_id=5))
        assert client.deleted == [(CHAT, [5])]
        log = await storage.fetchall("SELECT action, dry_run FROM moderation_log")
        assert log == [("delete", 0)]
    finally:
        await storage.close()


async def test_enforce_all_actions(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="full",
            conditions=RuleConditions(pattern="bad"),
            actions=RuleActions(delete=True, mute_sec=300, ban=True, warn_text="no!"),
        ))
        await engine.process_message(_imsg(text="bad", sender_id=7, msg_id=5))
        assert client.deleted == [(CHAT, [5])]
        assert client.muted == [(CHAT, 7, 300)]
        assert client.banned == [(CHAT, 7)]
        assert client.sent and client.sent[0][0] == CHAT and client.sent[0][1] == "no!"
    finally:
        await storage.close()


async def test_action_error_keeps_engine_alive(tmp_path, caplog):
    import logging
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    client.fail_action = "delete"
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="full",
            conditions=RuleConditions(pattern="bad"),
            actions=RuleActions(delete=True, ban=True),
        ))
        with caplog.at_level(logging.ERROR, logger="tg_messenger.core.moderation"):
            await engine.process_message(_imsg(text="bad", sender_id=7, msg_id=5))
        # delete failed but ban still ran — engine survives
        assert client.banned == [(CHAT, 7)]
        assert any(r.exc_info for r in caplog.records)
    finally:
        await storage.close()


async def test_first_matching_rule_wins(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="a",
            conditions=RuleConditions(pattern="x"), actions=RuleActions(delete=True),
        ))
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="b",
            conditions=RuleConditions(pattern="x"), actions=RuleActions(ban=True),
        ))
        await engine.process_message(_imsg(text="x", msg_id=5))
        # only the first matching rule's actions fired
        assert client.deleted == [(CHAT, [5])] and client.banned == []
    finally:
        await storage.close()


async def test_disabled_rule_skipped(tmp_path):
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="off", enabled=False,
            conditions=RuleConditions(pattern="x"), actions=RuleActions(delete=True),
        ))
        await engine.process_message(_imsg(text="x"))
        assert client.deleted == []
    finally:
        await storage.close()


async def test_is_forward_rule_fires_in_engine(tmp_path):
    # a forwarded message must trigger an is_forward rule (the engine reads Message.is_forward)
    engine, client, storage, t = _mk_engine(tmp_path, enforce=True)
    await storage.connect()
    try:
        await add_rule(storage, ModerationRule(
            chat_id=CHAT, name="noforward",
            conditions=RuleConditions(is_forward=True),
            actions=RuleActions(delete=True),
        ))
        fwd = Message(id=5, dialog_id=CHAT, sender_id=7, out=False, text="fwd",
                      date=datetime(2024, 1, 1, tzinfo=timezone.utc), is_forward=True)
        await engine.process_message(fwd)
        assert client.deleted == [(CHAT, [5])]
        # a non-forward message with the same rule does NOT match
        plain = Message(id=6, dialog_id=CHAT, sender_id=7, out=False, text="plain",
                        date=datetime(2024, 1, 1, tzinfo=timezone.utc), is_forward=False)
        await engine.process_message(plain)
        assert client.deleted == [(CHAT, [5])]  # unchanged
    finally:
        await storage.close()
