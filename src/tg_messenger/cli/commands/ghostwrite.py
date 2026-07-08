"""CLI ghostwrite commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import click

from tg_messenger.cli import main as cli_main


@click.command()
@click.option("--session", default="default")
@click.option("--enforce", is_flag=True,
              help="Actually send replies (default is dry-run: log only).")
@click.pass_context
def ghostwrite(ctx: click.Context, session: str, enforce: bool) -> None:
    """Auto-reply in your style in explicitly enabled DMs (dry-run unless --enforce).

    Destructive (sends from your account) and per-dialog opt-in only — enable dialogs with
    `ghostwrite-dialogs enable`. Safeties: hourly rate cap, auto-pause when you reply by
    hand, and `ghostwrite-dialogs pause-all` as a kill switch.
    """
    from tg_messenger.agent.ghostwrite import (
        GhostwriteEngine,
        list_enabled,
        register_ghostwrite_migrations,
    )
    from tg_messenger.agent.suggest import register_suggest_migrations
    session = cli_main._effective_session(ctx, session)
    cli_main._warn_if_send_rate_off()  # #50: loud when the global send cap is off
    cli_main._announce_tracing()  # #168: status + fail-fast, like every LLM command

    async def _do():
        client = cli_main.make_client(session_name=session)
        storage = cli_main.make_storage(session)
        register_ghostwrite_migrations(storage)
        register_suggest_migrations(storage)  # the suggester reads style profiles from here
        # build the suggester (LLM stack) before the network — config errors show first
        suggester = cli_main.make_suggester(client, storage=storage)
        await storage.connect()
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            enabled = await list_enabled(storage)
            if enabled:
                click.echo("Ghostwrite enabled for dialogs: "
                           + ", ".join(str(d) for d in enabled))
            else:
                # #187: U+FE0E text-presentation glyph; DIALOG_ID (not PEER) for consistency
                click.echo("⚠︎ no dialogs enabled — use 'ghostwrite-dialogs enable DIALOG_ID'",
                           err=True)
            engine = GhostwriteEngine(client, suggester, storage, enforce=enforce)
            mode = "ENFORCING" if enforce else "dry-run"
            click.echo(f"Ghostwriting ({mode}) — Ctrl+C to stop...")
            # цикл 98 (#17): рядом с движком пишем read-receipts (last_read
            # per dialog, kv) — сигнал «прочитал и молчит» для #18/#19.
            async with cli_main._read_receipts_watcher(client, storage):
                await engine.run()
        finally:
            await client.disconnect()
            await storage.close()

    cli_main._run_interruptible(_do(), session=session, flush_traces=True)


@click.group("ghostwrite-dialogs")
def ghostwrite_dialogs() -> None:
    """Manage the ghostwrite per-dialog allowlist (enable / disable / list / pause)."""


@ghostwrite_dialogs.command("enable")
@click.argument("dialog_id")
@click.option("--session", default="default")
@click.pass_context
def ghostwrite_dialogs_enable(ctx: click.Context, dialog_id: str, session: str) -> None:
    """Turn ghostwrite ON for a DM by DIALOG_ID (numeric). '*' is rejected by design.

    #187: one term (DIALOG_ID) across the arg, the error and the success line.
    """
    if dialog_id.strip() == "*":
        raise click.ClickException(
            "'*' (everyone) is not allowed for ghostwrite — enable each dialog explicitly."
        )
    try:
        dialog_id_int = int(dialog_id)
    except ValueError as exc:
        raise click.ClickException(
            f"DIALOG_ID must be a numeric dialog id, got {dialog_id!r}"
        ) from exc
    from tg_messenger.agent.ghostwrite import enable_dialog, register_ghostwrite_migrations
    session = cli_main._effective_session(ctx, session)

    cli_main._run(
        cli_main._with_storage(session, register_ghostwrite_migrations,
                      lambda storage: enable_dialog(storage, dialog_id_int)),
        session=session,
    )
    click.echo(f"ghostwrite enabled for dialog {dialog_id_int}.")


@ghostwrite_dialogs.command("disable")
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
@click.pass_context
def ghostwrite_dialogs_disable(ctx: click.Context, dialog_id: int, session: str) -> None:
    """Turn ghostwrite OFF for a dialog."""
    from tg_messenger.agent.ghostwrite import disable_dialog, register_ghostwrite_migrations
    session = cli_main._effective_session(ctx, session)

    cli_main._run(
        cli_main._with_storage(session, register_ghostwrite_migrations,
                      lambda storage: disable_dialog(storage, dialog_id)),
        session=session,
    )
    click.echo(f"ghostwrite disabled for dialog {dialog_id}.")


@ghostwrite_dialogs.command("list")
@click.option("--session", default="default")
@click.pass_context
def ghostwrite_dialogs_list(ctx: click.Context, session: str) -> None:
    """List dialogs where ghostwrite is enabled."""
    from tg_messenger.agent.ghostwrite import list_enabled, register_ghostwrite_migrations
    session = cli_main._effective_session(ctx, session)

    enabled = cli_main._run(
        cli_main._with_storage(session, register_ghostwrite_migrations, list_enabled),
        session=session,
    )
    if not enabled:
        click.echo("No dialogs enabled.")
        return
    for dialog_id in enabled:
        click.echo(str(dialog_id))


@ghostwrite_dialogs.command("pause-all")
@click.option("--session", default="default")
@click.pass_context
def ghostwrite_dialogs_pause_all(ctx: click.Context, session: str) -> None:
    """Kill switch: pause every enabled dialog (resume one with 'resume PEER')."""
    from tg_messenger.agent.ghostwrite import pause_all, register_ghostwrite_migrations
    session = cli_main._effective_session(ctx, session)

    cli_main._run(
        cli_main._with_storage(session, register_ghostwrite_migrations, pause_all),
        session=session,
    )
    click.echo("ghostwrite paused for all enabled dialogs.")


@ghostwrite_dialogs.command("resume")
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
@click.pass_context
def ghostwrite_dialogs_resume(ctx: click.Context, dialog_id: int, session: str) -> None:
    """Clear a dialog's pause (re-arm it after pause-all or an auto-pause)."""
    from tg_messenger.agent.ghostwrite import register_ghostwrite_migrations, resume_dialog
    session = cli_main._effective_session(ctx, session)

    cli_main._run(
        cli_main._with_storage(session, register_ghostwrite_migrations,
                      lambda storage: resume_dialog(storage, dialog_id)),
        session=session,
    )
    click.echo(f"ghostwrite resumed for dialog {dialog_id}.")


COMMANDS = [ghostwrite, ghostwrite_dialogs]
