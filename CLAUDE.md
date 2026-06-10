# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install
pip install -e ".[dev]"
pip install -e ".[dev,agent]"   # + LLM stack for the agent layer (langchain/langgraph/deepagents)

# Run the full test suite
pytest

# Single file / single test
pytest tests/test_client.py
pytest tests/test_client.py::test_name -x

# Lint (ruff: E, F, I, N, W; line-length 120)
ruff check . && ruff check --fix .

# Run the app
tg-messenger --help         # CLI entrypoint (login, dialogs, read, send, listen, chat, serve, tui, agent)
tg-messenger -v <cmd>        # DEBUG logging; file log: ~/.tg_messenger/logs/tg_messenger.log (env TG_LOG_DIR)
tg-messenger serve           # web (FastAPI/uvicorn), default port 8090 (env TG_WEB_PORT; --port overrides)
tg-messenger tui             # Textual TUI
tg-messenger agent           # AI auto-reply: needs the [agent] extra + TG_AGENT_* env (see .env.example)
```

Runtime requires `TG_API_ID` and `TG_API_HASH` (Telegram API credentials) — from the environment or from a `.env` in the cwd, which the CLI entrypoint auto-loads (`_load_dotenv` in `cli/main.py`; real env wins, see `.env.example`). Unit tests never need them — they patch the network seams (see below).

pytest is configured for `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed), a 30s per-test timeout, and `filterwarnings = ["error"]` — **any warning fails the test**.

## Architecture

A single UI-agnostic core wrapped by three interchangeable front-ends, plus an optional agent layer. `PLAN.md` holds the full design and the TDD build sequence (cycles 0–8 core/UI, 9–14 agent); the project is built test-first.

```
core/   ← all Telegram logic, no UI imports
cli/    ┐
tui/    ├ thin adapters over core, each independently runnable
web/    ┘
agent/  ← LangGraph intent orchestrator over core (optional [agent] extra); core never imports it
```

### Core (`src/tg_messenger/core/`)
- **`client.py` — `StandaloneTelegramClient`**: the only thing the UIs talk to. Thin async wrapper over one Telethon client exposing `dialogs()`, `history()`, `send_text()`, `send_media()`, `download_media()`/`download_message_media()`, and `listen()`. DM-only filtering (`_is_dm_entity`) excludes bots/channels/groups. **Every network call routes through `run_with_flood_wait_retry`.**
- **`flood.py`**: dependency-free FloodWait retry. Transient waits (≤60s, within a 120s budget) are slept-and-retried; anything else raises `HandledFloodWaitError`. Other exceptions propagate. Vendored from the parent `tg_content_factory` project — keep it pool/DB-free.
- **`events.py` — `EventBus`**: asyncio fan-out. One Telethon `NewMessage` handler `publish()`es; each UI `subscribe()`s its own bounded queue. **Publishing never blocks** — a full subscriber queue drops its oldest item so a slow consumer can't stall the Telethon loop.
- **`auth.py`**: `SessionStore` persists Telethon `StringSession` strings as 0600 plain-text files under `~/.tg_messenger/sessions/` (no SQLite). An external session string can be injected and is never written to disk. `LoginFlow` is the two-step phone→code→2FA sign-in; `phone_code_hash` stays bound to the same client/session.
- **`models.py`**: Pydantic v2 domain models (`Dialog`, `Message`, `User`, `MediaRef`, `IncomingEvent`) shared across all interfaces. UIs render these, never raw Telethon objects — mapping happens in `StandaloneTelegramClient._to_message`.
- **`logsetup.py`** — `setup_logging(verbose=, console=)`: rotating file log (always, INFO+; DEBUG with `-v`) + stderr handler (ERROR+ only, one-line, skips `tg_messenger.cli` records — the CLI speaks via click; tracebacks go to the file only). Every CLI invocation calls it; the `tui` command re-runs it with `console=False` (stderr corrupts the alternate screen). Tests are isolated via the autouse `TG_LOG_DIR` fixture in conftest. **No silent failures**: anything caught-and-suppressed must be logged (`logger.exception`/`warning`) — see `_on_new_message`, `EventBus.publish` drops, the chat listener task, SSE streams and the web `Exception` handler.

