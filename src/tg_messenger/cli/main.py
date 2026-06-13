"""click-based CLI over the shared core.

Commands: login, dialogs, read, send, listen, chat, serve, tui.
``make_client`` is the single seam tests patch to avoid real network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import click
from click.core import ParameterSource
from telethon.errors import UnauthorizedError

from tg_messenger.agent.config import langsmith_tracing_enabled
from tg_messenger.core.auth import LOGIN_HINT, session_store_from_env, validate_session_string
from tg_messenger.core.client import (
    MessageDeleteValidationError,
    SendForbiddenError,
    client_from_env,
    is_channel_or_megagroup_id,
)
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.logsetup import log_file_path, setup_logging
from tg_messenger.core.models import message_line

logger = logging.getLogger(__name__)
CHAT_OUTBOUND_TIMEOUT_SECONDS = 20


def _reaction_emoticon(emoticon: str | None) -> str:
    return emoticon if emoticon is not None else "<custom>"


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


def _session_store():
    """SessionStore over the configured session dir (env override for tests/ops)."""
    return session_store_from_env()


def make_client(**kwargs):
    """Build the client from env (core ``client_from_env``); the seam tests patch."""
    return client_from_env(**kwargs)


def _warn_if_send_rate_off() -> None:
    """Warn once if the outgoing rate limit is explicitly OFF.

    The safe default is 20/min; background senders (agent/heartbeat/worker/ghostwrite/
    moderate) are capped unless the operator deliberately sets ``TG_SEND_RATE=0``.

    A non-numeric value is surfaced separately (not folded into "off"): ``client_from_env``
    would raise on it, so warning "limit is off" there would hide the real misconfiguration.
    """
    raw_env = os.environ.get("TG_SEND_RATE")
    if raw_env is None:
        return
    raw = raw_env or ""
    try:
        rate = float(raw or 20)
    except ValueError:
        logger.warning(
            "TG_SEND_RATE=%r is not a number — the outgoing rate limit cannot be parsed; "
            "set a numeric value (e.g. TG_SEND_RATE=20) or unset it",
            raw,
        )
        return
    if rate <= 0:
        logger.warning(
            "outgoing rate limit is OFF (TG_SEND_RATE=0) — a high send rate can get "
            "the account banned; unset TG_SEND_RATE or set TG_SEND_RATE=20 to enable "
            "the safe default cap"
        )


def make_factory_client(*, base_url: str, password: str | None = None):
    """Build the interop FactoryClient; the seam worker tests patch.

    Lazy import: httpx (the [interop] extra) lives only inside interop/ — a base
    install without the extra raises a friendly ClickException pointing at it.
    """
    try:
        from tg_messenger.interop.factory_client import FactoryClient
    except ImportError as exc:
        raise click.ClickException(
            "interop requires: pip install 'tg-messenger[interop]'"
        ) from exc
    return FactoryClient(base_url=base_url, password=password or "")


def make_storage(profile: str = "default"):
    """Build the per-profile SQLite Storage; the seam moderation tests patch."""
    from tg_messenger.core.storage import Storage, default_db_path

    return Storage(default_db_path(profile))


async def _ensure_dm_dialog(client, dialog_id: int) -> None:
    dm_ids = {dialog.id for dialog in await client.dialogs()}
    if dialog_id not in dm_ids:
        raise click.ClickException("suggest is available for DM dialogs only.")


def make_worker_agent(client):
    """Optional agent for the worker's prompt tasks; the seam worker tests patch.

    Best-effort: without the [agent] extra or TG_AGENT_MODEL it returns None
    (the worker then fails prompt tasks with a clear message) — the worker's
    fetch/dm_reply tasks must keep working on a plain [interop] install.
    """
    try:
        from tg_messenger.agent.factory import build_orchestrator
    except ImportError:
        logger.info("worker: [agent] extra not installed — prompt tasks disabled")
        return None
    from tg_messenger.agent.config import AgentConfig

    try:
        cfg = AgentConfig.from_env(require_allowlist=False)
        return build_orchestrator(client, cfg)
    except ValueError as exc:
        logger.info("worker: agent not configured (%s) — prompt tasks disabled", exc)
        return None


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


def make_suggester(client, *, storage=None):
    """Build the reply Suggester (#17); the seam suggest tests patch.

    Like make_agent_runner: needs the [agent] extra + TG_AGENT_MODEL (fail-fast).
    """
    try:
        from tg_messenger.agent.factory import build_suggester
    except ImportError as exc:
        raise click.ClickException(
            'Agent dependencies are not installed — pip install "tg-messenger[agent]"'
        ) from exc
    from tg_messenger.agent.config import AgentConfig

    try:
        cfg = AgentConfig.from_env(require_allowlist=False)
        return build_suggester(client, cfg, storage=storage)
    except (ValueError, ImportError) as exc:
        raise click.ClickException(str(exc)) from exc


class _StorageBackedSuggester:
    """Lazily connect suggester storage for long-running web/TUI processes."""

    def __init__(self, suggester, storage):
        self._suggester = suggester
        self._storage = storage
        self._connected = False
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        async with self._lock:
            if not self._connected:
                await self._storage.connect()
                self._connected = True

    async def suggest(self, dialog_id: int) -> str:
        await self._ensure_connected()
        return await self._suggester.suggest(dialog_id)

    async def learn(self, dialog_id: int):
        await self._ensure_connected()
        return await self._suggester.learn(dialog_id)

    async def close(self) -> None:
        if self._connected:
            await self._storage.close()
            self._connected = False


def make_optional_suggester(client, *, session: str = "default"):
    """Best-effort production suggester for web/TUI.

    Suggest is an optional [agent] feature. A missing extra or model should disable
    the draft endpoint/strip, not prevent the web UI or TUI from starting.
    """
    try:
        from tg_messenger.agent.suggest import register_suggest_migrations

        storage = make_storage(session)
        register_suggest_migrations(storage)
        suggester = make_suggester(client, storage=storage)
        return _StorageBackedSuggester(suggester, storage)
    except (click.ClickException, ImportError) as exc:
        logger.warning("reply suggester disabled: %s", exc)
        return None


def make_message_store(client, *, session: str = "default"):
    """Build the persistent message store and its shared Storage."""
    from tg_messenger.core.message_store import MessageStore, register_message_store_migrations

    storage = make_storage(session)
    register_message_store_migrations(storage)
    return MessageStore(client=client, storage=storage), storage


def make_optional_translator(storage):
    """Best-effort cached translator over the shared message-store Storage."""
    try:
        from tg_messenger.agent.factory import build_translator
        from tg_messenger.agent.translate import translate_model_from_env
    except ImportError as exc:
        logger.warning("message translator disabled: %s", exc)
        return None
    model_name = translate_model_from_env()
    if not model_name:
        logger.info("message translator disabled: TG_TRANSLATE_MODEL/TG_AGENT_MODEL is unset")
        return None
    try:
        return build_translator(storage, model_name)
    except Exception as exc:
        logger.warning("message translator disabled: %s", exc)
        return None


def make_optional_outbound(store, storage):
    """Best-effort outbound translator, sharing the message-store Storage."""
    if os.environ.get("TG_OUTBOUND", "on").strip().lower() in {"0", "false", "off", "no"}:
        logger.info("outbound translator disabled: TG_OUTBOUND=off")
        return None
    try:
        from tg_messenger.agent.factory import build_outbound
        from tg_messenger.agent.suggest import register_suggest_migrations
        from tg_messenger.agent.translate import translate_model_from_env
    except ImportError as exc:
        logger.warning("outbound translator disabled: %s", exc)
        return None
    model_name = translate_model_from_env()
    if not model_name:
        logger.warning("outbound translator disabled: TG_TRANSLATE_MODEL/TG_AGENT_MODEL is unset")
        return None
    try:
        register_suggest_migrations(storage)
        return build_outbound(store, storage, model_name)
    except Exception as exc:
        logger.warning("outbound translator disabled: %s", exc)
        return None


@dataclass
class TuiDeps:
    """The full dependency set the TUI needs, built for one chosen profile (#52)."""

    client: object
    session_name: str
    suggester: object
    store: object
    translator: object
    outbound: object


def make_tui_deps(profile: str, *, log_kwargs: dict) -> TuiDeps:
    """Build the whole TUI dependency set for ``profile`` (#52 point 2).

    Used after the in-app ProfileScreen picks a profile, so the `tui` command no longer
    has to resolve the profile via a CLI menu before constructing the TUI. Re-inits the
    per-profile log file first (point 3) — idempotent, ``console=False`` carried in
    ``log_kwargs`` so the alternate screen stays intact.
    """
    setup_logging(profile=profile, **log_kwargs)
    client = make_client(session_name=profile)
    suggester = make_optional_suggester(client, session=profile)
    store, storage = make_message_store(client, session=profile)
    translator = make_optional_translator(storage)
    outbound = make_optional_outbound(store, storage)
    return TuiDeps(
        client=client,
        session_name=profile,
        suggester=suggester,
        store=store,
        translator=translator,
        outbound=outbound,
    )


def _print_message_with_translation(message) -> None:
    click.echo(message_line(message))
    if getattr(message, "translated_text", None):
        click.echo(f"  ↳ {message.translated_text}")


async def _maybe_translate_history(translator, dialog_id: int, messages):
    if translator is None:
        return list(messages)
    return await translator.translate_history(dialog_id, messages)


async def _maybe_translate_message(translator, message):
    if translator is None:
        return message
    return await translator.translate_message(message)


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
    except MessageDeleteValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    except SendForbiddenError as exc:
        logger.warning("send rejected (rights): %s", exc)
        raise click.ClickException(
            "This chat is read-only — you don't have permission to post here."
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
    session = _effective_session(click.get_current_context(silent=True), session)
    client = make_client(session_name=session)
    await client.connect()
    try:
        await _ensure_authorized(client, session)
        return await fn(client)
    finally:
        await client.disconnect()


async def _with_storage(session, register_fn, fn):
    """Open the per-profile storage with ``register_fn`` migrations, run ``fn``, close."""
    storage = make_storage(session)
    register_fn(storage)
    await storage.connect()
    try:
        return await fn(storage)
    finally:
        await storage.close()


def _run_interruptible(coro, session: str = "default") -> None:
    """``_run`` + the long-running commands' Ctrl+C contract (prints "stopped.")."""
    try:
        _run(coro, session=session)
    except KeyboardInterrupt:
        click.echo("stopped.")


@contextlib.asynccontextmanager
async def _read_receipts_watcher(client, storage):
    """Суфлёрская фиксация read-receipts (#17, kv) рядом с долгоживущим движком.

    Фоновая задача + cancel в finally, не gather: KeyboardInterrupt из движка
    не должен бросать вечный watcher недобитым.
    """
    from tg_messenger.agent.suggest import watch_read_receipts

    watcher = asyncio.create_task(watch_read_receipts(client, storage))
    try:
        yield
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def _is_interactive() -> bool:
    """True when a human can answer a prompt (stdin is a tty)."""
    return sys.stdin.isatty()


def _resolve_profile(session: str) -> str:
    """Pick the profile when ``--session/--profile`` was left at its default.

    0 or 1 saved profile → keep ``session`` (``default`` / the only one). With >1 and
    an interactive stdin, prompt a numbered menu; non-interactive → a clear error.
    An explicit non-default ``session`` short-circuits all of this.
    """
    if session != "default":
        return session
    profiles = _session_store().list_profiles()
    if len(profiles) <= 1:
        return profiles[0] if profiles else session
    if not _is_interactive():
        raise click.ClickException(
            f"multiple profiles ({', '.join(profiles)}) — pass --profile NAME"
        )
    for i, name in enumerate(profiles, 1):
        click.echo(f"  {i}. {name}")
    while True:
        choice = click.prompt("Select profile", type=int)
        if 1 <= choice <= len(profiles):
            return profiles[choice - 1]
        click.echo("out of range", err=True)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Verbose (DEBUG) logging.")
@click.option("--profile", default=None, help="Account profile (session name) to use.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, profile: str | None) -> None:
    """tg_messenger — chat in your Telegram DMs from the terminal."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["profile"] = profile
    _load_dotenv()
    # the CLI reports its own errors via click — keep its log records off stderr.
    # The final profile may still come from a menu; we remember the logging kwargs so
    # a menu-resolved profile can re-init the log file (#52, see _effective_session).
    ctx.obj["_log_kwargs"] = {
        "verbose": verbose,
        "console_skip_prefixes": ("tg_messenger.cli",),
    }
    setup_logging(profile=profile, **ctx.obj["_log_kwargs"])


def _effective_session(ctx: click.Context | None, session: str) -> str:
    """Combine the global --profile with a command's --session, then resolve a menu."""
    profile = ctx.obj.get("profile") if ctx is not None and ctx.obj else None
    if profile:
        return profile
    if (
        ctx is not None
        and ctx.get_parameter_source("session") == ParameterSource.COMMANDLINE
    ):
        return session
    resolved = _resolve_profile(session)
    # #52: a non-default profile picked via the interactive menu (no explicit --profile)
    # must isolate its log file too — re-init logging with the chosen profile. cli() only
    # set up logging with the global --profile (None here), so the menu pick is invisible
    # to the log file otherwise. Idempotent (setup_logging replaces marked handlers).
    if resolved != session and resolved != "default" and ctx is not None and ctx.obj:
        log_kwargs = ctx.obj.get("_log_kwargs", {})
        setup_logging(profile=resolved, **log_kwargs)
    return resolved


def _export_session(session: str) -> None:
    """Print the plaintext StringSession to stdout — full account access, never logged."""

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            if not await client.is_authorized():
                raise click.ClickException(_login_hint(session))
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
    if not raw:
        raise click.ClickException("invalid StringSession")
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
@click.pass_context
def login(ctx: click.Context, session: str, phone: str | None,
          export_session: bool, import_session: bool) -> None:
    """Interactive login: phone -> code -> optional 2FA.

    ``--export-session`` / ``--import-session`` move a session between machines or
    projects (SSO with tg_content_factory) without a fresh phone login.
    """
    from telethon.errors import RPCError, SendCodeUnavailableError, SessionPasswordNeededError

    from tg_messenger.core.auth import LoginFlow

    # login names a (possibly new) profile — honour the global --profile, but never
    # pop a selection menu here (you're creating/replacing this one).
    if ctx.obj and ctx.obj.get("profile"):
        session = ctx.obj["profile"]

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


@cli.group(invoke_without_command=True)
@click.pass_context
def profiles(ctx: click.Context) -> None:
    """List saved account profiles (sessions on disk)."""
    if ctx.invoked_subcommand is not None:
        return
    store = _session_store()
    names = store.list_profiles()
    if not names:
        click.echo("No profiles yet — run: tg-messenger --profile NAME login")
        return
    # #52: mark each profile valid (✓, session file present and parseable) or broken (✗).
    # Validity is a local check — no network call.
    for name in names:
        marker = "✓" if store.is_valid_profile(name) else "✗"
        click.echo(f"{name} {marker}")


@profiles.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def profiles_remove(name: str, yes: bool) -> None:
    """Delete a profile's session file WITHOUT logging out (for dead sessions).

    To also invalidate the session on Telegram's side use `tg-messenger
    --profile NAME logout`.
    """
    store = _session_store()
    if not store.path_for(name).is_file():
        raise click.ClickException(f"no saved session for profile {name!r}.")
    if not yes:
        click.confirm(f"Delete the session file for profile {name!r}?", abort=True)
    store.delete(name)
    click.echo(f"profile {name!r} removed.")


@cli.command()
@click.option("--session", default="default")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def logout(ctx: click.Context, session: str, yes: bool) -> None:
    """Log out of Telegram (best-effort) and delete the profile's session file.

    The Telegram-side log_out invalidates the auth key on the server; a dead or
    revoked session does not block the local file removal.
    """
    session = _effective_session(ctx, session)
    store = _session_store()
    if not store.path_for(session).is_file():
        raise click.ClickException(f"no saved session for profile {session!r}.")
    if not yes:
        click.confirm(
            f"Log out profile {session!r} from Telegram and delete its session file?",
            abort=True,
        )

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            await client.log_out()
        finally:
            await client.disconnect()

    try:
        _run(_do(), session=session)
        click.echo("logged out of Telegram.")
    except Exception as exc:
        # best-effort by design: a dead session must not keep its file alive
        logger.warning("telegram log_out failed for profile %r: %s", session, exc)
        click.echo(f"⚠ Telegram log_out failed ({exc}) — deleting the local session anyway.",
                   err=True)
    store.delete(session)
    click.echo(f"session file removed for profile {session!r}.")


@cli.command()
@click.option("--session", default="default")
@click.option("--groups", is_flag=True, help="List groups/channels/bots instead of DMs.")
@click.option("--find", "find", default=None,
              help="Filter dialogs locally by title/username/id (no network).")
def dialogs(session: str, groups: bool, find: str | None) -> None:
    """List your dialogs (DMs by default; --groups for groups/channels/bots).

    ``--find`` filters the already-fetched list locally (title substring, username
    with/without @, or id) — no extra request.
    """
    from tg_messenger.core.search import filter_dialogs

    async def _do(client):
        return await (client.group_dialogs() if groups else client.dialogs())

    items = _run(_with_client(session, _do), session=session)
    if find is not None:
        items = filter_dialogs(items, find)
    for d in items:
        unread = f" ({d.unread} unread)" if d.unread else ""
        uname = f" @{d.username}" if d.username else ""
        kind = f" [{d.kind}]" if groups else ""  # одна вкладка смешивает виды — пометить
        click.echo(f"{d.id}\t{d.title}{uname}{kind}{unread}")


@cli.command()
@click.argument("dialog_id", type=int)
@click.argument("query")
@click.option("--limit", default=20, help="Max number of messages to return.")
@click.option("--session", default="default")
def search(dialog_id: int, query: str, limit: int, session: str) -> None:
    """Search messages inside a dialog (Telegram's own server-side search)."""

    async def _do(client):
        return await client.search_messages(dialog_id, query, limit=limit)

    messages = _run(_with_client(session, _do), session=session)
    for m in messages:
        click.echo(message_line(m))


@cli.command()
@click.argument("dialog_id", type=int)
@click.option("--limit", default=50)
@click.option("--download", "download_dir", default=None,
              help="Download media of each message into this directory.")
@click.option("--session", default="default")
def read(dialog_id: int, limit: int, download_dir: str | None, session: str) -> None:
    """Print the message history of a dialog (and optionally download media)."""

    async def _do(client):
        store, storage = make_message_store(client, session=session)
        translator = make_optional_translator(storage)
        if download_dir:
            os.makedirs(download_dir, exist_ok=True)
        try:
            messages = await store.history(dialog_id, limit=limit)
            messages = await _maybe_translate_history(translator, dialog_id, messages)
            for m in messages:
                _print_message_with_translation(m)
                if download_dir and m.media is not None and m.media.downloadable:
                    dest = os.path.join(download_dir, f"{dialog_id}_{m.id}")
                    saved = await client.download_message_media(dialog_id, m.id, dest)
                    if saved:
                        click.echo(f"  saved: {saved}")
        finally:
            await store.close()

    _run(_with_client(session, _do), session=session)


@cli.command()
@click.argument("code", required=False)
@click.option("--clear", "clear", is_flag=True, help="Clear the stored language override.")
@click.option("--session", default="default")
@click.pass_context
def lang(ctx: click.Context, code: str | None, clear: bool, session: str) -> None:
    """Show or set the user's target language for cached message translation."""
    from tg_messenger.agent.translate import USER_LANG_KEY, get_user_lang, set_user_lang
    from tg_messenger.core.languages import validate_supported_lang_code

    if clear and code is not None:
        raise click.ClickException("CODE and --clear are mutually exclusive")
    language_code = None
    if code is not None:
        try:
            language_code = validate_supported_lang_code(code)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    session = _effective_session(ctx, session)

    async def _do(storage):
        if clear:
            await set_user_lang(storage, None)
            click.echo("language override cleared.")
            return
        if language_code is not None:
            await set_user_lang(storage, language_code)
            click.echo(f"language set to {language_code}.")
            return
        stored = await storage.get_value(USER_LANG_KEY)
        effective = await get_user_lang(storage)
        if stored:
            source = "kv"
        elif os.environ.get("TG_USER_LANG"):
            source = "env"
        else:
            source = "unset"
        click.echo(f"{effective or 'unset'}\t{source}")

    _run(_with_storage(session, lambda storage: None, _do), session=session)


@cli.command("dialog-lang")
@click.argument("dialog_id", type=int)
@click.argument("code", required=False)
@click.option("--auto", "auto", is_flag=True, help="Clear manual language override.")
@click.option("--on", "turn_on", is_flag=True, help="Enable outbound translation for this dialog.")
@click.option("--off", "turn_off", is_flag=True, help="Disable outbound translation for this dialog.")
@click.option("--session", default="default")
@click.pass_context
def dialog_lang(
    ctx: click.Context,
    dialog_id: int,
    code: str | None,
    auto: bool,
    turn_on: bool,
    turn_off: bool,
    session: str,
) -> None:
    """Show or override a dialog language and outbound translation flag."""
    from tg_messenger.agent.outbound import (
        get_dialog_lang,
        is_outbound_enabled,
        set_dialog_lang,
        set_outbound_enabled,
    )
    from tg_messenger.core.languages import validate_supported_lang_code

    if sum([code is not None, auto]) > 1:
        raise click.ClickException("CODE and --auto are mutually exclusive")
    if turn_on and turn_off:
        raise click.ClickException("--on and --off are mutually exclusive")
    language_code = None
    if code is not None:
        try:
            language_code = validate_supported_lang_code(code)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    session = _effective_session(ctx, session)

    async def _do(storage):
        if language_code is not None:
            await set_dialog_lang(storage, dialog_id, language_code, source="manual")
        if auto:
            await set_dialog_lang(storage, dialog_id, None)
        if turn_on:
            await set_outbound_enabled(storage, dialog_id, True)
        if turn_off:
            await set_outbound_enabled(storage, dialog_id, False)
        lang_info = await get_dialog_lang(storage, dialog_id)
        enabled = await is_outbound_enabled(storage, dialog_id)
        if lang_info is None:
            click.echo(f"{dialog_id}\tlang=unset\toutbound={'on' if enabled else 'off'}")
        else:
            click.echo(
                f"{dialog_id}\tlang={lang_info.lang}\tsource={lang_info.source}"
                f"\toutbound={'on' if enabled else 'off'}"
            )

    _run(_with_storage(session, lambda storage: None, _do), session=session)


@cli.command()
@click.argument("dialog_id", type=int)
@click.argument("text", required=False)
@click.option("--file", "file_path", default=None, help="Send a file/photo instead of text.")
@click.option("--caption", "caption", default=None,
              help="Caption for --file (overrides the positional TEXT).")
@click.option("--voice", "voice", is_flag=True, help="Send --file as a voice note.")
@click.option("--video-note", "video_note", is_flag=True,
              help="Send --file as a round video note.")
@click.option("--as-file", "as_file", is_flag=True,
              help="Send --file as a plain document (no media preview).")
@click.option("--reply-to", "reply_to", type=int, default=None,
              help="Reply to this message id.")
@click.option("--session", default="default")
def send(dialog_id: int, text: str | None, file_path: str | None, caption: str | None,
         voice: bool, video_note: bool, as_file: bool,
         reply_to: int | None, session: str) -> None:
    """Send a text message (or a file with --file); --reply-to to quote a message.

    --voice / --video-note / --as-file are mutually exclusive media modifiers.
    Caption comes from --caption or, failing that, the positional TEXT.
    """
    if sum([voice, video_note, as_file]) > 1:
        raise click.ClickException(
            "--voice, --video-note and --as-file are mutually exclusive"
        )

    async def _do(client):
        # No pre-flight dialog fetch: a one-shot CLI process has a cold cache, so a
        # read-only check would cost a full dialog list every time. send_media's own
        # offline path check runs first; the core SendForbiddenError seam (mapped in
        # _run) is the authoritative net for a read-only chat.
        if file_path:
            return await client.send_media(
                dialog_id, file_path, caption=caption or text,
                voice_note=voice, video_note=video_note, force_document=as_file,
            )
        return await client.send_text(dialog_id, text or "", reply_to=reply_to)

    _run(_with_client(session, _do), session=session)
    click.echo("sent.")


@cli.command()
@click.argument("dialog_id", type=int)
@click.argument("message_id", type=int)
@click.argument("emoticon")
@click.option("--session", default="default")
def react(dialog_id: int, message_id: int, emoticon: str, session: str) -> None:
    """React to a message with a standard emoji."""

    async def _do(client):
        # No pre-flight gate: reactions are a separate capability from posting, and a
        # one-shot CLI has a cold cache. Telegram rejects (→ SendForbiddenError) if the
        # channel truly forbids reactions. Proper per-message reaction UI: issue #86.
        await client.send_reaction(dialog_id, message_id, emoticon)

    _run(_with_client(session, _do), session=session)
    click.echo("reacted.")


def _parse_ids(raw: str) -> list[int]:
    """Parse a comma-separated message-id list; a bad token is a ClickException."""
    try:
        return [int(p.strip()) for p in raw.split(",") if p.strip()]
    except ValueError as exc:
        raise click.ClickException(f"invalid message id list: {raw!r}") from exc


@cli.command()
@click.argument("from_peer", type=int)
@click.argument("ids")
@click.argument("to_peer", type=int)
@click.option("--session", default="default")
def forward(from_peer: int, ids: str, to_peer: int, session: str) -> None:
    """Forward messages (comma-separated IDS) from FROM_PEER to TO_PEER."""
    message_ids = _parse_ids(ids)

    async def _do(client):
        return await client.forward(from_peer, message_ids, to_peer)

    _run(_with_client(session, _do), session=session)
    click.echo("forwarded.")


@cli.command()
@click.argument("dialog_id", type=int)
@click.argument("message_id", type=int)
@click.argument("text")
@click.option("--session", default="default")
def edit(dialog_id: int, message_id: int, text: str, session: str) -> None:
    """Edit the text of one of your messages."""

    async def _do(client):
        return await client.edit_text(dialog_id, message_id, text)

    _run(_with_client(session, _do), session=session)
    click.echo("edited.")


@cli.command()
@click.argument("dialog_id", type=int)
@click.argument("ids")
@click.option("--for-me", "for_me", is_flag=True,
              help="Delete only for yourself (don't revoke for everyone).")
@click.option("--session", default="default")
def delete(dialog_id: int, ids: str, for_me: bool, session: str) -> None:
    """Delete messages (comma-separated IDS); --for-me to keep them for others."""
    message_ids = _parse_ids(ids)
    if for_me and is_channel_or_megagroup_id(dialog_id):
        raise click.ClickException(
            "--for-me is not supported for channels/supergroups; Telegram deletes there for everyone"
        )

    async def _do(client):
        return await client.delete_messages(dialog_id, message_ids, revoke=not for_me)

    _run(_with_client(session, _do), session=session)
    click.echo("deleted.")


@cli.command("mark-read")
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
def mark_read(dialog_id: int, session: str) -> None:
    """Mark a dialog as read (clears its unread counter)."""

    async def _do(client):
        return await client.mark_read(dialog_id)

    _run(_with_client(session, _do), session=session)
    click.echo("marked read.")


@cli.command()
@click.option("--session", default="default")
@click.pass_context
def listen(ctx: click.Context, session: str) -> None:
    """Print incoming messages live."""
    session = _effective_session(ctx, session)

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

    _run_interruptible(_do(), session=session)


@cli.command()
@click.option("--session", default="default")
@click.pass_context
def watch(ctx: click.Context, session: str) -> None:
    """Back up your deleted messages (e.g. removed by group moderator bots) to Saved Messages."""
    from tg_messenger.core.watch import DeletionWatcher

    session = _effective_session(ctx, session)

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            click.echo("Watching for deletions of your messages (Ctrl+C to stop)...")
            await DeletionWatcher(client, echo=click.echo).run()
        finally:
            await client.disconnect()

    _run_interruptible(_do(), session=session)


@cli.command()
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

    session = _effective_session(ctx, session)
    _warn_if_send_rate_off()  # #50: warn rules can send (mute/ban notices, warn_text)

    async def _do():
        client = make_client(session_name=session)
        storage = make_storage(session)
        register_moderation_migrations(storage)
        await storage.connect()
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            rights = await check_admin_rights(client, storage)
            for chat_id, ok in rights.items():
                if not ok:
                    click.echo(f"⚠ no admin rights in chat {chat_id} — its rules are disabled",
                               err=True)
            engine = ModerationEngine(client, storage, enforce=enforce)
            engine.disable_chats([cid for cid, ok in rights.items() if not ok])
            mode = "ENFORCING" if enforce else "dry-run"
            click.echo(f"Moderating ({mode}) — Ctrl+C to stop...")
            await engine.run()
        finally:
            await client.disconnect()
            await storage.close()

    _run_interruptible(_do(), session=session)


@cli.group("moderate-rules")
def moderate_rules() -> None:
    """Manage moderation rules (list / add / remove)."""


@moderate_rules.command("list")
@click.option("--session", default="default")
@click.option("--chat", "chat_id", type=int, default=None, help="Filter by chat id.")
@click.pass_context
def moderate_rules_list(ctx: click.Context, session: str, chat_id: int | None) -> None:
    """List stored moderation rules."""
    from tg_messenger.core.moderation import list_rules, register_moderation_migrations

    session = _effective_session(ctx, session)

    rules = _run(
        _with_storage(session, register_moderation_migrations,
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

    session = _effective_session(ctx, session)

    raw = Path(rule_file).read_text(encoding="utf-8")
    try:
        rule = ModerationRule.model_validate_json(raw)
    except Exception as exc:
        raise click.ClickException(f"invalid rule JSON: {exc}") from exc

    _run(
        _with_storage(session, register_moderation_migrations,
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

    session = _effective_session(ctx, session)

    deleted = _run(
        _with_storage(session, register_moderation_migrations,
                      lambda storage: remove_rule(storage, chat_id, name)),
        session=session,
    )
    if deleted == 0:
        raise click.ClickException(f"rule '{name}' not found in chat {chat_id}.")
    click.echo(f"rule '{name}' removed from chat {chat_id}.")


@cli.command()
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
@click.pass_context
def chat(ctx: click.Context, dialog_id: int, session: str) -> None:
    """Interactive REPL: see incoming and send replies."""
    session = _effective_session(ctx, session)

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        store = None
        store_task = None
        try:
            await _ensure_authorized(client, session)
            store, storage = make_message_store(client, session=session)
            translator = make_optional_translator(storage)
            outbound = make_optional_outbound(store, storage)
            await store.connect()
            store_task = asyncio.create_task(store.run())
            from tg_messenger.agent.outbound import set_dialog_lang, set_outbound_enabled
            from tg_messenger.agent.translate import get_user_lang

            telegram_lang_code = None
            # read the dialog once: telegram_lang_code (outbound) AND can_send (read-only gate)
            try:
                for dialog in await client.dialogs(dm_only=False):
                    if dialog.id == dialog_id:
                        telegram_lang_code = getattr(dialog, "telegram_lang_code", None)
                        if not getattr(dialog, "can_send", True):
                            click.echo("Чат только для чтения — отправка отключена.", err=True)
                        break
            except Exception:
                logger.warning("failed to read dialogs for the chat REPL", exc_info=True)

            async def _send_or_warn(coro):
                """Run a send; on a rights rejection warn and return None so the REPL
                keeps running instead of the whole session exiting (F3)."""
                try:
                    return await coro
                except SendForbiddenError:
                    logger.warning("send rejected (rights) in chat REPL (dialog %s)", dialog_id)
                    click.echo("Сюда писать нельзя — чат только для чтения.", err=True)
                    return None

            # (dialog_id, message_id) keys we sent from this REPL echo on listen_outgoing();
            # skip them so our own input isn't printed back. Bounded (deque).
            sent_ids: deque[tuple[int, int]] = deque(maxlen=200)

            async def printer():
                async for ev in client.listen():
                    if ev.dialog_id == dialog_id:
                        msg = await _maybe_translate_message(translator, ev.message)
                        click.echo(f"\n← {msg.text or '<media>'}")
                        if msg.translated_text:
                            click.echo(f"  ↳ {msg.translated_text}")

            async def printer_outgoing():
                # our own messages sent from another device (phone/web/CLI elsewhere)
                async for ev in client.listen_outgoing():
                    if ev.dialog_id == dialog_id and (ev.dialog_id, ev.message.id) not in sent_ids:
                        msg = await _maybe_translate_message(translator, ev.message)
                        click.echo(f"\n→ {msg.text or '<media>'}")
                        if msg.translated_text:
                            click.echo(f"  ↳ {msg.translated_text}")

            async def printer_reactions():
                async for ev in client.listen_reactions():
                    if ev.dialog_id == dialog_id:
                        click.echo(
                            f"\n* reaction [{ev.message_id}]: {_reaction_emoticon(ev.emoticon)}"
                        )

            tasks = [
                asyncio.create_task(printer()),
                asyncio.create_task(printer_outgoing()),
                asyncio.create_task(printer_reactions()),
            ]
            try:
                while True:
                    try:
                        line = await asyncio.to_thread(input, "> ")
                    except EOFError:
                        break
                    if line.strip():
                        if line.startswith("/react "):
                            parts = line.split(maxsplit=2)
                            if len(parts) != 3 or not parts[1].isdigit():
                                click.echo("usage: /react MESSAGE_ID EMOTICON", err=True)
                                continue
                            await _send_or_warn(
                                client.send_reaction(dialog_id, int(parts[1]), parts[2])
                            )
                            continue
                        if line.split(maxsplit=1)[0] == "/lang":
                            parts = line.split(maxsplit=1)
                            if len(parts) != 2 or not parts[1].strip():
                                click.echo("usage: /lang CODE|auto|on|off", err=True)
                                continue
                            if outbound is None:
                                click.echo("outbound translation is not configured.", err=True)
                                continue
                            value = parts[1].strip().lower()
                            try:
                                if value == "auto":
                                    await set_dialog_lang(outbound.storage, dialog_id, None)
                                elif value == "on":
                                    await set_outbound_enabled(outbound.storage, dialog_id, True)
                                elif value == "off":
                                    await set_outbound_enabled(outbound.storage, dialog_id, False)
                                else:
                                    await set_dialog_lang(
                                        outbound.storage,
                                        dialog_id,
                                        value,
                                        source="manual",
                                    )
                            except ValueError as exc:
                                click.echo(str(exc), err=True)
                                continue
                            except Exception as exc:
                                logger.exception("dialog language command failed")
                                click.echo(f"language setting failed: {exc}", err=True)
                                continue
                            click.echo("language setting saved.")
                            continue
                        if outbound is not None:
                            try:
                                prepare = getattr(outbound, "prepare_variants", None)
                                if prepare is not None:
                                    target_lang, variants = await asyncio.wait_for(
                                        prepare(
                                            dialog_id,
                                            line,
                                            telegram_lang_code=telegram_lang_code,
                                        ),
                                        timeout=CHAT_OUTBOUND_TIMEOUT_SECONDS,
                                    )
                                else:
                                    target_lang = await asyncio.wait_for(
                                        outbound.applies(
                                            dialog_id,
                                            line,
                                            telegram_lang_code=telegram_lang_code,
                                        ),
                                        timeout=CHAT_OUTBOUND_TIMEOUT_SECONDS,
                                    )
                                    variants = []
                            except TimeoutError:
                                logger.warning(
                                    "outbound applicability timed out (dialog %s)", dialog_id
                                )
                                click.echo("translation timed out — sending original.", err=True)
                                msg = await _send_or_warn(client.send_text(dialog_id, line))
                                if msg is not None:
                                    sent_ids.append((dialog_id, msg.id))
                                continue
                            if target_lang is not None:
                                try:
                                    if not variants:
                                        variants = await asyncio.wait_for(
                                            outbound.variants(dialog_id, line, target_lang),
                                            timeout=CHAT_OUTBOUND_TIMEOUT_SECONDS,
                                        )
                                except TimeoutError:
                                    logger.warning("outbound variants timed out (dialog %s)", dialog_id)
                                    click.echo("translation timed out — sending original.", err=True)
                                    msg = await _send_or_warn(client.send_text(dialog_id, line))
                                    if msg is not None:
                                        sent_ids.append((dialog_id, msg.id))
                                    continue
                                except Exception:
                                    logger.exception("outbound variants failed (dialog %s)", dialog_id)
                                    confirm = await asyncio.to_thread(
                                        input, "перевод не удался, отправить оригинал? [y/N] "
                                    )
                                    if confirm.strip().lower() not in {"y", "yes", "д", "да"}:
                                        continue
                                    msg = await _send_or_warn(client.send_text(dialog_id, line))
                                    if msg is not None:
                                        sent_ids.append((dialog_id, msg.id))
                                    continue
                                for idx, variant in enumerate(variants, start=1):
                                    click.echo(f"[{idx}] {variant}")
                                original_idx = len(variants) + 1
                                click.echo(f"[{original_idx}] original: {line}")
                                click.echo("[0] cancel")
                                choice = await asyncio.to_thread(input, "вариант> ")
                                if not choice.strip() or choice.strip() == "0":
                                    continue
                                if choice.strip() == str(original_idx):
                                    msg = await _send_or_warn(client.send_text(dialog_id, line))
                                    if msg is not None:
                                        sent_ids.append((dialog_id, msg.id))
                                    continue
                                try:
                                    picked = variants[int(choice.strip()) - 1]
                                except (ValueError, IndexError):
                                    click.echo("cancelled.", err=True)
                                    continue
                                msg = await _send_or_warn(client.send_text(dialog_id, picked))
                                if msg is None:
                                    continue
                                sent_ids.append((dialog_id, msg.id))
                                source_lang = await get_user_lang(storage) or ""
                                if source_lang:
                                    try:
                                        await store.record_outgoing(
                                            dialog_id,
                                            msg,
                                            source_text=line,
                                            source_lang=source_lang,
                                        )
                                    except Exception:
                                        logger.exception("failed to record outbound source text")
                                click.echo(f"  ↳ {line}")
                                continue
                        msg = await _send_or_warn(client.send_text(dialog_id, line))
                        if msg is not None:
                            sent_ids.append((dialog_id, msg.id))  # suppress this line's echo
            finally:
                for t in tasks:
                    t.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    # CancelledError is BaseException — a real failure only
                    if isinstance(r, Exception):
                        logger.error("chat listener failed", exc_info=r)
                        click.echo(f"listener failed: {r}", err=True)
                if store_task is not None:
                    store_task.cancel()
                    store_results = await asyncio.gather(store_task, return_exceptions=True)
                    for r in store_results:
                        if isinstance(r, Exception):
                            logger.error("chat message-store task failed", exc_info=r)
        finally:
            if store is not None:
                await store.close()
            await client.disconnect()

    _run_interruptible(_do(), session=session)


@cli.command()
@click.option("--session", default="default")
@click.option("--notify-errors", is_flag=True,
              help="Reply with a short notice when processing a message fails.")
@click.pass_context
def agent(ctx: click.Context, session: str, notify_errors: bool) -> None:
    """AI assistant: auto-reply to incoming DMs, route tasks to a deep agent."""
    session = _effective_session(ctx, session)
    _warn_if_send_rate_off()  # #50: loud when the global send cap is off
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

    _run_interruptible(_do(), session=session)


@cli.command()
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
    session = _effective_session(ctx, session)
    _warn_if_send_rate_off()  # #50: loud when the global send cap is off
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
    factory = make_factory_client(
        base_url=factory_url, password=os.environ.get("TG_FACTORY_PASSWORD")
    )

    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _do():
        client = make_client(session_name=session)
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            agent = make_worker_agent(client)
            click.echo(
                f"Worker polling {factory_url} for {', '.join(task_types)} "
                f"(prompt tasks {'on' if agent is not None else 'off'}; Ctrl+C to stop)..."
            )
            await Worker(
                client, factory, types=task_types, sleep=_sleep, idle_sleep=interval,
                agent=agent,
            ).run()
        finally:
            await client.disconnect()

    _run_interruptible(_do(), session=session)


@cli.command()
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
    session = _effective_session(ctx, session)

    async def _do():
        client = make_client(session_name=session)
        from tg_messenger.agent.suggest import register_suggest_migrations

        storage = make_storage(session)
        register_suggest_migrations(storage)
        # build the suggester (LLM stack) before the network — config errors show first
        suggester = make_suggester(client, storage=storage)
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            await storage.connect()
            try:
                await _ensure_dm_dialog(client, dialog_id)
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

    _run(_do(), session=session)


# --- ghostwrite (#18): auto-reply in your style in explicitly enabled DMs ---------


@cli.command()
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
    session = _effective_session(ctx, session)
    _warn_if_send_rate_off()  # #50: loud when the global send cap is off

    async def _do():
        client = make_client(session_name=session)
        storage = make_storage(session)
        register_ghostwrite_migrations(storage)
        register_suggest_migrations(storage)  # the suggester reads style profiles from here
        # build the suggester (LLM stack) before the network — config errors show first
        suggester = make_suggester(client, storage=storage)
        await storage.connect()
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            enabled = await list_enabled(storage)
            if enabled:
                click.echo("Ghostwrite enabled for dialogs: "
                           + ", ".join(str(d) for d in enabled))
            else:
                click.echo("⚠ no dialogs enabled — use 'ghostwrite-dialogs enable PEER'",
                           err=True)
            engine = GhostwriteEngine(client, suggester, storage, enforce=enforce)
            mode = "ENFORCING" if enforce else "dry-run"
            click.echo(f"Ghostwriting ({mode}) — Ctrl+C to stop...")
            # цикл 98 (#17): рядом с движком пишем read-receipts (last_read
            # per dialog, kv) — сигнал «прочитал и молчит» для #18/#19.
            async with _read_receipts_watcher(client, storage):
                await engine.run()
        finally:
            await client.disconnect()
            await storage.close()

    _run_interruptible(_do(), session=session)


@cli.group("ghostwrite-dialogs")
def ghostwrite_dialogs() -> None:
    """Manage the ghostwrite per-dialog allowlist (enable / disable / list / pause)."""


@ghostwrite_dialogs.command("enable")
@click.argument("peer")
@click.option("--session", default="default")
@click.pass_context
def ghostwrite_dialogs_enable(ctx: click.Context, peer: str, session: str) -> None:
    """Turn ghostwrite ON for a DM by PEER (numeric id). '*' is rejected by design."""
    if peer.strip() == "*":
        raise click.ClickException(
            "'*' (everyone) is not allowed for ghostwrite — enable each dialog explicitly."
        )
    try:
        dialog_id = int(peer)
    except ValueError as exc:
        raise click.ClickException(f"PEER must be a numeric dialog id, got {peer!r}") from exc
    from tg_messenger.agent.ghostwrite import enable_dialog, register_ghostwrite_migrations
    session = _effective_session(ctx, session)

    _run(
        _with_storage(session, register_ghostwrite_migrations,
                      lambda storage: enable_dialog(storage, dialog_id)),
        session=session,
    )
    click.echo(f"ghostwrite enabled for dialog {dialog_id}.")


@ghostwrite_dialogs.command("disable")
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
@click.pass_context
def ghostwrite_dialogs_disable(ctx: click.Context, dialog_id: int, session: str) -> None:
    """Turn ghostwrite OFF for a dialog."""
    from tg_messenger.agent.ghostwrite import disable_dialog, register_ghostwrite_migrations
    session = _effective_session(ctx, session)

    _run(
        _with_storage(session, register_ghostwrite_migrations,
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
    session = _effective_session(ctx, session)

    enabled = _run(
        _with_storage(session, register_ghostwrite_migrations, list_enabled),
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
    session = _effective_session(ctx, session)

    _run(
        _with_storage(session, register_ghostwrite_migrations, pause_all),
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
    session = _effective_session(ctx, session)

    _run(
        _with_storage(session, register_ghostwrite_migrations,
                      lambda storage: resume_dialog(storage, dialog_id)),
        session=session,
    )
    click.echo(f"ghostwrite resumed for dialog {dialog_id}.")


# --- heartbeat (#19): scheduled pings with templates and safeties ---------------


def _parse_at(at: str):
    """Parse ``HH:MM`` into the next future local datetime (today, or tomorrow if past)."""
    from datetime import datetime, timedelta

    try:
        hh, mm = (int(p) for p in at.split(":", 1))
    except ValueError as exc:
        raise click.ClickException(f"--at must be HH:MM, got {at!r}") from exc
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


@cli.group()
def heartbeat() -> None:
    """Scheduled pings: one-shot (--at) or recurring stored plans (--interval)."""


@heartbeat.command("plan")
@click.argument("peer", type=int)
@click.option("--at", "at", default=None, help="One-shot HH:MM (server-side scheduled send).")
@click.option("--interval", "interval_hours", type=float, default=None,
              help="Recurring: hours between pings (stores a plan).")
@click.option("--template", "templates", multiple=True, help="Ping text (repeatable).")
@click.option("--jitter", "jitter_minutes", type=float, default=0.0,
              help="Random +0..N minutes added to each interval.")
@click.option("--quiet-start", type=int, default=None, help="Quiet window open hour (local).")
@click.option("--quiet-end", type=int, default=None, help="Quiet window close hour (local).")
@click.option("--max-per-day", type=int, default=1, help="Daily ping cap (recurring plans).")
@click.option("--session", default="default")
@click.pass_context
def heartbeat_plan(ctx: click.Context, peer: int, at: str | None, interval_hours: float | None,
                   templates: tuple[str, ...], jitter_minutes: float,
                   quiet_start: int | None, quiet_end: int | None,
                   max_per_day: int, session: str) -> None:
    """Schedule pings to PEER: --at for a one-shot native send, --interval for a stored plan."""
    session = _effective_session(ctx, session)
    if not templates:
        raise click.ClickException("at least one --template is required.")
    if at is None and interval_hours is None:
        raise click.ClickException("provide --at (one-shot) or --interval (recurring plan).")
    if at is not None and interval_hours is not None:
        raise click.ClickException("--at and --interval are mutually exclusive.")

    if at is not None:
        # one-shot: native server-side scheduled send (no stored plan)
        when = _parse_at(at)
        text = templates[0]

        async def _do_oneshot(client):
            await client.send_text(peer, text, schedule=when)

        _run(_with_client(session, _do_oneshot), session=session)
        click.echo(f"scheduled one-shot ping to {peer} at {when:%Y-%m-%d %H:%M}.")
        return

    # recurring: store a plan
    from tg_messenger.core.heartbeat import HeartbeatPlan, add_plan, register_heartbeat_migrations

    plan = HeartbeatPlan(
        peer=peer, templates=list(templates), interval_hours=interval_hours,
        jitter_minutes=jitter_minutes, quiet_start=quiet_start, quiet_end=quiet_end,
        max_per_day=max_per_day,
    )

    _run(
        _with_storage(session, register_heartbeat_migrations,
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
    session = _effective_session(ctx, session)

    plans = _run(
        _with_storage(session, register_heartbeat_migrations, list_plans),
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
    session = _effective_session(ctx, session)

    _run(
        _with_storage(session, register_heartbeat_migrations,
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
    session = _effective_session(ctx, session)
    _warn_if_send_rate_off()  # #50: loud when the global send cap is off

    async def _do():
        client = make_client(session_name=session)
        storage = make_storage(session)
        register_heartbeat_migrations(storage)
        await storage.connect()
        await client.connect()
        try:
            await _ensure_authorized(client, session)
            click.echo("Heartbeat scheduler running — Ctrl+C to stop...")
            # сигнал «прочитал и молчит»: last_read пишется и здесь (#17/#19)
            async with _read_receipts_watcher(client, storage):
                await HeartbeatService(client, storage).run()
        finally:
            await client.disconnect()
            await storage.close()

    _run_interruptible(_do(), session=session)


@cli.group()
def username() -> None:
    """Generate / check / set this account's public @username (#22)."""


@username.command("suggest")
@click.argument("base")
@click.option("--limit", default=10, help="Max number of available usernames to return.")
@click.option("--session", default="default")
def username_suggest(base: str, limit: int, session: str) -> None:
    """Suggest available usernames derived from BASE (checks candidates sequentially).

    Verified-free names are marked ``✓``; generated candidates past the limit that
    were never checked (no extra network calls) are marked ``?``.
    """
    from tg_messenger.core.usernames import find_available_marked

    async def _do(client):
        return await find_available_marked(client, base, limit=limit)

    free, unchecked = _run(_with_client(session, _do), session=session)
    if not free and not unchecked:
        click.echo("No available usernames found — try a different base.")
        return
    # free names are verified available (✓); unchecked candidates are generated but
    # their availability is unknown (?), so the user can probe them with `username set`
    for name in free:
        click.echo(f"{name} ✓")
    for name in unchecked:
        click.echo(f"{name} ?")


@username.command("set")
@click.argument("name")
@click.option("--session", default="default")
def username_set(name: str, session: str) -> None:
    """Set this account's public username to NAME (fails if invalid or taken)."""

    async def _do(client):
        try:
            available = await client.check_username(name)
            if not available:
                raise click.ClickException(f"username '{name}' is not available.")
            await client.set_username(name)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    _run(_with_client(session, _do), session=session)
    click.echo(f"username set to @{name}.")


@username.command("clear")
@click.option("--session", default="default")
def username_clear(session: str) -> None:
    """Remove this account's public username."""

    async def _do(client):
        await client.clear_username()

    _run(_with_client(session, _do), session=session)
    click.echo("username cleared.")


_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_local_host(host: str) -> bool:
    """True for loopback-only binds (127.0.0.1 / localhost / ::1)."""
    return host in _LOCAL_HOSTS


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=lambda: int(os.environ.get("TG_WEB_PORT", "8090")), type=int)
@click.option("--session", default="default")
@click.option("--insecure", is_flag=True,
              help="Serve a non-localhost host without TG_WEB_PASS (anyone can connect).")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int, session: str, insecure: bool) -> None:
    """Launch the web interface.

    Set ``TG_WEB_PASS`` to require a password (HMAC-cookie session). Binding to a
    non-localhost host without it is refused unless ``--insecure`` is passed.
    """
    try:
        import uvicorn

        from tg_messenger.web.app import build_app
    except ImportError as exc:
        # base install omits the web stack — point at the extra instead of a raw ImportError
        raise click.ClickException("web UI requires: pip install 'tg-messenger[web]'") from exc

    web_pass = os.environ.get("TG_WEB_PASS") or None
    if not _is_local_host(host) and not web_pass and not insecure:
        raise click.ClickException(
            f"refusing to serve on {host} without TG_WEB_PASS — set it or pass --insecure"
        )
    if not _is_local_host(host) and not web_pass and insecure:
        logger.warning("serving on %s without TG_WEB_PASS (--insecure) — anyone can connect", host)

    session = _effective_session(ctx, session)
    client = make_client(session_name=session)
    suggester = make_optional_suggester(client, session=session)
    store, storage = make_message_store(client, session=session)
    translator = make_optional_translator(storage)
    outbound = make_optional_outbound(store, storage)
    # uvicorn's own banner goes to the file (log_config=None) — announce the URL here
    click.echo(f"Serving on http://{host}:{port} — Ctrl+C to stop.")
    uvicorn.run(
        build_app(
            client=client,
            session_name=session,
            suggester=suggester,
            web_pass=web_pass,
            store=store,
            translator=translator,
            outbound=outbound,
        ),
        host=host,
        port=port,
        log_config=None,
    )


@cli.command()
@click.option("--session", default="default")
@click.pass_context
def tui(ctx: click.Context, session: str) -> None:
    """Launch the TUI interface."""
    try:
        from tg_messenger.tui.app import MessengerTUI
    except ImportError as exc:
        raise click.ClickException("TUI requires: pip install 'tg-messenger[tui]'") from exc

    # stderr handler would corrupt the alternate screen — file log only. Keep these kwargs
    # so a profile picked LATER (CLI fallback or the in-app ProfileScreen) re-inits the
    # log WITHOUT a console handler (#52). make_tui_deps replays setup_logging per profile.
    log_kwargs = {"verbose": ctx.obj["verbose"], "console": False}
    ctx.obj["_log_kwargs"] = log_kwargs
    setup_logging(profile=ctx.obj.get("profile"), **log_kwargs)

    # #52 point 2: when no profile is fixed up front and >1 exist, defer selection to the
    # in-app ProfileScreen instead of the CLI menu — pass profiles + a deps_factory that
    # builds the whole dependency set (and re-inits per-profile logging) for the pick.
    explicit_profile = ctx.obj.get("profile")
    explicit_session = (
        ctx.get_parameter_source("session") == ParameterSource.COMMANDLINE
    )
    if explicit_profile:
        resolved = explicit_profile
    elif explicit_session:
        resolved = session
    else:
        profiles = _session_store().list_profiles()
        if len(profiles) <= 1:
            resolved = profiles[0] if profiles else session
        elif not _is_interactive():
            raise click.ClickException(
                f"multiple profiles ({', '.join(profiles)}) — pass --profile NAME"
            )
        else:
            # >1 profiles + interactive tty → let the TUI's ProfileScreen pick
            MessengerTUI(
                profiles=profiles,
                deps_factory=lambda p: make_tui_deps(p, log_kwargs=log_kwargs),
            ).run()
            return

    deps = make_tui_deps(resolved, log_kwargs=log_kwargs)
    MessengerTUI(
        client=deps.client,
        session_name=deps.session_name,
        suggester=deps.suggester,
        store=deps.store,
        translator=deps.translator,
        outbound=deps.outbound,
    ).run()


if __name__ == "__main__":
    cli()
