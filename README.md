# tg_messenger

[![CI](https://github.com/axisrow/tg_messenger/actions/workflows/ci.yml/badge.svg)](https://github.com/axisrow/tg_messenger/actions/workflows/ci.yml)

Standalone, reusable Telegram messenger client for manual chatting in DMs.
Three interfaces (CLI / TUI / Web) over a shared UI-agnostic core, built on Telethon.

- Runs standalone in its own venv (`tg-messenger ...`).
- Reusable as an external dependency: `from tg_messenger.core import StandaloneTelegramClient`.
- Own independent StringSession storage (`~/.tg_messenger/`), with optional injection of an
  externally supplied session string.

See `PLAN.md` for the full design and the TDD build sequence.

## Quickstart

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/tg-messenger --help
```

The base install is **core + CLI**. The Web and TUI front-ends are optional extras —
`tg-messenger serve` / `tg-messenger tui` without the extra fail with a hint pointing at it.
`[dev]` pulls `[web]` + `[tui]` so the full test/lint toolchain and every interface run locally.

```bash
pip install tg-messenger          # core + CLI
pip install 'tg-messenger[web]'   # + FastAPI web UI  (tg-messenger serve)
pip install 'tg-messenger[tui]'   # + Textual TUI     (tg-messenger tui)
pip install 'tg-messenger[all]'   # web + tui + agent
```

## Use as a library

`tg_messenger` ships a `py.typed` marker and a pinned public API — import the core
client without dragging in any UI stack:

```python
import asyncio
from tg_messenger import StandaloneTelegramClient

async def main():
    client = StandaloneTelegramClient(
        api_id=12345,
        api_hash="...",
        session_string="...",   # or session_name=... for on-disk StringSession
    )
    await client.connect()
    for dialog in await client.dialogs():
        print(dialog.id, dialog.title)
    await client.send_text(dialog.id, "hello")
    async for event in client.listen():   # incoming DMs
        print(event.message.text)

asyncio.run(main())
```

The public surface (`tg_messenger.__all__`) also exports `SessionStore`, `LoginFlow`,
`LOGIN_HINT`, `EventBus`, `run_with_flood_wait_retry` and `HandledFloodWaitError`.
