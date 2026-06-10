"""click-based CLI over the shared core.

Commands: login, dialogs, read, send, listen, chat, serve, tui.
``make_client`` is the single seam tests patch to avoid real network.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import click
from telethon.errors import UnauthorizedError

from tg_messenger.core.client import StandaloneTelegramClient
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.logsetup import log_file_path, setup_logging

logger = logging.getLogger(__name__)


def _load_dotenv(path: Path | str = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file in cwd; real env always wins."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def make_client(**kwargs) -> StandaloneTelegramClient:
    api_id = int(os.environ.get("TG_API_ID", "0"))
    api_hash = os.environ.get("TG_API_HASH", "")
    return StandaloneTelegramClient(api_id=api_id, api_hash=api_hash, **kwargs)


_CODE_DELIVERY_HINTS = {
    "app": "Code sent to your Telegram app — check devices where you are already logged in.",
    "sms": "Code sent via SMS.",
    "call": "You will get a phone call with the code.",
}


def _delivery_message(delivery) -> str:
    msg = _CODE_DELIVERY_HINTS.get(delivery.kind, "Code sent — check your Telegram app and SMS.")
    if delivery.next_kind:
        msg += f" No code? Press Enter at the prompt to resend via {delivery.next_kind}"
        if delivery.timeout:
            msg += f" (available in ~{delivery.timeout}s)"
        msg += "."
    elif delivery.kind == "app":
        msg += (
            " This is the only delivery channel for this number — check the 'Telegram'"
            " service chat (sender 777000) on your logged-in devices."
            " Press Enter to send the code again."
        )
    return msg


def _login_hint(session: str = "default") -> str:
    hint = "Not logged in. Run: tg-messenger login"
    if session != "default":
        hint += f" --session {session}"
    return hint


def _run(coro, session: str = "default"):
    try:
        return asyncio.run(coro)
    except (click.ClickException, click.Abort):
        raise  # already user-friendly
    except HandledFloodWaitError as exc:
        logger.warning("%s: flood wait %ss", exc.operation, exc.wait_seconds)
        raise click.ClickException(
            f"Telegram flood wait {exc.wait_seconds}s — try again later."
        ) from exc
    except UnauthorizedError as exc:
        # session missing or revoked mid-command
        raise click.ClickException(_login_hint(session)) from exc
    except Exception as exc:
        logger.exception("command failed")
        raise click.ClickException(
            f"Unexpected error: {exc} — details logged to {log_file_path()}"
        ) from exc


async def _ensure_authorized(client, session: str) -> None:
    if not await client.is_authorized():
        raise click.ClickException(_login_hint(session))


async def _with_client(session, fn):
    client = make_client(session_name=session)
    await client.connect()
    try:
        await _ensure_authorized(client, session)
        return await fn(client)
    finally:
        await client.disconnect()


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Verbose (DEBUG) logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """tg_messenger — chat in your Telegram DMs from the terminal."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _load_dotenv()
    setup_logging(verbose=verbose)


@cli.command()
@click.option("--session", default="default", help="Session name.")
@click.option("--phone", prompt=True, help="Phone number in international format.")
def login(session: str, phone: str) -> None:
    """Interactive login: phone -> code -> optional 2FA."""
    from telethon.errors import RPCError, SendCodeUnavailableError, SessionPasswordNeededError

    from tg_messenger.core.auth import LoginFlow

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            flow = LoginFlow(client._client)
            try:
                delivery = await flow.send_code(phone)
            except RPCError as exc:
                raise click.ClickException(f"Could not send code: {exc}") from exc
            click.echo(_delivery_message(delivery))
            while True:
                code = click.prompt("Code (Enter = resend)", default="", show_default=False)
                if code.strip():
                    break
                try:
                    if delivery.next_kind:
                        delivery = await flow.resend_code()
                    else:
                        # no alternative channel (next_type=None): a fresh send_code
                        # repeats the same channel — like the web's "send again" button
                        delivery = await flow.send_code(phone)
                except SendCodeUnavailableError as exc:
                    # the original code is still valid — keep the login alive
                    logger.warning("code resend failed: %s", exc)
                    click.echo(
                        "Telegram won't resend right now — the previous code is still valid;"
                        " check the 'Telegram' service chat in your app.",
                        err=True,
                    )
                    continue
                except RPCError as exc:
                    logger.warning("code resend failed: %s", exc)
                    click.echo(f"Could not resend code: {type(exc).__name__}", err=True)
                    continue
                click.echo(_delivery_message(delivery))
            try:
                await flow.sign_in(code=code)
            except SessionPasswordNeededError:
                pw = click.prompt("2FA password", hide_input=True)
                await flow.check_password(pw)
            except RPCError as exc:
                raise click.ClickException(f"Sign-in failed: {exc}") from exc
            client.save_session()
            click.echo(f"Logged in, session '{session}' saved.")
        finally:
            await client.disconnect()

    _run(_do())


