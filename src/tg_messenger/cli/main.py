"""click-based CLI over the shared core.

Commands: login, dialogs, read, send, listen, chat, serve, tui.
``make_client`` is the single seam tests patch to avoid real network.
"""

from __future__ import annotations

import asyncio
import os

import click

from tg_messenger.core.client import StandaloneTelegramClient
from tg_messenger.core.flood import HandledFloodWaitError


def make_client(**kwargs) -> StandaloneTelegramClient:
    api_id = int(os.environ.get("TG_API_ID", "0"))
    api_hash = os.environ.get("TG_API_HASH", "")
    return StandaloneTelegramClient(api_id=api_id, api_hash=api_hash, **kwargs)


def _run(coro):
    try:
        return asyncio.run(coro)
    except HandledFloodWaitError as exc:
        raise click.ClickException(
            f"Telegram flood wait {exc.wait_seconds}s — try again later."
        ) from exc


async def _with_client(session, fn):
    client = make_client(session_name=session)
    await client.connect()
    try:
        return await fn(client)
    finally:
        await client.disconnect()


@click.group()
def cli() -> None:
    """tg_messenger — chat in your Telegram DMs from the terminal."""


@cli.command()
@click.option("--session", default="default", help="Session name.")
@click.option("--phone", prompt=True, help="Phone number in international format.")
def login(session: str, phone: str) -> None:
    """Interactive login: phone -> code -> optional 2FA."""
    from tg_messenger.core.auth import LoginFlow

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        flow = LoginFlow(client._client)
        await flow.send_code(phone)
        code = click.prompt("Code")
        try:
            await flow.sign_in(code=code)
        except Exception:
            pw = click.prompt("2FA password", hide_input=True)
            await flow.check_password(pw)
        client.save_session()
        await client.disconnect()
        click.echo(f"Logged in, session '{session}' saved.")

    _run(_do())


@cli.command()
@click.option("--session", default="default")
def dialogs(session: str) -> None:
    """List your direct-message dialogs."""

    async def _do(client):
        return await client.dialogs(dm_only=True)

    items = _run(_with_client(session, _do))
    for d in items:
        unread = f" ({d.unread} unread)" if d.unread else ""
        uname = f" @{d.username}" if d.username else ""
        click.echo(f"{d.id}\t{d.title}{uname}{unread}")


@cli.command()
@click.argument("dialog_id", type=int)
@click.option("--limit", default=50)
@click.option("--session", default="default")
def read(dialog_id: int, limit: int, session: str) -> None:
    """Print the message history of a dialog."""

    async def _do(client):
        return await client.history(dialog_id, limit=limit)

    for m in _run(_with_client(session, _do)):
        who = "→" if m.out else "←"
        click.echo(f"{who} [{m.id}] {m.text or '<media>'}")


@cli.command()
@click.argument("dialog_id", type=int)
@click.argument("text", required=False)
@click.option("--file", "file_path", default=None, help="Send a file/photo instead of text.")
@click.option("--session", default="default")
def send(dialog_id: int, text: str | None, file_path: str | None, session: str) -> None:
    """Send a text message (or a file with --file)."""

    async def _do(client):
        if file_path:
            return await client.send_media(dialog_id, file_path, caption=text)
        return await client.send_text(dialog_id, text or "")

    _run(_with_client(session, _do))
    click.echo("sent.")


@cli.command()
@click.option("--session", default="default")
def listen(session: str) -> None:
    """Print incoming messages live."""

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        click.echo("Listening for incoming messages (Ctrl+C to stop)...")
        async for ev in client.listen():
            click.echo(f"← [{ev.dialog_id}] {ev.message.text or '<media>'}")

    _run(_do())


@cli.command()
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
def chat(dialog_id: int, session: str) -> None:
    """Interactive REPL: see incoming and send replies."""

    async def _do():
        client = make_client(session_name=session)
        await client.connect()

        async def printer():
            async for ev in client.listen():
                if ev.dialog_id == dialog_id:
                    click.echo(f"\n← {ev.message.text or '<media>'}")

        task = asyncio.create_task(printer())
        try:
            while True:
                line = await asyncio.to_thread(input, "> ")
                if line.strip():
                    await client.send_text(dialog_id, line)
        finally:
            task.cancel()
            await client.disconnect()

    _run(_do())


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8080)
@click.option("--session", default="default")
def serve(host: str, port: int, session: str) -> None:
    """Launch the web interface."""
    import uvicorn

    from tg_messenger.web.app import build_app

    uvicorn.run(build_app(session_name=session), host=host, port=port)


@cli.command()
@click.option("--session", default="default")
def tui(session: str) -> None:
    """Launch the TUI interface."""
    from tg_messenger.tui.app import MessengerTUI

    MessengerTUI(session_name=session).run()


if __name__ == "__main__":
    cli()
