#!/usr/bin/env bash
# Dangerous/account-visible parity stub. This script is intentionally
# non-mutating and is never called by run_safe.sh.

set -uo pipefail

cat <<'EOF'
Dangerous scenarios are documented for parity only and intentionally not automated.

Safe automated analogs live in:
- scripts/e2e/01_readonly.sh
- scripts/e2e/02_saved_messages.sh
- scripts/e2e/03_optional_safe.sh
- scripts/e2e/04_guided_events.sh
- scripts/e2e/run_safe.sh

Parity-only dangerous scenarios:

| Scenario | Reason not automated | Safe parity |
| --- | --- | --- |
| real dialog send/delete outside Saved Messages | externally visible message mutation | send/edit/delete in Saved Messages |
| deleting real messages outside Saved Messages | destructive user-visible state change | delete created Saved Messages artifacts |
| username set/clear | public account identity mutation | username suggest read-only check |
| heartbeat --at to a real peer | server-side scheduled external send with no CLI cancel | heartbeat --interval local CRUD |
| logout | destroys current Telegram session | profiles read-only listing |
| profiles remove | deletes local session profile | profiles read-only listing |
| group/account destructive operations | irreversible or broad external state changes | no automated analog |

See scripts/e2e/PARITY.md for the full CLI parity map.
EOF
