"""click-based CLI over the shared core.

Commands: login, dialogs, read, send, listen, chat, serve, tui.
``make_client`` is the single seam tests patch to avoid real network.
"""

from __future__ import annotations

# #179: `python -m tg_messenger.cli.main` runs THIS file as `__main__`, a SEPARATE module object
# from the canonical `tg_messenger.cli.main` that the relocated command modules bind to. Letting
# the body run twice causes a re-entrant registration crash. So when executed as `__main__`,
# delegate to the canonical module (a single, clean import) and exit before the body runs.
if __name__ == "__main__":
    import sys as _sys

    from tg_messenger.cli.main import cli as _cli

    _sys.exit(_cli())  # the canonical module imported above ran registration exactly once

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

from tg_messenger.agent.config import flush_tracers, langsmith_tracing_enabled

# #179: the pure parsers moved to a leaf module; re-export so `cli.main._parse_dotenv` (test_e2e)
# and the in-module callers (`_load_dotenv`, forward/delete, heartbeat plan) keep resolving here.
from tg_messenger.cli.parsers import _parse_at, _parse_dotenv, _parse_ids  # noqa: F401
from tg_messenger.core import paths as core_paths
from tg_messenger.core.auth import LOGIN_HINT, session_store_from_env, validate_session_string
from tg_messenger.core.client import (
    MessageDeleteValidationError,
    MissingCredentialsError,
    SendForbiddenError,
    client_from_env,
)
from tg_messenger.core.flood import HandledFloodWaitError
from tg_messenger.core.logsetup import log_file_path, setup_logging
from tg_messenger.core.models import message_line
from tg_messenger.core.paths import tg_home

logger = logging.getLogger(__name__)
CHAT_OUTBOUND_TIMEOUT_SECONDS = 20

# #187: --session duplicates the global --profile; document that the global one wins
# (see _effective_session) instead of leaving the option bare in --help.
SESSION_OPTION_HELP = "Alias of the global --profile (which wins if both are set)."

# #187: the chat REPL announces its slash-commands and exit on start, and /help reprints
# them — so /react//lang and "how do I quit?" aren't a read-the-source feature, and a
# stray /help//langs typo is intercepted instead of being sent to the contact.
CHAT_REPL_COMMANDS = (
    "Commands: /react MESSAGE_ID EMOTICON · /lang CODE|auto|on|off · /help · "
    "Ctrl-D to quit. Anything else is sent to the chat."
)


def _reaction_emoticon(emoticon: str | None) -> str:
    return emoticon if emoticon is not None else "<custom>"


def _picker_line(marker: str, text: str) -> str:
    """#187: render one ``[idx] text`` picker row, indenting continuation lines of a
    multiline variant under the text column so the ``[idx]`` markers stay aligned.

    Variants are NOT truncated (they must be read in full) — only the wrapping cue is
    fixed. A single-line variant renders exactly as ``[idx] text``.
    """
    prefix = f"{marker} "
    if "\n" not in text:
        return f"{prefix}{text}"
    indent = " " * len(prefix)
    head, *rest = text.split("\n")
    return "\n".join([f"{prefix}{head}", *(f"{indent}{line}" for line in rest)])


def _load_dotenv(path: Path | str = ".env") -> None:
    """Layer .env files into the environment; the real environment always wins.

    Precedence (highest first): real env > cwd ``.env`` > the ACTIVE root's
    ``tg_home()/.env`` > the fixed ``~/.tg/.env`` fallback. ``setdefault`` never
    overwrites, so loading a source EARLIER makes it win; every file yields to a value
    already in the real environment. The persistent config is what makes
    ``tg-messenger tui`` work from ANY directory, not just one that happens to hold a ``.env``.

    Two config paths are read, deduped, in this order (#188 Axis B):

    1. ``tg_home()/.env`` — the ACTIVE data root. This is ``~/.tg`` normally, but a
       legacy ``~/.tg_messenger`` on fallback, OR an explicit ``TG_HOME`` override. It
       must win over the fixed default below: an explicit ``TG_HOME=/custom-root`` points
       sessions/db there, so a stale ``SESSION_ENCRYPTION_KEY`` in ``~/.tg/.env`` winning
       would open the custom root's encrypted sessions with the WRONG key (#190 cycle-3).
    2. ``DEFAULT_HOME/.env`` (= ``~/.tg/.env``) — always attempted as a LAST fallback,
       decoupled from the data-root decision (#190 cycle-2). A legacy user (real session
       data in ``~/.tg_messenger/``) whose only creds live in ``~/.tg/.env`` has
       ``tg_home()`` resolve to the legacy root, so reading ONLY ``tg_home()/.env`` would
       miss the file the missing-creds hint told them to create — this fallback catches it.

    When ``tg_home()`` already IS ``DEFAULT_HOME`` (the common case), the dedup reads
    ``~/.tg/.env`` exactly once. Missing files are fine (_parse_dotenv → {}).
    """
    sources: list[Path | str] = [path]
    for candidate in (tg_home() / ".env", core_paths.DEFAULT_HOME / ".env"):
        if candidate not in sources:
            sources.append(candidate)
    for source in sources:
        for key, value in _parse_dotenv(source).items():
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


