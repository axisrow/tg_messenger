"""CLI heartbeat commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import click

from tg_messenger.cli import main as cli_main


@click.group()
def heartbeat() -> None:
    """Scheduled pings: one-shot (--at) or recurring stored plans (--interval)."""


@heartbeat.command("plan")
@click.argument("peer", type=int)
@click.option("--at", "at", default=None, help="One-shot HH:MM (server-side scheduled send).")
@click.option("--interval", "interval_hours",
              type=click.FloatRange(min=0, min_open=True), default=None,
              help="Recurring: hours between pings (stores a plan).")
@click.option("--template", "templates", multiple=True, help="Ping text (repeatable).")
@click.option("--jitter", "jitter_minutes", type=click.FloatRange(min=0), default=0.0,
              help="Random +0..N minutes added to each interval.")
@click.option("--quiet-start", type=click.IntRange(0, 23), default=None,
              help="Quiet window open hour (local).")
@click.option("--quiet-end", type=click.IntRange(0, 23), default=None,
              help="Quiet window close hour (local).")
@click.option("--max-per-day", type=click.IntRange(min=1), default=1,
              help="Daily ping cap (recurring plans).")
@click.option("--session", default="default")
@click.pass_context
def heartbeat_plan(ctx: click.Context, peer: int, at: str | None, interval_hours: float | None,
                   templates: tuple[str, ...], jitter_minutes: float,
                   quiet_start: int | None, quiet_end: int | None,
                   max_per_day: int, session: str) -> None:
    """Schedule pings to PEER: --at for a one-shot native send, --interval for a stored plan."""
    session = cli_main._effective_session(ctx, session)
    if not templates:
        raise click.ClickException("at least one --template is required.")
    if at is None and interval_hours is None:
        raise click.ClickException("provide --at (one-shot) or --interval (recurring plan).")
    if at is not None and interval_hours is not None:
        raise click.ClickException("--at and --interval are mutually exclusive.")

    if at is not None:
        # one-shot: native server-side scheduled send (no stored plan)
        when = cli_main._parse_at(at)
        text = templates[0]

        async def _do_oneshot(client):
            await client.send_text(peer, text, schedule=when)

        cli_main._run(cli_main._with_client(session, _do_oneshot), session=session)
        click.echo(f"scheduled one-shot ping to {peer} at {when:%Y-%m-%d %H:%M}.")
        return

    # recurring: store a plan
    from tg_messenger.core.heartbeat import HeartbeatPlan, add_plan, register_heartbeat_migrations

    plan = HeartbeatPlan(
        peer=peer, templates=list(templates), interval_hours=interval_hours,
        jitter_minutes=jitter_minutes, quiet_start=quiet_start, quiet_end=quiet_end,
        max_per_day=max_per_day,
    )

    cli_main._run(
        cli_main._with_storage(session, register_heartbeat_migrations,
                      lambda storage: add_plan(storage, plan)),
        session=session,
    )
    click.echo(f"heartbeat plan stored for peer {peer} (every {interval_hours}h).")


@heartbeat.command("list")
@click.option("--session", default="default")
@click.pass_context
def heartbeat_list(ctx: click.Context, session: str) -> None:
    """List stored heartbeat plans."""
    from tg_messenger.core.heartbeat import list_plans, register_heartbeat_migrations
    session = cli_main._effective_session(ctx, session)

    plans = cli_main._run(
        cli_main._with_storage(session, register_heartbeat_migrations, list_plans),
        session=session,
    )
    if not plans:
        click.echo("No plans.")
        return
    for p in plans:
        state = "on" if p.enabled else "off"
        click.echo(f"{p.peer}\tevery {p.interval_hours}h\t[{state}]\t{p.templates}")


@heartbeat.command("remove")
@click.argument("peer", type=int)
@click.option("--session", default="default")
@click.pass_context
def heartbeat_remove(ctx: click.Context, peer: int, session: str) -> None:
    """Remove a stored heartbeat plan by PEER."""
    from tg_messenger.core.heartbeat import register_heartbeat_migrations, remove_plan
    session = cli_main._effective_session(ctx, session)

    cli_main._run(
        cli_main._with_storage(session, register_heartbeat_migrations,
                      lambda storage: remove_plan(storage, peer)),
        session=session,
    )
    click.echo(f"heartbeat plan removed for peer {peer}.")


@heartbeat.command("run")
@click.option("--session", default="default")
@click.pass_context
def heartbeat_run(ctx: click.Context, session: str) -> None:
    """Run the heartbeat scheduler: send stored plans' pings on schedule (Ctrl+C to stop)."""
    from tg_messenger.core.heartbeat import HeartbeatService, register_heartbeat_migrations
    session = cli_main._effective_session(ctx, session)
    cli_main._warn_if_send_rate_off()  # #50: loud when the global send cap is off
    cli_main._announce_tracing()  # #168: status + fail-fast, like every LLM command

    async def _do():
        client = cli_main.make_client(session_name=session)
        storage = cli_main.make_storage(session)
        register_heartbeat_migrations(storage)
        await storage.connect()
        await client.connect()
        # #146: the суфлёр hook — let the Suggester compose each ping's text when the [agent]
        # extra + TG_AGENT_MODEL are configured; None falls back to plan templates (heartbeat's
        # built-in fallback also catches a per-ping failure, so this is best-effort).
        suggester = cli_main.make_optional_suggester(client, session=session)
        text_provider = suggester.suggest if suggester is not None else None
        try:
            await cli_main._ensure_authorized(client, session)
            click.echo(
                "Heartbeat scheduler running — Ctrl+C to stop... "
                f"(suggester text {'on' if text_provider else 'off — using templates'})"
            )
            # сигнал «прочитал и молчит»: last_read пишется и здесь (#17/#19)
            async with cli_main._read_receipts_watcher(client, storage):
                await HeartbeatService(client, storage, text_provider=text_provider).run()
        finally:
            if suggester is not None:
                await suggester.close()
            await client.disconnect()
            await storage.close()

    cli_main._run_interruptible(_do(), session=session, flush_traces=True)


COMMANDS = [heartbeat]
