"""click-based CLI over the shared core.

Commands: login, dialogs, read, send, listen, chat, serve, tui.
``make_client`` is the single seam tests patch to avoid real network.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import click
from telethon.errors import UnauthorizedError

from tg_messenger.agent.config import langsmith_tracing_enabled
from tg_messenger.core.auth import LOGIN_HINT, SessionStore, validate_session_string
from tg_messenger.core.client import StandaloneTelegramClient
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.logsetup import log_file_path, setup_logging
from tg_messenger.core.models import message_line

logger = logging.getLogger(__name__)


def _parse_dotenv(path: Path | str = ".env") -> dict[str, str]:
    """Parse KEY=VALUE pairs from a .env file (quotes stripped); missing file -> {}."""
    path = Path(path)
    pairs: dict[str, str] = {}
    if not path.exists():
        return pairs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            pairs[key] = value.strip().strip("'\"")
    return pairs


def _load_dotenv(path: Path | str = ".env") -> None:
    """Load a .env from cwd into the environment; real env always wins."""
    for key, value in _parse_dotenv(path).items():
        os.environ.setdefault(key, value)


def _session_encryption_key() -> str | None:
    return os.environ.get("SESSION_ENCRYPTION_KEY") or None


def _session_store() -> SessionStore:
    return SessionStore(encryption_key=_session_encryption_key())


def make_client(**kwargs) -> StandaloneTelegramClient:
    api_id = int(os.environ.get("TG_API_ID", "0"))
    api_hash = os.environ.get("TG_API_HASH", "")
    # optional at-rest session encryption (shared SESSION_ENCRYPTION_KEY = SSO with the factory)
    kwargs.setdefault("encryption_key", _session_encryption_key())
    return StandaloneTelegramClient(api_id=api_id, api_hash=api_hash, **kwargs)


def make_agent_runner(client, *, notify_errors: bool = False):
    """Build the AI agent runner; the second seam tests patch (next to ``make_client``)."""
    try:
        from tg_messenger.agent.factory import build_orchestrator
    except ImportError as exc:
        raise click.ClickException(
            'Agent dependencies are not installed — pip install "tg-messenger[agent]"'
        ) from exc
    from tg_messenger.agent.config import AgentConfig
    from tg_messenger.agent.runner import AgentRunner

    try:
        cfg = AgentConfig.from_env()
        orchestrator = build_orchestrator(client, cfg)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    # конфиг, влияющий на поведение агента, виден на старте
    if cfg.vision_model:
        click.echo(f"Vision model: {cfg.vision_model}")
    if cfg.intents:
        click.echo("Custom intents: " + ", ".join(spec.name for spec in cfg.intents))
    return AgentRunner(client, orchestrator, config=cfg, notify_errors=notify_errors)


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
    hint = LOGIN_HINT
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
        raise click.ClickException(exc.user_message) from exc
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
    # the CLI reports its own errors via click — keep its log records off stderr
    setup_logging(verbose=verbose, console_skip_prefixes=("tg_messenger.cli",))


def _export_session(session: str) -> None:
    """Print the plaintext StringSession to stdout — full account access, never logged."""

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            if not await client.is_authorized():
                raise click.ClickException(LOGIN_HINT)
            return client.export_session_string()
        finally:
            await client.disconnect()

    session_string = _run(_do())
    click.echo("WARNING: this StringSession grants full access to your account — keep it secret.",
               err=True)
    click.echo(session_string)


def _import_session(session: str) -> None:
    """Read a StringSession from stdin (no echo), validate it, and save it under ``session``."""
    if sys.stdin.isatty():
        raw = click.prompt("Paste StringSession", hide_input=True).strip()
    else:
        raw = click.get_text_stream("stdin").read().strip()
    try:
        # validate before touching the client so garbage is rejected up front
        validate_session_string(raw)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _session_store().save(session, raw)
    click.echo(f"Session '{session}' imported and saved.")


@cli.command()
@click.option("--session", default="default", help="Session name.")
@click.option("--phone", default=None, help="Phone number in international format.")
@click.option("--export-session", "export_session", is_flag=True,
              help="Print the plaintext StringSession to stdout (full account access) and exit.")
@click.option("--import-session", "import_session", is_flag=True,
              help="Read a StringSession from stdin (no echo), validate and save it.")
def login(session: str, phone: str | None, export_session: bool, import_session: bool) -> None:
    """Interactive login: phone -> code -> optional 2FA.

    ``--export-session`` / ``--import-session`` move a session between machines or
    projects (SSO with tg_content_factory) without a fresh phone login.
    """
    from telethon.errors import RPCError, SendCodeUnavailableError, SessionPasswordNeededError

    from tg_messenger.core.auth import LoginFlow

    if export_session and import_session:
        raise click.ClickException("choose either --export-session or --import-session, not both")

    if export_session:
        _export_session(session)
        return
    if import_session:
        _import_session(session)
        return

    if not phone:
        phone = click.prompt("Phone number in international format")

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
@click.option("--groups", is_flag=True, help="List groups/channels/bots instead of DMs.")
def dialogs(session: str, groups: bool) -> None:
    """List your dialogs (DMs by default; --groups for groups/channels/bots)."""

    async def _do(client):
        return await (client.group_dialogs() if groups else client.dialogs())

    items = _run(_with_client(session, _do), session=session)
    for d in items:
        unread = f" ({d.unread} unread)" if d.unread else ""
        uname = f" @{d.username}" if d.username else ""
        kind = f" [{d.kind}]" if groups else ""  # одна вкладка смешивает виды — пометить
        click.echo(f"{d.id}\t{d.title}{uname}{kind}{unread}")


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
            click.echo(message_line(m))
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
@click.option("--session", default="default")
def watch(session: str) -> None:
    """Back up your deleted messages (e.g. removed by group moderator bots) to Saved Messages."""
    from tg_messenger.core.watch import DeletionWatcher

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            click.echo("Watching for deletions of your messages (Ctrl+C to stop)...")
            await DeletionWatcher(client, echo=click.echo).run()
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
@click.option("--session", default="default")
@click.option("--notify-errors", is_flag=True,
              help="Reply with a short notice when processing a message fails.")
def agent(session: str, notify_errors: bool) -> None:
    """AI assistant: auto-reply to incoming DMs, route tasks to a deep agent."""
    # langchain/langgraph трассируются в LangSmith сами по LANGSMITH_* env —
    # здесь только fail-fast (включено без ключа) и видимый статус
    try:
        if langsmith_tracing_enabled():
            project = os.environ.get("LANGSMITH_PROJECT", "default")
            click.echo(f"LangSmith tracing: on (project={project})")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _do():
        client = make_client(session_name=session)
        # конфиг и LLM-стек собираем до сети — ошибки настроек видны сразу
        runner = make_agent_runner(client, notify_errors=notify_errors)
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            click.echo("Agent is listening for incoming messages (Ctrl+C to stop)...")
            await runner.run()
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
    try:
        import uvicorn

        from tg_messenger.web.app import build_app
    except ImportError as exc:
        # base install omits the web stack — point at the extra instead of a raw ImportError
        raise click.ClickException("web UI requires: pip install 'tg-messenger[web]'") from exc

    # uvicorn's own banner goes to the file (log_config=None) — announce the URL here
    click.echo(f"Serving on http://{host}:{port} — Ctrl+C to stop.")
    uvicorn.run(build_app(session_name=session), host=host, port=port, log_config=None)


@cli.command()
@click.option("--session", default="default")
@click.pass_context
def tui(ctx: click.Context, session: str) -> None:
    """Launch the TUI interface."""
    try:
        from tg_messenger.tui.app import MessengerTUI
    except ImportError as exc:
        raise click.ClickException("TUI requires: pip install 'tg-messenger[tui]'") from exc

    # stderr handler would corrupt the alternate screen — file log only
    setup_logging(verbose=ctx.obj["verbose"], console=False)
    MessengerTUI(session_name=session).run()


if __name__ == "__main__":
    cli()