def _announce_tracing(echo=click.echo) -> None:
    """Surface LangSmith tracing status at startup; fail fast on a missing key (#168).

    Every LLM command goes through this (not just `agent`): when tracing is on it
    announces the project, and when ``LANGSMITH_TRACING=true`` without a key it raises
    a ``ClickException`` instead of letting langsmith silently spew per-trace errors.

    ``echo`` is injectable so non-CLI front-ends can route the line through a logger
    (TUI's alt-screen, the web server) instead of stdout.
    """
    try:
        if langsmith_tracing_enabled():
            project = os.environ.get("LANGSMITH_PROJECT", "default")
            echo(f"LangSmith tracing: on (project={project})")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


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
                await self._apply_stored_model()

    async def _apply_stored_model(self) -> None:
        """Apply a persisted model override (#143) once storage is live.

        Best-effort: a bad stored model is logged and the default model kept, so a
        once-good-now-broken override never disables the suggester at startup.
        """
        from tg_messenger.agent.suggest import SUGGEST_MODEL_KEY

        if not getattr(self._suggester, "supports_model_swap", False):
            return
        model = await self._storage.get_value(SUGGEST_MODEL_KEY)
        if not model:
            return
        try:
            self._suggester.set_suggest_fn(self._suggester.build_suggest_fn(str(model)))
        except Exception as exc:
            logger.warning("suggester model override %r ignored: %s", model, exc)

    async def suggest(self, dialog_id: int) -> str:
        await self._ensure_connected()
        return await self._suggester.suggest(dialog_id)

    async def learn(self, dialog_id: int):
        await self._ensure_connected()
        return await self._suggester.learn(dialog_id)

    async def get_settings(self) -> dict:
        """Read the live suggester settings (for the settings UI)."""
        await self._ensure_connected()
        from tg_messenger.agent.suggest import get_suggest_settings

        return await get_suggest_settings(self._storage)

    async def save_settings(self, *, enabled: bool, history: int, model: str | None) -> None:
        """Persist settings and apply them live (validates the model before commit)."""
        await self._ensure_connected()
        from tg_messenger.agent.suggest import _coerce_history, set_suggest_settings

        # Validate the cheap field (history) FIRST so a bad history doesn't trigger a
        # wasted init_chat_model build; then validate/build the model BEFORE persisting,
        # so a bad model never half-commits (mirrors the translator validate-then-commit).
        history = _coerce_history(history)
        supports_swap = getattr(self._suggester, "supports_model_swap", False)
        new_fn = None
        if model and supports_swap:
            new_fn = self._suggester.build_suggest_fn(model)
        await set_suggest_settings(
            self._storage, enabled=enabled, history=history, model=model
        )
        if new_fn is not None:
            self._suggester.set_suggest_fn(new_fn)
        elif not model and supports_swap:
            # the override was CLEARED — live-revert to the default model, else drafts
            # keep using the previously-overridden model until restart (#143 review).
            self._suggester.reset_suggest_fn()

    async def close(self) -> None:
        if self._connected:
            await self._storage.close()
            self._connected = False


