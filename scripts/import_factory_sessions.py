#!/usr/bin/env python3
"""Manually import Telegram account sessions from a tg_content_factory SQLite DB
into tg_messenger's per-profile session store.

This is a human-run, one-off migration helper (same spirit as scripts/e2e/):
it is NEVER wired into pytest/CI, and it never writes to the tg_content_factory
database — it only opens it read-only (SQLite ``mode=ro``) to fetch the
``accounts`` table's ``session_string`` column.

Usage:

    python scripts/import_factory_sessions.py --db /path/to/tg_search.db          # dry-run
    python scripts/import_factory_sessions.py --db /path/to/tg_search.db --apply  # writes profiles

The Fernet key must be the SAME `SESSION_ENCRYPTION_KEY` tg_content_factory
uses to encrypt those rows (`enc:v2:` format, byte-compatible between the two
projects). Pass it via --key, or (safer — avoids shell history) via the
SESSION_ENCRYPTION_KEY / TG_FACTORY_SESSION_KEY env var, or a --key-file.

Imported profiles are named "<prefix><phone-without-plus>" (default prefix:
"factory_"); an existing profile of the same name is never overwritten.
Session strings/keys are never logged — only lengths/prefixes are printed
for a sanity check, matching the project-wide "no secrets in logs" rule.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Make an in-checkout `src/` layout importable without requiring the package
# to be installed (mirrors how scripts/e2e drives the *installed* CLI instead —
# here we need the library internals, so we add src/ to sys.path directly).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from tg_messenger.core.auth import SessionStore  # noqa: E402
from tg_messenger.core.names import is_safe_profile_name  # noqa: E402
from tg_messenger.core.session_cipher import decrypt_session, is_encrypted  # noqa: E402


def profile_name(prefix: str, phone: str | None) -> str | None:
    """Build the target profile name, or None when ``phone`` is unusable.

    Pure and side-effect-free so it's independently testable. Returns None
    (never raises) for a missing/blank phone, matching the loop's existing
    "skip this row, keep going" contract for other bad-data cases (a NULL
    ``phone`` column crashing the whole import would be worse than skipping
    one row of a human-run migration helper).
    """
    if not phone or not phone.strip():
        return None
    return prefix + phone.lstrip("+")


def resolve_key(args: argparse.Namespace) -> str | None:
    if args.key:
        return args.key
    if args.key_file:
        try:
            return Path(args.key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"error: could not read --key-file {args.key_file!r}: {exc}", file=sys.stderr)
            return None
    return os.environ.get("SESSION_ENCRYPTION_KEY") or os.environ.get(
        "TG_FACTORY_SESSION_KEY"
    )


def fetch_accounts(db_path: str) -> list[tuple[int, str | None, bool, str | None]]:
    """Read (id, phone, is_primary, session_string) rows, read-only.

    ``phone``/``session_string`` are nullable in practice (e.g. a factory
    account row that was never actually logged in) — the caller must handle
    both as absent, not assume a well-formed string.
    """
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        cur = con.execute(
            "SELECT id, phone, is_primary, session_string FROM accounts"
        )
        return [(row[0], row[1], bool(row[2]), row[3]) for row in cur.fetchall()]
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the tg_content_factory SQLite DB (e.g. data/tg_search.db)",
    )
    parser.add_argument(
        "--key",
        help="SESSION_ENCRYPTION_KEY value (prefer --key-file or the env var instead)",
    )
    parser.add_argument(
        "--key-file",
        help="Path to a file containing just the key",
    )
    parser.add_argument(
        "--prefix",
        default="factory_",
        help="Profile name prefix (default: 'factory_')",
    )
    parser.add_argument(
        "--session-dir",
        help="Override tg_messenger session dir (default: resolved from TG_HOME/legacy state)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write profiles. Without this flag, only a dry-run preview is printed.",
    )
    args = parser.parse_args()

    key = resolve_key(args)
    if not key:
        print(
            "error: no encryption key given. Pass --key, --key-file, or set "
            "SESSION_ENCRYPTION_KEY in the environment.",
            file=sys.stderr,
        )
        return 2

    if not Path(args.db).is_file():
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2

    rows = fetch_accounts(args.db)
    if not rows:
        print("no accounts found in the factory DB — nothing to do")
        return 0

    store = SessionStore(session_dir=args.session_dir, encryption_key=key)
    existing = set(store.list_profiles())

    print(f"found {len(rows)} account(s) in {args.db}")
    print(f"existing tg_messenger profiles: {sorted(existing) or '(none)'}")
    print(f"mode: {'APPLY (writing)' if args.apply else 'DRY-RUN (preview only)'}")
    print()

    for id_, phone, is_primary, enc in rows:
        profile = profile_name(args.prefix, phone)
        if profile is None:
            print(f"[skip] id={id_} phone={phone!r}: missing/blank phone — cannot name a profile")
            continue
        if not is_safe_profile_name(profile):
            print(
                f"[skip] id={id_} phone={phone}: generated profile name {profile!r} is not "
                "canonical (would collapse onto a different file via sanitization) — "
                "refusing to guess; fix --prefix or the source phone value"
            )
            continue
        if not enc:
            print(f"[skip] id={id_} phone={phone}: session_string is empty/NULL — nothing to import")
            continue
        if not is_encrypted(enc):
            print(
                f"[skip] id={id_} phone={phone}: session_string is not enc:v2: "
                "(factory DB has no SESSION_ENCRYPTION_KEY set — plaintext row); "
                "pass --key matching what encrypted it, or handle manually"
            )
            continue
        try:
            plain = decrypt_session(enc, key)
        except Exception as exc:  # noqa: BLE001 — report and continue with other rows
            print(f"[fail] id={id_} phone={phone}: decrypt failed: {exc}")
            continue

        if profile in existing:
            print(f"[skip] profile '{profile}' already exists — not overwriting")
            continue

        marker = " (primary)" if is_primary else ""
        # Claim the name NOW, in-memory, before moving to the next row — two
        # rows in this same DB can normalize to the same profile (e.g. "+123"
        # and "123"), and without this the second row's `profile in existing`
        # check above would still pass, silently overwriting what this row
        # just wrote (or would have written in dry-run).
        existing.add(profile)
        if args.apply:
            store.save(profile, plain)
            print(
                f"[ok] id={id_} phone={phone}{marker} -> profile '{profile}' "
                f"saved (session_len={len(plain)})"
            )
        else:
            print(
                f"[dry-run] id={id_} phone={phone}{marker} -> would save profile "
                f"'{profile}' (session_len={len(plain)})"
            )

    if not args.apply:
        print()
        print("dry-run only — re-run with --apply to actually write session files")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
