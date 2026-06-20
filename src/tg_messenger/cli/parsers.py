"""Pure argument/.env parsers for the CLI.

No seam, no network — string/file parsing only, so they are unit-testable standalone and carry no
import-isolation risk. Re-exported from ``tg_messenger.cli.main`` for backward-compatible imports
(``test_e2e`` imports ``_parse_dotenv`` from there).
"""

from __future__ import annotations

from pathlib import Path

import click


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


def _parse_ids(raw: str) -> list[int]:
    """Parse a comma-separated message-id list; a bad token is a ClickException."""
    try:
        return [int(p.strip()) for p in raw.split(",") if p.strip()]
    except ValueError as exc:
        raise click.ClickException(f"invalid message id list: {raw!r}") from exc


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