def make_optional_suggester(client, *, session: str = "default"):
    """Best-effort production suggester for web/TUI.

    Suggest is an optional [agent] feature. A missing extra or model should disable
    the draft endpoint/strip, not prevent the web UI or TUI from starting. The
    disabled state is logged LOUDLY with the concrete reason (#144) — never silent.
    """
    try:
        from tg_messenger.agent.suggest import register_suggest_migrations

        storage = make_storage(session)
        register_suggest_migrations(storage)
        suggester = make_suggester(client, storage=storage)
    except (click.ClickException, ImportError) as exc:
        # the expected "feature off" paths — log the actionable reason
        from tg_messenger.agent.suggest import suggester_disabled_reason

        logger.warning(
            "reply suggester disabled: %s", suggester_disabled_reason() or exc
        )
        return None
    except Exception:
        # anything else is unexpected: never swallow it silently (project rule)
        logger.exception("reply suggester disabled by an unexpected error")
        return None
    logger.info("reply suggester enabled (session=%s)", session)
    return _StorageBackedSuggester(suggester, storage)


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


def make_outbound_coordinator(outbound, store):
    """The UI-agnostic outbound-send coordinator the chat REPL drives (#162).

    Mirrors how the TUI and web build it; a seam so tests can stub the whole prepare→send flow.
    Pass the chat timeout so the named constant stays the single source (it equals the
    coordinator's own default, so behaviour is unchanged).
    """
    from tg_messenger.agent.outbound_coordinator import OutboundSendCoordinator

    return OutboundSendCoordinator(outbound=outbound, store=store, timeout=CHAT_OUTBOUND_TIMEOUT_SECONDS)


def make_translation_deps(client, *, session: str = "default"):
    """The shared message-store → inbound/outbound translator setup (#164).

    The same three-step build is needed by both ``make_tui_deps`` and ``serve``; one helper keeps
    them in step. Returns ``(translator, outbound, store, storage)``. (``chat`` builds the store
    too but then connects it + runs its sync loop, so it stays inline.)
    """
    store, storage = make_message_store(client, session=session)
    translator = make_optional_translator(storage)
    outbound = make_optional_outbound(store, storage)
    return translator, outbound, store, storage


def translate_auto_from_env(env=None) -> bool:
    """Default auto-translate state for inbound messages (off — don't burn tokens).

    Unlike TG_OUTBOUND (which defaults on), inbound auto-translation defaults OFF so a
    fresh setup never spends LLM tokens until the user opts in (key ``t`` or this flag).
    A per-profile KV override set via the UI wins over this at startup.
    """
    source = os.environ if env is None else env
    return source.get("TG_TRANSLATE_AUTO", "off").strip().lower() in {"1", "true", "on", "yes"}


