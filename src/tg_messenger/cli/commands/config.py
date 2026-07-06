"""CLI ``config`` command group (#188 Axis C).

Saves Telegram API credentials to the persistent ``~/.tg/.env`` without hand-editing
the file by hand:

    tg-messenger config set-api --api-id 1234567 --api-hash deadbeef...

The same prompt+validate+write path is shared with the auto-prompt on ``tg-messenger
login`` (see :func:`prompt_and_save_api_creds`), so the two entry points can never
drift apart. Credentials are NEVER echoed (success only names the file written).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import click

from tg_messenger.core.dotenv import write_env_values

logger = logging.getLogger(__name__)


def prompt_and_save_api_creds(
    *,
    api_id: str | None = None,
    api_hash: str | None = None,
    prompt: Callable[..., str] | None = None,
) -> tuple[Path, str, str]:
    """Prompt for (if missing) and persist ``TG_API_ID`` / ``TG_API_HASH`` to ~/.tg/.env.

    Shared by ``config set-api`` and the ``login`` auto-prompt fallback so the two
    paths stay byte-identical. Each value is taken from the argument when given,
    otherwise prompted interactively (``api_hash`` hidden). Validation runs before
    anything is written — a bad ``api_id`` (non-numeric) or blank ``api_hash`` raises
    :class:`click.ClickException` with the friendly hint tone of
    :data:`MISSING_CREDENTIALS_HINT`, never a raw ValueError.

    ``prompt`` defaults to :func:`click.prompt` resolved at CALL time (so a test
    monkeypatching ``click.prompt`` reaches us — a default bound at definition would
    freeze the pre-patch function). Returns ``(path, api_id, api_hash)``: the path
    written plus the two validated values, so the ``login`` caller can fold ONLY those
    into the live env (not the whole file — see auth.py). The values are returned for
    in-process use; they are NEVER echoed/logged here.
    """
    from tg_messenger.core.client import MISSING_CREDENTIALS_HINT

    prompt = prompt or click.prompt

    if api_id is None:
        api_id = prompt("TG_API_ID (from https://my.telegram.org)")
    if api_hash is None:
        api_hash = prompt("TG_API_HASH", hide_input=True)

    api_id = (api_id or "").strip()
    api_hash = (api_hash or "").strip()

    # Validate api_id by the SAME conversion client_from_env uses (int()), not isdigit():
    # isdigit() accepts Unicode digits ('²', '๓') that then crash int() downstream and
    # coerce to 0 — a confusing "missing creds" after a "passed" check. The error message
    # is generic on purpose: the supplied value is credential-shaped and must never be
    # echoed (a swapped-field paste of the hash into the api_id prompt would otherwise
    # leak it into terminal/CI output).
    try:
        int(api_id)
    except ValueError:
        raise click.ClickException(
            f"TG_API_ID must be a number.\n{MISSING_CREDENTIALS_HINT}"
        )
    if not api_hash:
        raise click.ClickException(
            f"TG_API_HASH must not be empty.\n{MISSING_CREDENTIALS_HINT}"
        )

    path = write_env_values({"TG_API_ID": api_id, "TG_API_HASH": api_hash})
    logger.debug("wrote API credentials to %s", path)
    return path, api_id, api_hash


@click.group()
def config() -> None:
    """Read/write persistent tg-messenger config (~/.tg/.env)."""


@config.command("set-api")
@click.option("--api-id", "api_id", default=None, help="Telegram TG_API_ID (numeric).")
@click.option("--api-hash", "api_hash", default=None, help="Telegram TG_API_HASH.")
def set_api(api_id: str | None, api_hash: str | None) -> None:
    """Save TG_API_ID / TG_API_HASH to ~/.tg/.env (0600; values never echoed).

    Omit a flag to be prompted for it interactively (the hash is hidden). Validates
    before writing: a non-numeric api_id or blank api_hash aborts with a helpful hint.
    """
    path, _, _ = prompt_and_save_api_creds(api_id=api_id, api_hash=api_hash)
    click.echo(
        f"Saved API credentials to {path} (0600). They are read from any directory now."
    )


COMMANDS = [config]
