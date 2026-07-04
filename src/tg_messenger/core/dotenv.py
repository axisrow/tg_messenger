"""Merge-writer for the persistent ``~/.tg/.env`` (#188 Axis C).

Lets a user SAVE ``TG_API_ID`` / ``TG_API_HASH`` to disk without hand-editing —
via ``tg-messenger config set-api`` or the auto-prompt on ``tg-messenger login``.
Lives in ``core`` (next to :mod:`tg_messenger.core.paths`) so the CLI can import it
without a ``core -> cli`` cycle. Reading reuses :func:`tg_messenger.cli.parsers._parse_dotenv`
in the CLI callers; this module does its own tiny inline parse on read so it never
imports ``cli`` (and stays usable by non-CLI front-ends).

The writer MERGES — it never clobbers keys it wasn't told about (e.g.
``SESSION_ENCRYPTION_KEY``, ``TG_SEND_RATE``). Only the passed ``updates`` win;
everything else is re-serialized verbatim. Permissions mirror :meth:`SessionStore.save`
(``auth.py``): dir ``0700``, file ``0600`` — creds are a new persistent secret.

Credential VALUES are NEVER logged here or by callers.
"""

from __future__ import annotations

import os
from pathlib import Path

from tg_messenger.core import paths as core_paths


def _parse_dotenv_lines(path: Path) -> dict[str, str]:
    """Read KEY=VALUE pairs from a .env file (quotes stripped); missing file -> {}.

    A deliberately tiny copy of :func:`tg_messenger.cli.parsers._parse_dotenv` so this
    module stays free of a ``core -> cli`` import. Kept in sync with that one; if the
    parser grows features the writer needs, extend here, not by importing cli.
    """
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


def write_env_values(updates: dict[str, str], *, path: Path | None = None) -> Path:
    """Merge ``updates`` into the persistent ``~/.tg/.env`` without clobbering other keys.

    The target is :data:`tg_messenger.core.paths.DEFAULT_HOME` / ``.env`` by default
    (the fixed ``~/.tg/.env`` the Axis B missing-creds hint points users at) — NOT the
    active ``tg_home()`` root. The hint and ``config set-api`` always tell users the
    same path, so the writer must always write that same path regardless of legacy
    fallback or a ``TG_HOME`` override. Tests point this at ``tmp_path`` via the
    ``path`` kwarg (monkeypatching ``DEFAULT_HOME`` would race ``tg_home()``'s cache).

    - Reads existing pairs with :func:`_parse_dotenv_lines` (``{}`` if absent).
    - Updates ONLY the passed keys; every other key (``SESSION_ENCRYPTION_KEY``,
      ``TG_SEND_RATE`` …) is preserved.
    - Re-serializes ``KEY=VALUE`` one per line (sorted for stable diffs).
    - ``mkdir(parents=True, exist_ok=True)`` + ``chmod 0o700`` on the dir;
      ``write_text`` + ``chmod 0o600`` on the file — mirror :meth:`SessionStore.save`.

    Returns the path written. NEVER logs the values.
    """
    if path is None:
        # read DEFAULT_HOME at CALL time (not import) so a test monkeypatching
        # ``paths.DEFAULT_HOME`` reaches us — same reason tg_home() reads it live.
        path = core_paths.DEFAULT_HOME / ".env"

    merged = _parse_dotenv_lines(path)
    merged.update(updates)

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    serialized = "\n".join(f"{key}={value}" for key, value in sorted(merged.items()))
    path.write_text(serialized + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path