@dataclass
class TuiDeps:
    """The full dependency set the TUI needs, built for one chosen profile (#52)."""

    client: object
    session_name: str
    suggester: object
    store: object
    translator: object
    outbound: object
    auto_translate: bool


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
    translator, outbound, store, _storage = make_translation_deps(client, session=profile)
    return TuiDeps(
        client=client,
        session_name=profile,
        suggester=suggester,
        store=store,
        translator=translator,
        outbound=outbound,
        auto_translate=translate_auto_from_env(),
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
    except MissingCredentialsError as exc:
        # #188 Axis B: empty TG_API_ID/TG_API_HASH — surface the friendly hint as-is,
        # not folded into the generic "Unexpected error" line below.
        raise click.ClickException(str(exc)) from exc
    except HandledFloodWaitError as exc:
        logger.warning("%s: flood wait %ss", exc.operation, exc.wait_seconds)
        raise click.ClickException(exc.user_message) from exc
    except MessageDeleteValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    except SendForbiddenError as exc:
        # surface Telegram's specific reason (already cleaned in core, #92), not a fixed line
        logger.warning("send rejected (rights): %s", exc)
        raise click.ClickException(str(exc)) from exc
    except UnauthorizedError as exc:
        # session missing or revoked mid-command
        raise click.ClickException(_login_hint(session)) from exc
    except Exception as exc:
        logger.exception("command failed")
        # #187: an exception whose str() is empty would render as
        # "Unexpected error:  — details logged…" (looks broken) — fall back to the class name.
        detail = str(exc) or type(exc).__name__
        raise click.ClickException(
            f"Unexpected error: {detail} — details logged to {log_file_path()}"
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


def _run_interruptible(coro, session: str = "default", *, flush_traces: bool = False) -> None:
    """``_run`` + the long-running commands' Ctrl+C contract (prints "stopped.").

    ``flush_traces`` (set by LLM commands) drains buffered LangSmith traces in the
    ``finally`` — synchronously, after ``asyncio.run`` has returned, so a Ctrl+C or an
    ``asyncio.timeout`` cancellation still gets its run-end events uploaded (#168) instead
    of leaving the run stuck ``pending``.
    """
    try:
        _run(coro, session=session)
    except KeyboardInterrupt:
        click.echo("stopped.")
    finally:
        if flush_traces:
            flush_tracers()


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
    # Freeze the data-root decision NOW, before setup_logging() (or any subdir) can
    # mkdir it. A legacy user (~/.tg_messenger present, ~/.tg absent) with a
    # TG_LOG_DIR under ~/.tg would otherwise have setup_logging create ~/.tg first,
    # flipping every later session/db lookup off the legacy root — see tg_home()'s
    # per-process memo. This first call resolves it from the honest on-disk state.
    tg_home()
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
@click.argument("dialog_id", type=int)
@click.option("--session", default="default")
@click.pass_context
def chat(ctx: click.Context, dialog_id: int, session: str) -> None:
    """Interactive REPL: see incoming and send replies."""
    session = _effective_session(ctx, session)
    _announce_tracing()  # #168: status + fail-fast (chat traces the inbound/outbound translator)

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
            from tg_messenger.agent.outbound_coordinator import OutboundError

            telegram_lang_code = None
            # #199: whether this peer is a BOT. In a bot dialog the slash IS the payload
            # (/start, /settings, /cancel …), so the #187 unknown-slash guard below must not
            # fire — it only protects irreversible sends to a real person. Unknown until the
            # one-time dialog read resolves it (fail-safe: treated as non-bot / guard active).
            dialog_kind = None
            # read the dialog once: kind (#199 bot-slash), telegram_lang_code (outbound) AND
            # can_send (read-only gate)
            try:
                for dialog in await client.dialogs(dm_only=False):
                    if dialog.id == dialog_id:
                        dialog_kind = getattr(dialog, "kind", None)
                        telegram_lang_code = getattr(dialog, "telegram_lang_code", None)
                        if not getattr(dialog, "can_send", True):
                            # #187: English like the rest of the CLI REPL
                            click.echo("This chat is read-only — sending is disabled.", err=True)
                        break
            except Exception:
                logger.warning("failed to read dialogs for the chat REPL", exc_info=True)

            async def _send_or_warn(coro):
                """Run a send; on a rights rejection warn and return None so the REPL
                keeps running instead of the whole session exiting (F3)."""
                try:
                    return await coro
                except SendForbiddenError as exc:
                    # surface Telegram's specific reason (#92); keep the REPL alive (F3)
                    logger.warning(
                        "send rejected (rights) in chat REPL (dialog %s): %s", dialog_id, exc
                    )
                    click.echo(str(exc), err=True)
                    return None

            # (dialog_id, message_id) keys we sent from this REPL echo on listen_outgoing();
            # skip them so our own input isn't printed back. Bounded (deque).
            sent_ids: deque[tuple[int, int]] = deque(maxlen=200)

            # #162: outbound translation goes through the same coordinator the TUI/web use — it owns
            # the prepare timeout, the variant token lifecycle and source recording. The REPL only
            # presents the picker. Built once (None when outbound isn't configured).
            coordinator = make_outbound_coordinator(outbound, store) if outbound is not None else None

            async def _coord_send(peer, body):
                """SendFn for the coordinator: send + return the Message; a rights rejection is
                surfaced (like _send_or_warn) but RE-RAISED so the coordinator restores its token
                and the REPL loop can skip this line without sending."""
                try:
                    return await client.send_text(peer, body)
                except SendForbiddenError as exc:
                    logger.warning(
                        "send rejected (rights) in chat REPL (dialog %s): %s", peer, exc
                    )
                    click.echo(str(exc), err=True)
                    raise

            def _reprint_prompt() -> None:
                # #187: a background echo (incoming/outgoing/reaction) prints straight into
                # the terminal while the user may be mid-line at "> ". We can't read the
                # in-flight input() buffer, so we can't restore a partial draft — but we
                # reprint the bare prompt so the cursor isn't left on an orphaned line.
                click.echo("> ", nl=False)

            async def printer():
                async for ev in client.listen():
                    if ev.dialog_id == dialog_id:
                        msg = await _maybe_translate_message(translator, ev.message)
                        click.echo(f"\n← {msg.text or '<media>'}")
                        if msg.translated_text:
                            click.echo(f"  ↳ {msg.translated_text}")
                        _reprint_prompt()

            async def printer_outgoing():
                # our own messages sent from another device (phone/web/CLI elsewhere)
                async for ev in client.listen_outgoing():
                    if ev.dialog_id == dialog_id and (ev.dialog_id, ev.message.id) not in sent_ids:
                        msg = await _maybe_translate_message(translator, ev.message)
                        click.echo(f"\n→ {msg.text or '<media>'}")
                        if msg.translated_text:
                            click.echo(f"  ↳ {msg.translated_text}")
                        _reprint_prompt()

            async def printer_reactions():
                async for ev in client.listen_reactions():
                    if ev.dialog_id == dialog_id:
                        click.echo(
                            f"\n* reaction [{ev.message_id}]: {_reaction_emoticon(ev.emoticon)}"
                        )
                        _reprint_prompt()

            tasks = [
                asyncio.create_task(printer()),
                asyncio.create_task(printer_outgoing()),
                asyncio.create_task(printer_reactions()),
            ]
            click.echo(CHAT_REPL_COMMANDS)  # #187: announce slash-commands + exit on start
            try:
                while True:
                    try:
                        line = await asyncio.to_thread(input, "> ")
                    except EOFError:
                        break
                    if line.strip():
                        first = line.split(maxsplit=1)[0]
                        if first == "/help":
                            # #187: /help must NOT be sent to the contact — print the hint
                            click.echo(CHAT_REPL_COMMANDS)
                            continue
                        if first == "/react":
                            parts = line.split(maxsplit=2)
                            if len(parts) != 3 or not parts[1].isdigit():
                                click.echo("usage: /react MESSAGE_ID EMOTICON", err=True)
                                continue
                            await _send_or_warn(
                                client.send_reaction(dialog_id, int(parts[1]), parts[2])
                            )
                            continue
                        if (
                            first != "/lang"
                            and first.startswith("/")
                            and dialog_kind != "bot"
                        ):
                            # #187 HIGH: an unknown /command (a typo like /langs or /halp, or a
                            # user hunting for the exit) must NOT be sent verbatim to a real
                            # person. Reject it, keep the draft-less loop alive, send nothing.
                            # #199: a BOT dialog is exempt — there the slash IS the payload
                            # (/start, /settings, /cancel …), so it falls through to be sent.
                            # /help//react//lang stay reserved for every kind (handled above).
                            click.echo(
                                f"unknown command: {first} (type /help)", err=True
                            )
                            continue
                        if first == "/lang":
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
                        if coordinator is not None:
                            # prepare → (REPL picker) → send, all via the coordinator. It owns the
                            # timeout, the variant token and source recording; the REPL only presents
                            # the picker and tracks sent_ids for the echo dedup.
                            result = await coordinator.prepare(
                                dialog_id, line, telegram_lang_code=telegram_lang_code,
                                owner_id=str(dialog_id),
                            )
                            if result.status in {"disabled", "not_applicable", "invalid_empty"}:
                                # translation doesn't apply (or the line is blank) → send the original
                                try:
                                    msg = await coordinator.send_original(dialog_id, line, _coord_send)
                                except SendForbiddenError:
                                    continue
                                sent_ids.append((dialog_id, msg.id))
                                continue
                            if result.status == "error":
                                # prepare timed out or failed — surface why, then offer the original
                                # (the coordinator collapses timeout and other failures into "error").
                                # The "send original?" question lives in the prompt below, not here,
                                # so the line isn't asked twice.
                                click.echo(result.error or "translation failed", err=True)
                                confirm = await asyncio.to_thread(
                                    input, "send original? [y/N] "
                                )
                                # the prompt is English [y/N] now, so accept only y/yes
                                if confirm.strip().lower() not in {"y", "yes"}:
                                    continue
                                try:
                                    msg = await coordinator.send_original(dialog_id, line, _coord_send)
                                except SendForbiddenError:
                                    continue
                                sent_ids.append((dialog_id, msg.id))
                                continue
                            # status == "ready": present the REPL picker (the one CLI-specific part)
                            variants = result.variants
                            for idx, variant in enumerate(variants, start=1):
                                click.echo(_picker_line(f"[{idx}]", variant))
                            original_idx = len(variants) + 1
                            click.echo(_picker_line(f"[{original_idx}] original:", line))
                            click.echo("[0] cancel")
                            choice = (await asyncio.to_thread(input, "variant> ")).strip()
                            if not choice or choice == "0":
                                continue
                            if choice == str(original_idx):
                                try:
                                    msg = await coordinator.send_original(dialog_id, line, _coord_send)
                                except SendForbiddenError:
                                    continue
                                sent_ids.append((dialog_id, msg.id))
                                continue
                            try:
                                idx = int(choice)
                            except ValueError:
                                click.echo("cancelled.", err=True)
                                continue
                            # require an in-range 1-based index — negative ints index from the end
                            # in Python (variants[-2] etc.), so guard the bound explicitly
                            if not 1 <= idx <= len(variants):
                                click.echo("cancelled.", err=True)
                                continue
                            picked = variants[idx - 1]
                            try:
                                msg = await coordinator.send_variant(
                                    dialog_id, result.token, picked, _coord_send,
                                    owner_id=str(dialog_id),
                                )
                            except SendForbiddenError:
                                continue
                            except OutboundError:
                                logger.warning(
                                    "outbound token rejected in chat REPL (dialog %s)", dialog_id
                                )
                                # #187: actionable English, aligned with the TUI's hint (which
                                # says "expired — pick again") instead of a bare Russian line.
                                click.echo(
                                    "Translation choice expired — type the message again to retry.",
                                    err=True,
                                )
                                continue
                            sent_ids.append((dialog_id, msg.id))
                            click.echo(f"  ↳ {line}")  # show the original under the sent variant
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

    _run_interruptible(_do(), session=session, flush_traces=True)


# --- ghostwrite (#18): auto-reply in your style in explicitly enabled DMs ---------


# --- heartbeat (#19): scheduled pings with templates and safeties ---------------


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

    # #168: status + fail-fast (the TUI traces the suggester/translator). The status goes
    # to the file log, NOT click.echo — a console line corrupts the Textual alt-screen; the
    # no-key fail-fast still surfaces because it raises before the UI starts.
    _announce_tracing(echo=logger.info)

    # #52 point 2: when no profile is fixed up front and >1 exist, defer selection to the
    # in-app ProfileScreen instead of the CLI menu — pass profiles + a deps_factory that
    # builds the whole dependency set (and re-inits per-profile logging) for the pick.
    explicit_profile = ctx.obj.get("profile")
    explicit_session = (
        ctx.get_parameter_source("session") == ParameterSource.COMMANDLINE
    )
    try:
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
            auto_translate=deps.auto_translate,
        ).run()
    except MissingCredentialsError as exc:
        # #188 Axis B: make_tui_deps builds the client up front (outside _run), so empty
        # TG_API_ID/TG_API_HASH would otherwise crash `tui` with a raw traceback before the
        # UI even opens. Surface the friendly hint instead. (The deps_factory branch above
        # builds the client inside the TUI's own _startup, whose except already shows it.)
        raise click.ClickException(str(exc)) from exc
    finally:
        flush_tracers()  # #168: drain buffered LangSmith traces when the TUI exits



# --- #179: command registration. Imported LAST so the command modules see a fully populated
# `main` module; register every relocated command/group onto the root `cli`. ---
from tg_messenger.cli.commands import agent as _agent_cmds  # noqa: E402
from tg_messenger.cli.commands import auth as _auth_cmds  # noqa: E402
from tg_messenger.cli.commands import config as _config_cmds  # noqa: E402
from tg_messenger.cli.commands import ghostwrite as _ghostwrite_cmds  # noqa: E402
from tg_messenger.cli.commands import heartbeat as _heartbeat_cmds  # noqa: E402
from tg_messenger.cli.commands import message as _message_cmds  # noqa: E402
from tg_messenger.cli.commands import serve as _serve_cmds  # noqa: E402
from tg_messenger.cli.commands import username as _username_cmds  # noqa: E402
from tg_messenger.cli.commands import watch as _watch_cmds  # noqa: E402
from tg_messenger.cli.commands import worker as _worker_cmds  # noqa: E402

for _mod in (_auth_cmds, _message_cmds, _watch_cmds, _agent_cmds, _ghostwrite_cmds,
             _heartbeat_cmds, _username_cmds, _worker_cmds, _serve_cmds, _config_cmds):
    for _cmd in _mod.COMMANDS:
        cli.add_command(_cmd)
