# Scripts

Manual, human-run helper scripts. Never wired into `pytest`/CI (same rule as
`scripts/e2e/`).

## `import_factory_sessions.py`

One-off migration helper: imports Telegram account sessions from a
`tg_content_factory` SQLite DB (`accounts.session_string`, `enc:v2:` format)
into tg_messenger profiles (`~/.tg/sessions/`, or the legacy
`~/.tg_messenger/sessions/` if that's what's in use — see `core/auth.py`).

- Opens the factory DB **read-only** (`sqlite3 mode=ro`) — it never writes to
  tg_content_factory.
- Requires the SAME `SESSION_ENCRYPTION_KEY` tg_content_factory used to
  encrypt those rows (SSO-compatible `enc:v2:` Fernet format, see
  `core/session_cipher.py`).
- Dry-run by default; pass `--apply` to actually write session files.
- Never overwrites an existing tg_messenger profile of the same name.
- Never logs/prints session strings or the key — only lengths and phone
  numbers (already present in the source DB, not a new secret).

```bash
# preview (safe, no writes)
python scripts/import_factory_sessions.py --db /path/to/tg_search.db

# actually import, key from env (avoids shell history)
export SESSION_ENCRYPTION_KEY=...   # same key as tg_content_factory's .env
python scripts/import_factory_sessions.py --db /path/to/tg_search.db --apply
```

`--prefix` (default `factory_`) controls the imported profile name prefix;
`--key-file PATH` reads the key from a file instead of `--key`/env.

This is a stopgap: today it's a manual step you run when you need to pull an
account tg_content_factory already has logged in. A future iteration could
automate it further, but for now this script is deliberately explicit and
read-only against factory's DB.
