"""CLI watch commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from tg_messenger.cli import main as cli_main


@click.command()
@click.option("--session", default="default")
@click.option("--ids", is_flag=True,
              help="Include the message id in every line: [DIALOG_ID] [MSG_ID] text.")
@click.option("--out", is_flag=True,
              help="Also print own messages sent from other devices (→ lines).")
@click.pass_context
def listen(ctx: click.Context, session: str, ids: bool, out: bool) -> None:
    """Print incoming messages live."""
    session = cli_main._effective_session(ctx, session)

    def _line(arrow: str, ev) -> str:
        text = ev.message.text or "<media>"
        if ids:
            return f"{arrow} [{ev.dialog_id}] [{ev.message.id}] {text}"
        return f"{arrow} [{ev.dialog_id}] {text}"

    async def _do():
        client = cli_main.make_client(session_name=session)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            click.echo("Listening for incoming messages (Ctrl+C to stop)...")

            async def _incoming() -> None:
                async for ev in client.listen():
                    click.echo(_line("←", ev))

            # gather, NOT TaskGroup — the project's Ctrl+C convention (watch.py)
            async def _pump_outgoing() -> None:
                async for ev in client.listen_outgoing():
                    click.echo(_line("→", ev))

            if out:
                await asyncio.gather(_incoming(), _pump_outgoing())
            else:
                await _incoming()
        finally:
            await client.disconnect()

    cli_main._run_interruptible(_do(), session=session)


async def _collect_burst(queue: asyncio.Queue, chat_id: int | None,
                         settle_s: float, timeout_s: float) -> list | None:
    """Collect ONE settled burst of matching events from ``queue`` (#329).

    ``None`` = timeout elapsed with no matching message; a list = the burst.
    A message from another chat neither matches nor resets the settle timer;
    each matching message appends and restarts the settle window, so a burst
    of rapid messages is delivered as one event, not N wake-ups. With no
    ``chat_id`` filter the burst is scoped to the FIRST matched dialog — one
    record must describe one chat (message ids are per-chat).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:  # first matching message, bounded by the overall deadline
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if chat_id is not None and ev.dialog_id != chat_id:
            continue
        if chat_id is None:
            chat_id = ev.dialog_id
        break
    events = [ev]
    settle_deadline = loop.time() + settle_s
    while True:
        remaining = settle_deadline - loop.time()
        if remaining <= 0:
            return events
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return events
        if chat_id is not None and ev.dialog_id != chat_id:
            continue
        events.append(ev)
        settle_deadline = loop.time() + settle_s


@click.command()
@click.option("--chat", "chat", default="any", show_default=True,
              help="Wait on one DM dialog: numeric id, @username or +phone "
                   "(get ids from `tg-messenger dialogs`); 'any' wakes on every "
                   "incoming DM.")
@click.option("--settle-ms", type=int, default=6000, show_default=True,
              help="After the first matching message keep collecting; every new "
                   "message restarts this timer, so a rapid burst arrives as ONE event.")
@click.option("--timeout", type=float, default=60, show_default=True,
              help="Give up after this many seconds without a matching message "
                   "(exit code 4, JSON {\"event\": false, \"reason\": \"timeout\"}).")
@click.option("--json", "as_json", is_flag=True, help="Print one machine-readable JSON record.")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
@click.pass_context
def wait(ctx: click.Context, chat: str, settle_ms: int, timeout: float,
         as_json: bool, session: str) -> None:
    """Block until an incoming message arrives (bounded long-poll, #329).

    On event prints the burst (plain text like `listen`, or one JSON record with
    chat_id/name/message_count/first_message_id/last_message_id/texts). The ids
    feed incremental reads: `tg-messenger read <chat>` or, for library callers,
    core history_since(peer, min_id=last_message_id) — no new read path.
    """
    session = cli_main._effective_session(ctx, session)
    if settle_ms < 0:
        raise click.ClickException("--settle-ms must be >= 0")
    if timeout <= 0:
        raise click.ClickException("--timeout must be > 0")
    from tg_messenger.cli.commands.message import _dialog_arg

    # [record] on event, [None] on timeout; empty on Ctrl+C (_run_interruptible
    # swallows the coro's return value, so the box carries the verdict out)
    verdict: list = []

    async def _do():
        client = cli_main.make_client(session_name=session)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            target = None if chat == "any" else await _dialog_arg(client, chat)
            queue: asyncio.Queue = asyncio.Queue()
            subscribed = asyncio.Event()

            async def _pump():
                async for ev in client.listen(on_subscribed=subscribed.set):
                    queue.put_nowait(ev)

            pump = asyncio.create_task(_pump())
            try:
                try:
                    await asyncio.wait_for(subscribed.wait(), timeout=timeout)
                    burst = await _collect_burst(queue, target, settle_ms / 1000, timeout)
                except asyncio.TimeoutError:
                    burst = None
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            if burst is None:
                verdict.append(None)
                return
            try:
                name = await client.entity_title(burst[0].dialog_id)
            except Exception:
                name = None  # best-effort label; JSON keeps null, ids stay authoritative
            verdict.append({
                "event": True,
                "chat_id": burst[0].dialog_id,
                "name": name,
                "message_count": len(burst),
                "first_message_id": burst[0].message.id,
                "last_message_id": burst[-1].message.id,
                "texts": [ev.message.text or "<media>" for ev in burst],
            })
        finally:
            await client.disconnect()

    cli_main._run_interruptible(_do(), session=session)
    if not verdict:  # Ctrl+C — same contract as `listen`
        return
    result = verdict[0]
    if result is None:
        if as_json:
            click.echo(json.dumps({"event": False, "reason": "timeout"}, ensure_ascii=False))
        else:
            click.echo(f"timeout: no matching message in {timeout:g}s", err=True)
        ctx.exit(4)
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        for text in result["texts"]:
            click.echo(f"← [{result['chat_id']}] {text}")


@click.command()
@click.option("--session", default="default")
@click.pass_context
def watch(ctx: click.Context, session: str) -> None:
    """Back up your deleted messages (e.g. removed by group moderator bots) to Saved Messages."""
    from tg_messenger.core.watch import DeletionWatcher

    session = cli_main._effective_session(ctx, session)

    async def _do():
        client = cli_main.make_client(session_name=session)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            click.echo("Starting deletion watcher...")
            await DeletionWatcher(client, echo=click.echo).run(on_ready=lambda: click.echo(
                "Watching for deletions of your messages (Ctrl+C to stop)..."
            ))
        finally:
            await client.disconnect()

    cli_main._run_interruptible(_do(), session=session)


@click.command()
@click.option("--session", default="default")
@click.option("--enforce", is_flag=True,
              help="Actually delete/mute/ban (default is dry-run: log only).")
@click.pass_context
def moderate(ctx: click.Context, session: str, enforce: bool) -> None:
    """Auto-moderate group chats by stored rules (dry-run unless --enforce).

    On start, checks admin rights in every chat with rules; chats where we can't
    act are skipped with a warning (the command does not abort).
    """
    from tg_messenger.core.moderation import (
        ModerationEngine,
        check_admin_rights,
        register_moderation_migrations,
    )

    session = cli_main._effective_session(ctx, session)
    cli_main._warn_if_send_rate_off()  # #50: warn rules can send (mute/ban notices, warn_text)

    async def _do():
        client = cli_main.make_client(session_name=session)
        storage = cli_main.make_storage(session)
        register_moderation_migrations(storage)
        await storage.connect()
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            rights = await check_admin_rights(client, storage)
            for chat_id, ok in rights.items():
                if not ok:
                    # #187: U+FE0E forces text (not emoji) presentation for a stable width
                    click.echo(f"⚠︎ no admin rights in chat {chat_id} — its rules are disabled",
                               err=True)
            engine = ModerationEngine(client, storage, enforce=enforce)
            engine.disable_chats([cid for cid, ok in rights.items() if not ok])
            mode = "ENFORCING" if enforce else "dry-run"
            click.echo(f"Moderating ({mode}) — Ctrl+C to stop...")
            await engine.run()
        finally:
            await client.disconnect()
            await storage.close()

    cli_main._run_interruptible(_do(), session=session)


@click.group("moderate-rules")
def moderate_rules() -> None:
    """Manage moderation rules (list / add / remove)."""


@moderate_rules.command("list")
@click.option("--session", default="default")
@click.option("--chat", "chat_id", type=int, default=None, help="Filter by chat id.")
@click.pass_context
def moderate_rules_list(ctx: click.Context, session: str, chat_id: int | None) -> None:
    """List stored moderation rules."""
    from tg_messenger.core.moderation import list_rules, register_moderation_migrations

    session = cli_main._effective_session(ctx, session)

    rules = cli_main._run(
        cli_main._with_storage(session, register_moderation_migrations,
                      lambda storage: list_rules(storage, chat_id=chat_id)),
        session=session,
    )
    if not rules:
        click.echo("No rules.")
        return
    for r in rules:
        state = "on" if r.enabled else "off"
        click.echo(f"{r.chat_id}\t{r.name}\t[{state}]")


@moderate_rules.command("add")
@click.argument("rule_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--session", default="default")
@click.pass_context
def moderate_rules_add(ctx: click.Context, rule_file: str, session: str) -> None:
    """Add (or replace) a rule from a JSON file (see moderation.json.example)."""
    from tg_messenger.core.moderation import ModerationRule, add_rule, register_moderation_migrations

    session = cli_main._effective_session(ctx, session)

    raw = Path(rule_file).read_text(encoding="utf-8")
    try:
        rule = ModerationRule.model_validate_json(raw)
    except Exception as exc:
        raise click.ClickException(f"invalid rule JSON: {exc}") from exc

    cli_main._run(
        cli_main._with_storage(session, register_moderation_migrations,
                      lambda storage: add_rule(storage, rule)),
        session=session,
    )
    click.echo(f"rule '{rule.name}' added for chat {rule.chat_id}.")


@moderate_rules.command("remove")
@click.argument("chat_id", type=int)
@click.argument("name")
@click.option("--session", default="default")
@click.pass_context
def moderate_rules_remove(ctx: click.Context, chat_id: int, name: str, session: str) -> None:
    """Remove a rule by CHAT_ID and NAME."""
    from tg_messenger.core.moderation import register_moderation_migrations, remove_rule

    session = cli_main._effective_session(ctx, session)

    deleted = cli_main._run(
        cli_main._with_storage(session, register_moderation_migrations,
                      lambda storage: remove_rule(storage, chat_id, name)),
        session=session,
    )
    if deleted == 0:
        raise click.ClickException(f"rule '{name}' not found in chat {chat_id}.")
    click.echo(f"rule '{name}' removed from chat {chat_id}.")


COMMANDS = [listen, wait, watch, moderate, moderate_rules]
