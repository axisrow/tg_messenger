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

## Session encryption & SSO with tg_content_factory

By default sessions live as plaintext `0600` files under `~/.tg_messenger/sessions/`.
Set `SESSION_ENCRYPTION_KEY` (and `pip install 'tg-messenger[crypto]'`) to store them
encrypted instead — Fernet over a PBKDF2-derived key, format `enc:v2:`, **byte-compatible
with [tg_content_factory](https://github.com/axisrow/tg_content_factory)**. A plaintext file
read under a key is lazily rewritten encrypted; an encrypted file read without the key errors
with a hint.

Two ways to share one login across both projects (single sign-on):

- **Shared key (option A):** put the same `SESSION_ENCRYPTION_KEY` in both `.env` files —
  the encrypted session strings become mutually readable.
- **Export / import (option B):**
  ```bash
  tg-messenger login --export-session         # prints the plaintext StringSession (full access!)
  tg-messenger login --import-session         # reads a StringSession from stdin (no echo) and saves it
  ```
  or inject directly as a library: `StandaloneTelegramClient(..., external_session=STRING)`.

Session strings are never written to logs.