@cli.command()
@click.option("--session", default="default")
def dialogs(session: str) -> None:
    """List your direct-message dialogs."""

    async def _do(client):
        return await client.dialogs(dm_only=True)

    items = _run(_with_client(session, _do), session=session)
    for d in items:
        unread = f" ({d.unread} unread)" if d.unread else ""
        uname = f" @{d.username}" if d.username else ""
        click.echo(f"{d.id}\t{d.title}{uname}{unread}")


@cli.command()
@click.argument("dialog_id", type=int)
@click.option("--limit", default=50)
@click.option("--download", "download_dir", default=None,
              help="Download media of each message into this directory.")
@click.option("--session", default="default")
def read(dialog_id: int, limit: int, download_dir: str | None, session: str) -> None:
    """Print the message history of a dialog (and optionally download media)."""

    async def _do(client):
        if download_dir:
            os.makedirs(download_dir, exist_ok=True)
        messages = await client.history(dialog_id, limit=limit)
        for m in messages:
            who = "→" if m.out else "←"
            click.echo(f"{who} [{m.id}] {m.text or '<media>'}")
            if download_dir and m.media is not None and m.media.downloadable:
                dest = os.path.join(download_dir, f"{dialog_id}_{m.id}")
                saved = await client.download_message_media(dialog_id, m.id, dest)
                if saved:
                    click.echo(f"  saved: {saved}")

    _run(_with_client(session, _do), session=session)


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

    _run(_with_client(session, _do), session=session)
    click.echo("sent.")


@cli.command()
@click.option("--session", default="default")
def listen(session: str) -> None:
    """Print incoming messages live."""

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            click.echo("Listening for incoming messages (Ctrl+C to stop)...")
            async for ev in client.listen():
                click.echo(f"← [{ev.dialog_id}] {ev.message.text or '<media>'}")
        finally:
            await client.disconnect()

    try:
        _run(_do(), session=session)
    except KeyboardInterrupt:
        click.echo("stopped.")


@cli.command()
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
def chat(dialog_id: int, session: str) -> None:
    """Interactive REPL: see incoming and send replies."""

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            await _ensure_authorized(client, session)

            async def printer():
                async for ev in client.listen():
                    if ev.dialog_id == dialog_id:
                        click.echo(f"\n← {ev.message.text or '<media>'}")

            task = asyncio.create_task(printer())
            try:
                while True:
                    try:
                        line = await asyncio.to_thread(input, "> ")
                    except EOFError:
                        break
                    if line.strip():
                        await client.send_text(dialog_id, line)
            finally:
                task.cancel()
                results = await asyncio.gather(task, return_exceptions=True)
                for r in results:
                    # CancelledError is BaseException — a real failure only
                    if isinstance(r, Exception):
                        logger.error("chat listener failed", exc_info=r)
                        click.echo(f"listener failed: {r}", err=True)
        finally:
            await client.disconnect()

    try:
        _run(_do(), session=session)
    except KeyboardInterrupt:
        click.echo("stopped.")


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=lambda: int(os.environ.get("TG_WEB_PORT", "8090")), type=int)
@click.option("--session", default="default")
def serve(host: str, port: int, session: str) -> None:
    """Launch the web interface."""
    import uvicorn

    from tg_messenger.web.app import build_app

    # uvicorn's own banner goes to the file (log_config=None) — announce the URL here
    click.echo(f"Serving on http://{host}:{port} — Ctrl+C to stop.")
    uvicorn.run(build_app(session_name=session), host=host, port=port, log_config=None)


@cli.command()
@click.option("--session", default="default")
@click.pass_context
def tui(ctx: click.Context, session: str) -> None:
    """Launch the TUI interface."""
    from tg_messenger.tui.app import MessengerTUI

    # stderr handler would corrupt the alternate screen — file log only
    setup_logging(verbose=ctx.obj.get("verbose", False), console=False)
    MessengerTUI(session_name=session).run()


if __name__ == "__main__":
    cli()
