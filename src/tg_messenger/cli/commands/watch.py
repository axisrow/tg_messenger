"""CLI watch commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

from pathlib import Path

import click

from tg_messenger.cli import main as cli_main


@click.command()
@click.option("--session", default="default")
@click.pass_context
def listen(ctx: click.Context, session: str) -> None:
    """Print incoming messages live."""
    session = cli_main._effective_session(ctx, session)

    async def _do():
        client = cli_main.make_client(session_name=session)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            click.echo("Listening for incoming messages (Ctrl+C to stop)...")
            async for ev in client.listen():
                click.echo(f"← [{ev.dialog_id}] {ev.message.text or '<media>'}")
        finally:
            await client.disconnect()

    cli_main._run_interruptible(_do(), session=session)


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
            click.echo("Watching for deletions of your messages (Ctrl+C to stop)...")
            await DeletionWatcher(client, echo=click.echo).run()
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


COMMANDS = [listen, watch, moderate, moderate_rules]
