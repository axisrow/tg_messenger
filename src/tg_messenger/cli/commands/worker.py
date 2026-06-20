"""CLI worker commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import asyncio
import os

import click

from tg_messenger.cli import main as cli_main


@click.command()
@click.option("--session", default="default")
@click.option("--factory-url", "factory_url", default=lambda: os.environ.get("TG_FACTORY_URL"),
              help="tg_content_factory base URL (env TG_FACTORY_URL).")
@click.option("--types", "types", default="dm_reply,chat_answer",
              help="Comma-separated task types to claim.")
@click.option("--interval", "interval", type=float, default=5.0,
              help="Seconds to wait between empty polls.")
@click.pass_context
def worker(ctx: click.Context, session: str, factory_url: str | None, types: str, interval: float) -> None:
    """Run a worker for tg_content_factory: claim tasks, execute them, report back.

    The factory is the agent's memory + search; this worker is its hands —
    dm_reply/chat_answer send messages, fetch_history/fetch_dialogs read them.
    """
    session = cli_main._effective_session(ctx, session)
    cli_main._warn_if_send_rate_off()  # #50: loud when the global send cap is off
    cli_main._announce_tracing()  # #168: status + fail-fast, like every LLM command
    if not factory_url:
        raise click.ClickException(
            "--factory-url is required (or set TG_FACTORY_URL)."
        )
    try:
        from tg_messenger.interop.worker import Worker
    except ImportError as exc:
        raise click.ClickException(
            "interop requires: pip install 'tg-messenger[interop]'"
        ) from exc

    task_types = [t.strip() for t in types.split(",") if t.strip()]
    factory = cli_main.make_factory_client(
        base_url=factory_url, password=os.environ.get("TG_FACTORY_PASSWORD")
    )

    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _do():
        client = cli_main.make_client(session_name=session)
        await client.connect()
        try:
            await cli_main._ensure_authorized(client, session)
            agent = cli_main.make_worker_agent(client)
            click.echo(
                f"Worker polling {factory_url} for {', '.join(task_types)} "
                f"(prompt tasks {'on' if agent is not None else 'off'}; Ctrl+C to stop)..."
            )
            # #125-A6: the factory owns its httpx.AsyncClient (http=None) — close it via
            # `async with` so the process doesn't leak the connection / emit "Unclosed
            # AsyncClient" ResourceWarning on shutdown (mirrors agent/tools.py).
            async with factory:
                await Worker(
                    client, factory, types=task_types, sleep=_sleep, idle_sleep=interval,
                    agent=agent,
                ).run()
        finally:
            await client.disconnect()

    cli_main._run_interruptible(_do(), session=session, flush_traces=True)


COMMANDS = [worker]
