"""CLI agent commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import click

from tg_messenger.cli import main as cli_main


@click.command()
@click.option("--session", default="default")
@click.option("--notify-errors", is_flag=True,
              help="Reply with a short notice when processing a message fails.")
@click.pass_context
def agent(ctx: click.Context, session: str, notify_errors: bool) -> None:
    """AI assistant: auto-reply to incoming DMs, route tasks to a deep agent."""
    session = cli_main._effective_session(ctx, session)
    cli_main._warn_if_send_rate_off()  # #50: loud when the global send cap is off
    # langchain/langgraph трассируются в LangSmith сами по LANGSMITH_* env —
    # здесь только fail-fast (включено без ключа) и видимый статус (#168: общий хелпер)
    cli_main._announce_tracing()

    async def _do():
        client = cli_main.make_client(session_name=session)
        # конфиг и LLM-стек собираем до сети — ошибки настроек видны сразу
        runner = cli_main.make_agent_runner(client, notify_errors=notify_errors)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            click.echo("Agent is listening for incoming messages (Ctrl+C to stop)...")
            await runner.run()
        finally:
            await client.disconnect()

    cli_main._run_interruptible(_do(), session=session, flush_traces=True)


@click.command()
@click.argument("dialog_id", type=int)
@click.option("--send", "do_send", is_flag=True,
              help="Send the draft instead of just printing it.")
@click.option("--learn", "do_learn", is_flag=True,
              help="Build and persist the contact's style profile (one history pass).")
@click.option("--session", default="default")
@click.pass_context
def suggest(ctx: click.Context, dialog_id: int, do_send: bool, do_learn: bool, session: str) -> None:
    """Draft a reply for DIALOG_ID in the style of past messages (#17).

    Human-in-the-loop: prints a draft you review/edit. --send sends it as-is;
    --learn (re)builds the style profile from this dialog's history.
    """
    session = cli_main._effective_session(ctx, session)
    cli_main._announce_tracing()  # #168: status + fail-fast, like every LLM command

    async def _do():
        client = cli_main.make_client(session_name=session)
        from tg_messenger.agent.suggest import register_suggest_migrations

        storage = cli_main.make_storage(session)
        register_suggest_migrations(storage)
        # build the suggester (LLM stack) before the network — config errors show first
        suggester = cli_main.make_suggester(client, storage=storage)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            await storage.connect()
            try:
                await cli_main._ensure_dm_dialog(client, dialog_id)
                # #187: a status before the (billed) LLM call — to stderr so stdout stays
                # just the draft/receipt — so the user can tell it's working, not hung.
                click.echo("Learning style…" if do_learn else "Drafting a reply…", err=True)
                if do_learn:
                    profile = await suggester.learn(dialog_id)
                    click.echo(
                        f"learned style profile for {dialog_id} "
                        f"({len(profile.examples)} examples)."
                    )
                    return
                draft = await suggester.suggest(dialog_id)
                if do_send:
                    await client.send_text(dialog_id, draft)
                    click.echo("sent.")
                else:
                    click.echo(draft)
            finally:
                await storage.close()
        finally:
            await client.disconnect()

    cli_main._run(_do(), session=session)


@click.command("suggest-nudges")
@click.option("--after-hours", type=float, default=24.0,
              help="Only nudge dialogs read at least this many hours ago (default 24).")
@click.option("--session", default="default")
@click.pass_context
def suggest_nudges(ctx: click.Context, after_hours: float, session: str) -> None:
    """List DMs the contact READ but never replied to, with a nudge draft (#145).

    Human-in-the-loop: prints each candidate and a draft you review/send. Acts on
    the stored outbox read-receipts (recorded by `chat`/`heartbeat run`/`ghostwrite`);
    if you've never run a receipt watcher there will be nothing to nudge yet.
    """
    import time

    session = cli_main._effective_session(ctx, session)
    cli_main._announce_tracing()  # #168: status + fail-fast, like every LLM command

    async def _do():
        client = cli_main.make_client(session_name=session)
        from tg_messenger.agent.suggest import register_suggest_migrations

        storage = cli_main.make_storage(session)
        register_suggest_migrations(storage)
        suggester = cli_main.make_suggester(client, storage=storage)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            await storage.connect()
            try:
                # #187: status before the (billed) scan/LLM pass — to stderr, not stdout
                click.echo("Scanning dialogs for nudge candidates…", err=True)
                dms = await client.dialogs(dm_only=True)  # cached list — no per-dialog resolve
                candidates = await suggester.nudge_candidates(
                    [d.id for d in dms], now=time.time(), after_sec=after_hours * 3600,
                )
                if not candidates:
                    click.echo("No dialogs to nudge.")
                    return
                titles = {d.id: d.title for d in dms}
                for c in candidates:
                    did = c["dialog_id"]
                    title = titles.get(did) or str(did)
                    click.echo(f"{did} — {title}")
                    click.echo(f"  draft: {c['draft']}")
            finally:
                await storage.close()
        finally:
            await client.disconnect()

    cli_main._run(_do(), session=session)


COMMANDS = [agent, suggest, suggest_nudges]
