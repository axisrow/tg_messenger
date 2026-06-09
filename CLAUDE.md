# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, all extras) into the project venv
python -m venv .venv && ./.venv/bin/pip install -e ".[dev,cli,tui,web]"

# Run the full test suite
./.venv/bin/pytest

# Single file / single test
./.venv/bin/pytest tests/test_client.py
./.venv/bin/pytest tests/test_client.py::test_name -x

# Lint (ruff: E, F, I, N, W; line-length 120)
./.venv/bin/ruff check . && ./.venv/bin/ruff check --fix .

# Run the app
./.venv/bin/tg-messenger --help         # CLI entrypoint (login, dialogs, read, send, listen, chat, serve, tui)
./.venv/bin/tg-messenger serve           # web (FastAPI/uvicorn), default port 8090 (env TG_WEB_PORT; --port overrides)
./.venv/bin/tg-messenger tui             # Textual TUI
```

Runtime requires `TG_API_ID` and `TG_API_HASH` in the environment (Telegram API credentials). Tests never need them — they patch the network seams (see below).

pytest is configured for `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed), a 30s per-test timeout, and `filterwarnings = ["error"]` — **any warning fails the test**.

## Architecture

A single UI-agnostic core wrapped by three interchangeable front-ends. `PLAN.md` holds the full design and the TDD build sequence (cycles 1–8); the project is built test-first.

```
core/   ← all Telegram logic, no UI imports
cli/    ┐
tui/    ├ thin adapters over core, each independently runnable
web/    ┘
```

### Core (`src/tg_messenger/core/`)
- **`client.py` — `StandaloneTelegramClient`**: the only thing the UIs talk to. Thin async wrapper over one Telethon client exposing `dialogs()`, `history()`, `send_text()`, `send_media()`, `download_media()`, and `listen()`. DM-only filtering (`_is_dm_entity`) excludes bots/channels/groups. **Every network call routes through `run_with_flood_wait_retry`.**
- **`flood.py`**: dependency-free FloodWait retry. Transient waits (≤60s, within a 120s budget) are slept-and-retried; anything else raises `HandledFloodWaitError`. Other exceptions propagate. Vendored from the parent `tg_content_factory` project — keep it pool/DB-free.
- **`events.py` — `EventBus`**: asyncio fan-out. One Telethon `NewMessage` handler `publish()`es; each UI `subscribe()`s its own bounded queue. **Publishing never blocks** — a full subscriber queue drops its oldest item so a slow consumer can't stall the Telethon loop.
- **`auth.py`**: `SessionStore` persists Telethon `StringSession` strings as 0600 plain-text files under `~/.tg_messenger/sessions/` (no SQLite). An external session string can be injected and is never written to disk. `LoginFlow` is the two-step phone→code→2FA sign-in; `phone_code_hash` stays bound to the same client/session.
- **`models.py`**: Pydantic v2 domain models (`Dialog`, `Message`, `User`, `MediaRef`, `IncomingEvent`) shared across all interfaces. UIs render these, never raw Telethon objects — mapping happens in `StandaloneTelegramClient._to_message`.

### Interfaces
- **`cli/main.py`**: click group. `make_client(**kwargs)` builds the client from env.
- **`web/app.py`**: FastAPI + server-rendered HTMX fragments + SSE for live messages (`sse_event_stream`). `build_app(client=..., session_name=...)`. The client connects/disconnects in the FastAPI lifespan.
- **`tui/app.py`**: Textual app, `MessengerTUI(session_name=...)`.

### Test seams (how the network is faked)
Tests inject a fake Telethon client — never hit the network. Mirror these patterns when adding tests:
- **Core**: pass `client_factory=lambda session, api_id, api_hash: fake_client` to `StandaloneTelegramClient`.
- **Web**: `build_app(client=stub)`.
- **CLI**: `monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)`.
- `tests/conftest.py` provides `FakeTelethonClient` (records `sent`/`downloads`, can `push_event` into registered handlers) and a `session_dir` tmp fixture.
