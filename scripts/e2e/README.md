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
```

`E2E_SAVED_ID` is your own numeric user id. Telegram uses that id as the
Saved Messages/self-dialog peer id, and the current CLI accepts numeric peers.
Look it up once from Telegram/account metadata or from a trusted local session
tool. A future `tg-messenger whoami` command could remove this manual step.

## Run Safe Checks

```bash
scripts/e2e/run_safe.sh
```

`run_safe.sh` executes:

- `01_readonly.sh`
- `02_mutations_saved.sh`

It never calls `03_manual.sh`.

Useful optional variables:

```bash
export E2E_DIALOG_QUERY="$E2E_SAVED_ID"
export E2E_SEARCH_QUERY="some text expected in Saved Messages"
export E2E_USERNAME_BASE=e2esmoke
export E2E_REACTION_EMOTICON="👍"
export E2E_MUTATION_SLEEP=2
export E2E_VERBOSE=1
```

`E2E_REACT_PEER` can point reaction checks at a throwaway group or peer when
Saved Messages rejects reactions. Setting it means the mutation script will
send and delete a temporary marker in that explicit peer.

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

### Tier 2: `02_mutations_saved.sh`

Reversible mutations, confined to Saved Messages by default.

The script creates unique `e2e-...` markers, recovers message ids from
`tg-messenger read` output, and deletes its created messages. Cleanup also runs
best-effort on exit.

Covered scenarios:

- send/read/delete text
- reply
- edit
- best-effort reaction
- forward Saved Messages to Saved Messages
- send generated file with caption
- send generated file as a document
- mark-read
- reaction event round-trip through `chat`
- local SQLite CRUD for `moderate-rules`, `heartbeat`, and `ghostwrite-dialogs`

### Tier 3: `03_manual.sh`

Dangerous/account-visible checks. This script is never run by `run_safe.sh`.

It requires:

```bash
export E2E_I_UNDERSTAND=1
scripts/e2e/03_manual.sh
```

It also asks interactive `y/N` before every step. Targets have no defaults; set
only the variables for the checks you intend to run:

```bash
export E2E_REAL_PEER=<explicit peer id>
export E2E_USERNAME_TEST_NAME=<temporary_public_username>
export E2E_HEARTBEAT_PEER=<explicit peer id>
export E2E_HEARTBEAT_AT=23:59
export E2E_FACTORY_URL=http://127.0.0.1:8000
export E2E_RUN_SERVE=1
export E2E_RUN_TUI=1
```

Manual tier scenarios include real-dialog send/delete, username set/clear,
server-side scheduled sends, enforcing/long-running services, `serve`, `tui`,
`logout`, and profile removal.

## Notes

- Scripts print `PASS`, `FAIL`, and `SKIP` lines plus a final summary.
- Any `FAIL` produces a non-zero exit code.
- `SKIP` is used for optional or Telegram-policy-dependent checks, such as
  reactions in Saved Messages.
- Scripts never print session strings, login codes, or phone numbers.
- Mutating Telegram calls are sequential and sleep between operations to reduce
  flood risk.
