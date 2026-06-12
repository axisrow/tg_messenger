# Manual E2E CLI Parity

Manual real-CLI E2E checks are grouped by real risk. Operations that stay
inside Saved Messages and delete their own artifacts are safe. Dangerous means
destructive or externally visible real-state changes outside Saved Messages.

## Coverage Map

| CLI surface | Status | Coverage |
| --- | --- | --- |
| `--help` / command help | safe automated | `01_readonly.sh` |
| `profiles` | safe automated | `01_readonly.sh` |
| `profiles remove` | parity stub only | Dangerous local session deletion; see `99_dangerous_parity.sh`. |
| `login` | not applicable | Interactive setup prerequisite, not smoke-tested. |
| `logout` | parity stub only | Session destruction; see `99_dangerous_parity.sh`. |
| `dialogs`, `dialogs --groups`, `dialogs --find` | safe automated | `01_readonly.sh` |
| `read`, `read --download` | safe automated | `01_readonly.sh` |
| `search` | safe automated | `01_readonly.sh`, default query `тест`. |
| `send` text | safe automated | `02_saved_messages.sh` in Saved Messages. |
| `send --reply-to` | safe automated | `02_saved_messages.sh` in Saved Messages. |
| `send --file --caption` | safe automated | `02_saved_messages.sh` with explicit `--caption`. |
| `send --file --as-file` | safe automated | `02_saved_messages.sh`. |
| `send --voice` | safe optional | `02_saved_messages.sh`, gated by `E2E_VOICE_FILE`. |
| `send --video-note` | safe optional | `02_saved_messages.sh`, gated by `E2E_VIDEO_NOTE_FILE`. |
| `edit` | safe automated | `02_saved_messages.sh` in Saved Messages. |
| `react` | safe optional | `02_saved_messages.sh`, best-effort because Telegram may reject Saved Messages reactions. |
| `forward` single id | safe automated | `02_saved_messages.sh`. |
| `forward` id list | safe automated | `02_saved_messages.sh`. |
| `delete` created Saved Messages | safe automated | `02_saved_messages.sh`. |
| `delete --for-me` in Saved Messages | safe automated | `02_saved_messages.sh`. |
| deleting real messages outside Saved Messages | parity stub only | Dangerous external state mutation; see `99_dangerous_parity.sh`. |
| `mark-read` in Saved Messages | safe automated | `02_saved_messages.sh`. |
| `listen` | guided manual | `04_guided_events.sh`; operator triggers a controlled incoming DM/bot reply. |
| `watch` | guided manual | `04_guided_events.sh`; operator performs a throwaway group deletion scenario. |
| `chat` reaction listener | safe optional | `02_saved_messages.sh`, best-effort with reactions. |
| `chat` REPL send | safe automated | `03_optional_safe.sh`, sends to Saved Messages and cleans up. |
| `chat` REPL `/react` | safe optional | `03_optional_safe.sh`, best-effort because Saved Messages reactions may be rejected. |
| `moderate-rules` CRUD | safe automated | `02_saved_messages.sh`, local SQLite only. |
| `moderate` dry-run | safe optional | `03_optional_safe.sh`, timed startup gated by `E2E_RUN_SERVICES=1`. |
| `moderate --enforce` | parity stub only | Can delete or otherwise affect real chats. |
| `ghostwrite-dialogs` CRUD | safe automated | `02_saved_messages.sh`, local SQLite only. |
| `ghostwrite` dry-run | safe optional | `03_optional_safe.sh`, timed startup gated by `E2E_RUN_SERVICES=1` and `E2E_ALLOW_LLM=1`. |
| `ghostwrite --enforce` | parity stub only | Can send externally visible replies. |
| `heartbeat plan --interval/list/remove` | safe automated | `02_saved_messages.sh`, local SQLite only. |
| `heartbeat plan --at` | parity stub only | Server-side scheduled external send can be hard to cancel. |
| `heartbeat run` | safe optional | `03_optional_safe.sh`, startup-only and skipped when stored plans exist. |
| `username suggest` | safe automated | `01_readonly.sh`. |
| `username set`, `username clear` | parity stub only | Public account identity mutation. |
| `suggest` dry-run | safe optional | `03_optional_safe.sh`, gated by `TG_AGENT_MODEL`, `E2E_ALLOW_LLM=1`, and `E2E_SUGGEST_DM`. |
| `suggest --learn` | safe optional | `03_optional_safe.sh`, additionally gated by `E2E_SUGGEST_LEARN=1`. |
| `suggest --send` | safe optional follow-up | `03_optional_safe.sh` skips it until the CLI exposes the sent message id for safe cleanup. |
| `agent` | safe optional follow-up | Needs agent env and bounded dry-run harness. |
| `worker` | safe optional follow-up | Needs controlled factory URL fixture. |
| `serve` | safe optional | `03_optional_safe.sh`, localhost `/login` HTTP assertion gated by `E2E_RUN_SERVICES=1`. |
| `tui` | safe optional follow-up | Interactive smoke only. |

## Dangerous Stub

`99_dangerous_parity.sh` is intentionally non-mutating. It prints the dangerous
parity table and exits without calling `tg-messenger`.
