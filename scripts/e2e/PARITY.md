# Manual E2E CLI Parity

Manual real-CLI E2E checks are grouped by real risk. Operations that stay
inside Saved Messages and delete their own artifacts are safe. Dangerous means
destructive or externally visible real-state changes outside Saved Messages.

## Coverage Map

| CLI surface | Status | Coverage |
| --- | --- | --- |
| `--help` / command help | safe automated | `01_readonly.sh` |
| `profiles` | safe automated | `01_readonly.sh` |
| `profiles remove` | parity stub only | Dangerous local session deletion; see `03_dangerous_parity.sh`. |
| `login` | not applicable | Interactive setup prerequisite, not smoke-tested. |
| `logout` | parity stub only | Session destruction; see `03_dangerous_parity.sh`. |
| `dialogs`, `dialogs --groups`, `dialogs --find` | safe automated | `01_readonly.sh` |
| `read`, `read --download` | safe automated | `01_readonly.sh` |
| `search` | safe automated | `01_readonly.sh`, default query `тест`. |
| `send` text | safe automated | `02_saved_messages.sh` in Saved Messages. |
| `send --reply-to` | safe automated | `02_saved_messages.sh` in Saved Messages. |
| `send --file --caption` | safe automated | `02_saved_messages.sh` with explicit `--caption`. |
| `send --file --as-file` | safe automated | `02_saved_messages.sh`. |
| `send --voice`, `send --video-note` | safe optional follow-up | Needs env-provided valid media fixtures. |
| `edit` | safe automated | `02_saved_messages.sh` in Saved Messages. |
| `react` | safe optional | `02_saved_messages.sh`, best-effort because Telegram may reject Saved Messages reactions. |
| `forward` single id | safe automated | `02_saved_messages.sh`. |
| `forward` id list | safe automated | `02_saved_messages.sh`. |
| `delete` created Saved Messages | safe automated | `02_saved_messages.sh`. |
| `delete --for-me` in Saved Messages | safe automated | `02_saved_messages.sh`. |
| deleting real messages outside Saved Messages | parity stub only | Dangerous external state mutation; see `03_dangerous_parity.sh`. |
| `mark-read` in Saved Messages | safe automated | `02_saved_messages.sh`. |
| `listen` | safe optional follow-up | Requires controlled incoming event source. |
| `watch` | safe optional follow-up | Requires controlled group deletion event source. |
| `chat` reaction listener | safe optional | `02_saved_messages.sh`, best-effort with reactions. |
| `moderate-rules` CRUD | safe automated | `02_saved_messages.sh`, local SQLite only. |
| `moderate --enforce` | parity stub only | Can delete or otherwise affect real chats. |
| `ghostwrite-dialogs` CRUD | safe automated | `02_saved_messages.sh`, local SQLite only. |
| `ghostwrite --enforce` | parity stub only | Can send externally visible replies. |
| `heartbeat plan --interval/list/remove` | safe automated | `02_saved_messages.sh`, local SQLite only. |
| `heartbeat plan --at` | parity stub only | Server-side scheduled external send can be hard to cancel. |
| `heartbeat run` | safe optional follow-up | Needs a no-op plan fixture or isolated Saved Messages-only plan. |
| `username suggest` | safe automated | `01_readonly.sh`. |
| `username set`, `username clear` | parity stub only | Public account identity mutation. |
| `suggest` dry-run | safe optional follow-up | Needs `TG_AGENT_MODEL` and LLM credentials. |
| `suggest --learn`, `suggest --send` | safe optional follow-up | Needs agent setup; `--send` must target Saved Messages only. |
| `agent` | safe optional follow-up | Needs agent env and bounded dry-run harness. |
| `worker` | safe optional follow-up | Needs controlled factory URL fixture. |
| `serve` | safe optional follow-up | Should assert HTTP response from localhost. |
| `tui` | safe optional follow-up | Interactive smoke only. |

## Dangerous Stub

`03_dangerous_parity.sh` is intentionally non-mutating. It prints the dangerous
parity table and exits without calling `tg-messenger`.
