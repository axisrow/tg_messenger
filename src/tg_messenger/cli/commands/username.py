"""CLI username commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import click

from tg_messenger.cli import main as cli_main


@click.group()
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

    # #187: a one-line status before the (billed, sequential) availability probes — on
    # stderr so it doesn't pollute the name list a script may parse from stdout.
    click.echo("Checking username availability…", err=True)
    free, unchecked = cli_main._run(cli_main._with_client(session, _do), session=session)
    if not free and not unchecked:
        click.echo("No available usernames found — try a different base.")
        return
    # #187: pair each glyph with a word so the status isn't glyph-only (a screen reader
    # says "check mark"/"question mark" with no meaning). Free names are verified
    # available (✓ free); unchecked candidates were generated but never probed (? unchecked).
    for name in free:
        click.echo(f"{name} ✓ free")
    for name in unchecked:
        click.echo(f"{name} ? unchecked")


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

    cli_main._run(cli_main._with_client(session, _do), session=session)
    click.echo(f"username set to @{name}.")


@username.command("clear")
@click.option("--session", default="default")
def username_clear(session: str) -> None:
    """Remove this account's public username."""

    async def _do(client):
        await client.clear_username()

    cli_main._run(cli_main._with_client(session, _do), session=session)
    click.echo("username cleared.")


COMMANDS = [username]
