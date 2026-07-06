"""CLI auth commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import logging
import sys

import click

from tg_messenger.cli import main as cli_main
from tg_messenger.cli.commands.config import prompt_and_save_api_creds
from tg_messenger.core.client import credentials_missing_from_env

logger = logging.getLogger(__name__)


def _maybe_prompt_for_creds() -> None:
    """Auto-prompt to save creds on ``login`` when they're still missing (#188 Axis C).

    After the root group's ``_load_dotenv`` has layered every ``.env`` into the env,
    if creds are STILL absent AND stdin is a tty, offer to save them right here so a
    brand-new user can go from ``tg-messenger login`` straight to logged-in without
    hand-editing ``~/.tg/.env``. The saved file is read on the NEXT process, so we also
    fold the just-written values into the live env so THIS login proceeds without a
    restart.

    Non-interactive (piped/no tty) -> no prompt, no hang: ``make_client`` will raise
    :class:`MissingCredentialsError` and surface the friendly hint. ``--import-session``
    never reaches here (it ``return``s upstream) so we don't steal its stdin.
    """
    import os

    if not credentials_missing_from_env():
        return
    if not sys.stdin.isatty():
        return
    click.echo("Telegram API credentials aren't set yet. Let's save them to ~/.tg/.env.")
    # prompt+validate+write lives in config.py (shared with `config set-api`). It returns
    # the just-validated values so we fold ONLY TG_API_ID/TG_API_HASH into this process —
    # NOT the whole file. Re-importing the whole ~/.tg/.env would clobber unrelated keys
    # the user set in the REAL env / cwd .env (SESSION_ENCRYPTION_KEY, TG_HOME, …) and
    # break _load_dotenv's documented precedence (real env always wins). We reached here
    # only because those two were missing, so setdefault fills exactly the gap.
    _path, api_id, api_hash = prompt_and_save_api_creds()
    os.environ.setdefault("TG_API_ID", api_id)
    os.environ.setdefault("TG_API_HASH", api_hash)


@click.command()
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
        cli_main._export_session(session)
        return
    if import_session:
        cli_main._import_session(session)
        return

    if not phone:
        phone = click.prompt("Phone number in international format")

    # #188 Axis C: if creds are still missing after _load_dotenv (and stdin is a tty),
    # offer to save them now so login can continue. Non-interactive -> falls through to
    # make_client's MissingCredentialsError (no hang). --import-session returns above.
    _maybe_prompt_for_creds()

    async def _do():
        client = cli_main.make_client(session_name=session)
        await client.connect()
        try:
            flow = LoginFlow(client._client)
            try:
                delivery = await flow.send_code(phone)
            except RPCError as exc:
                raise click.ClickException(f"Could not send code: {exc}") from exc
            click.echo(cli_main._delivery_message(delivery))
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
                click.echo(cli_main._delivery_message(delivery))
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

    cli_main._run(_do())


@click.group(invoke_without_command=True)
@click.pass_context
def profiles(ctx: click.Context) -> None:
    """List saved account profiles (sessions on disk)."""
    if ctx.invoked_subcommand is not None:
        return
    store = cli_main._session_store()
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
    store = cli_main._session_store()
    if not store.path_for(name).is_file():
        raise click.ClickException(f"no saved session for profile {name!r}.")
    if not yes:
        click.confirm(f"Delete the session file for profile {name!r}?", abort=True)
    store.delete(name)
    click.echo(f"profile {name!r} removed.")


@click.command()
@click.option("--session", default="default")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def logout(ctx: click.Context, session: str, yes: bool) -> None:
    """Log out of Telegram (best-effort) and delete the profile's session file.

    The Telegram-side log_out invalidates the auth key on the server; a dead or
    revoked session does not block the local file removal.
    """
    session = cli_main._effective_session(ctx, session)
    store = cli_main._session_store()
    if not store.path_for(session).is_file():
        raise click.ClickException(f"no saved session for profile {session!r}.")
    if not yes:
        click.confirm(
            f"Log out profile {session!r} from Telegram and delete its session file?",
            abort=True,
        )

    async def _do():
        client = cli_main.make_client(session_name=session)
        await client.connect()
        try:
            await client.log_out()
        finally:
            await client.disconnect()

    try:
        cli_main._run(_do(), session=session)
        click.echo("logged out of Telegram.")
    except Exception as exc:
        # best-effort by design: a dead session must not keep its file alive
        logger.warning("telegram log_out failed for profile %r: %s", session, exc)
        click.echo(f"⚠ Telegram log_out failed ({exc}) — deleting the local session anyway.",
                   err=True)
    store.delete(session)
    click.echo(f"session file removed for profile {session!r}.")


COMMANDS = [login, logout, profiles]
