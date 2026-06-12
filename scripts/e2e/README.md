# Manual Real-CLI E2E Smoke Tests

These scripts are human-run smoke tests for the real installed
`tg-messenger` CLI against the real Telegram API.

They are intentionally separate from `pytest` and CI. The automated unit suite
stays fake-backed/offline; these checks exist to periodically verify the
external Telegram contract end to end.

## Prerequisites

Install the CLI from this checkout:

```bash
pip install -e ".[dev,agent,interop]"
```

Log in once:

```bash
tg-messenger login
```

Provide Telegram API credentials either in the environment or in `.env` in the
repository root. The CLI auto-loads `.env` from its current working directory,
and these scripts run from the repository root.

```bash
export TG_API_ID=12345
export TG_API_HASH=...
```

Set the profile and Saved Messages peer:

```bash
export E2E_PROFILE=default
export E2E_SAVED_ID=<your numeric Telegram user id>
export E2E_SAVED_ID_CONFIRM="$E2E_SAVED_ID"
```

`E2E_SAVED_ID` is your own numeric user id. Telegram uses that id as the
Saved Messages/self-dialog peer id, and the current CLI accepts numeric peers.
Look it up once from Telegram/account metadata or from a trusted local session
tool. A wrong value can target a real DM, so the Saved Messages mutation tier
requires `E2E_SAVED_ID_CONFIRM` to match after you verify the id. A future
`tg-messenger whoami` command could remove this manual step.

## Run Safe Checks

```bash
scripts/e2e/run_safe.sh
```

`run_safe.sh` executes:

- `01_readonly.sh`
- `02_saved_messages.sh`

It never calls `03_dangerous_parity.sh`.

Useful optional variables:

```bash
export E2E_DIALOG_QUERY="$E2E_SAVED_ID"
export E2E_SEARCH_QUERY="тест"
export E2E_USERNAME_BASE=e2esmoke
export E2E_REACTION_EMOTICON="👍"
export E2E_MUTATION_SLEEP=2
export E2E_VERBOSE=1
```

`E2E_SEARCH_QUERY` defaults to `тест`. Reactions run against Saved Messages and
are skipped when Telegram rejects them for account-policy reasons.

## Safety Tiers

### Tier 1: `01_readonly.sh`

Read-only checks. Safe to run periodically.

- command help smoke
- `profiles`
- `dialogs`, `dialogs --groups`, `dialogs --find`
- `read`, `search`
- `read --download` into a temp directory
- low-limit `username suggest`
- local list commands for heartbeat, moderation rules, ghostwrite dialogs

### Tier 2: `02_saved_messages.sh`

Safe reversible mutations, confined to Saved Messages.

The script creates unique `e2e-...` markers, recovers message ids from
`tg-messenger read` output, and deletes its created messages. Cleanup also runs
best-effort on exit.

Covered scenarios:

- send/read/delete text
- reply
- edit
- best-effort reaction
- forward Saved Messages to Saved Messages
- forward a comma-separated id list
- send generated file with explicit `--caption`
- send generated file as a document
- delete a created Saved Messages message with `--for-me`
- mark-read
- reaction event round-trip through `chat`
- local SQLite CRUD for `moderate-rules`, `heartbeat`, and `ghostwrite-dialogs`

### Dangerous parity: `03_dangerous_parity.sh`

Dangerous scenarios are documented for parity only and intentionally not
automated. This script is never run by `run_safe.sh`, and it does not call
`tg-messenger`.

Run it to print the parity stub:

```bash
scripts/e2e/03_dangerous_parity.sh
```

Dangerous means destructive or externally visible real-state operations outside
Saved Messages, such as:

- deleting real messages outside Saved Messages
- `logout` or profile/session destruction
- public `username set` / `username clear`
- server-side scheduled sends to a real peer
- destructive group/account operations

See `PARITY.md` for the full CLI coverage map.

## Notes

- Scripts print `PASS`, `FAIL`, and `SKIP` lines plus a final summary.
- Any `FAIL` produces a non-zero exit code.
- `SKIP` is used for optional or Telegram-policy-dependent checks, such as
  reactions in Saved Messages.
- Scripts never print session strings, login codes, or phone numbers.
- Mutating Telegram calls are sequential and sleep between operations to reduce
  flood risk.