### Interfaces
- **`cli/main.py`**: click group. `make_client(**kwargs)` builds the client from env.
- **`web/app.py`**: FastAPI + server-rendered HTMX fragments + SSE for live messages (`sse_event_stream`). `build_app(client=..., session_name=...)`. The client connects/disconnects in the FastAPI lifespan.
- **`tui/app.py`**: Textual app, `MessengerTUI(session_name=...)`. Two non-obvious constraints: `on_mount` resets the loop's task factory **before** the client exists — Textual's `eager_task_factory` (py3.12+) breaks Telethon, see the comment in `on_mount`; don't remove or move that line. And all network work goes through `self.run_worker(...)`, never `await` in handlers — awaiting stalls the message pump.

### Agent (`src/tg_messenger/agent/`)
`tg-messenger agent` auto-replies to incoming DMs: a LangGraph router classifies each message as **chat** (single `init_chat_model` call) or **task** (a `deepagents.create_deep_agent` with Telegram tools + web search), then replies via `client.send_text`. Configured entirely from env: `TG_AGENT_MODEL` (`provider:model`), `TG_AGENT_ALLOWLIST` (`*` or ids/@usernames — empty is a startup error by design, `*` cannot be mixed with other entries), `TG_AGENT_SEARCH` (duckduckgo/tavily/exa/brave, lazy-imported in `search.py`). **Accepted v1 risk**: the allowlist controls who can *trigger* the agent, not what it can do — an allowlisted user can have the agent read/send in ANY dialog (full-trust model, documented in `.env.example`).

- **`orchestrator.py` — `Orchestrator`**: the real LangGraph graph (classify → chat | task) with per-dialog memory (`InMemorySaver`, `thread_id` = dialog id). It never calls a model itself — `classify_fn`/`chat_fn`/`task_agent` are injected; only the deep agent's **final** answer is appended to dialog state (tool chatter never leaks).
- **`factory.py`** is the ONLY module importing the LLM stack (`init_chat_model`, `create_deep_agent`) — keep it that way; deepagents API drift stays contained here. `runner.py`/`tools.py`/`config.py`/`search.py` are stdlib+core only.
- **`runner.py` — `AgentRunner`**: listen → skip (out=True / no text / not in allowlist) → handle → reply. A failing message is logged (`logger.exception`) and the loop continues. No reply loops: core subscribes with `NewMessage(incoming=True)`, plus a defensive `out` check. While handling, the dialog shows a 'typing…' indicator via `client.typing()` — best-effort **by core contract** (`_SafeChatAction` logs its own failures and never raises), so callers use a bare `async with` without defensive wrappers.
- Tools in `tools.py` are plain async functions over client methods (flood-wait retry included for free); their docstrings/annotations ARE the tool schema deepagents shows the model.

### Test seams (how the network is faked)
Tests inject a fake Telethon client — never hit the network. Mirror these patterns when adding tests:
- **Core**: pass `client_factory=lambda session, api_id, api_hash: fake_client` to `StandaloneTelegramClient`.
- **Web**: `build_app(client=stub)`.
- **CLI**: `monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)`.
- **TUI**: `MessengerTUI(client=stub)`.
- **Agent**: `Orchestrator(classify_fn=fake, chat_fn=fake, task_agent=stub_with_ainvoke)`; factory tests monkeypatch `factory.init_chat_model`/`factory.create_deep_agent`; CLI patches `cli_main.make_agent_runner`. Agent test modules guard with `pytest.importorskip("langgraph"/"deepagents")` — the suite stays green on a plain `.[dev]` install (they skip). Tests never call a real LLM.
- `tests/conftest.py` provides `FakeTelethonClient` (records `sent`/`downloads`, can `push_event` into registered handlers) and a `session_dir` tmp fixture.
- `tests/__init__.py` is deliberately empty — don't delete it. Without it `tests/` is a namespace package, and a stray top-level `tests` package in site-packages (ultralytics and yfinance_cache ship one) wins the import resolution, breaking `from tests.conftest import ...` at collection time.
