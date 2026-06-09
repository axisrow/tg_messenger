# tg_messenger

Standalone, reusable Telegram messenger client for manual chatting in DMs.
Three interfaces (CLI / TUI / Web) over a shared UI-agnostic core, built on Telethon.

- Runs standalone in its own venv (`tg-messenger ...`).
- Reusable as an external dependency: `from tg_messenger.core import StandaloneTelegramClient`.
- Own independent StringSession storage (`~/.tg_messenger/`), with optional injection of an
  externally supplied session string.

See `PLAN.md` for the full design and the TDD build sequence.

## Quickstart

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev,cli,tui,web]"
./.venv/bin/tg-messenger --help
```
