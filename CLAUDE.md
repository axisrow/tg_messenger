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
tg-messenger --help         # CLI entrypoint (login, dialogs, read, send, listen, watch, chat, serve, tui, agent)
tg-messenger -v <cmd>        # DEBUG logging; file log: ~/.tg_messenger/logs/tg_messenger.log (env TG_LOG_DIR)
tg-messenger serve           # web (FastAPI/uvicorn), default port 8090 (env TG_WEB_PORT; --port overrides)
tg-messenger tui             # Textual TUI
tg-messenger agent           # AI auto-reply: needs the [agent] extra + TG_AGENT_* env (see .env.example)
```

Runtime requires `TG_API_ID` and `TG_API_HASH` (Telegram API credentials) — from the environment or from a `.env` in the cwd, which the CLI entrypoint auto-loads (`_load_dotenv` in `cli/main.py`; real env wins, see `.env.example`). Unit tests never need them — they patch the network seams (see below).

pytest is configured for `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed), a 30s per-test timeout, and `filterwarnings = ["error"]` — **any warning fails the test**.

## Architecture

A single UI-agnostic core wrapped by three interchangeable front-ends, plus an optional agent layer. `PLAN.md` holds the full design and the TDD build sequence (cycles 0–8 core/UI, 9–25 agent, 26–30 deletion watch, 31–38 DM/groups tabs); the project is built test-first.

```
core/   ← all Telegram logic, no UI imports
cli/    ┐
tui/    ├ thin adapters over core, each independently runnable
web/    ┘
agent/  ← LangGraph intent orchestrator over core (optional [agent] extra); core never imports it
```

### Core (`src/tg_messenger/core/`)
- **`client.py` — `StandaloneTelegramClient`**: the only thing the UIs talk to. Thin async wrapper over one Telethon client exposing `dialogs()`, `history()`, `send_text()`, `send_media()`, `download_media()`/`download_message_media()`, `get_me()`, `entity_title()`, and four event streams: `listen()` (incoming from private chats only — DMs + bots), `listen_all()` (incoming from EVERY chat, groups/channels included — the UIs' live feed), `listen_outgoing()` (own messages from any device, groups INCLUDED — no DM filter) and `listen_deleted()` (`MessagesDeletedEvent`; Telegram names the chat only for channels/supergroups). Still three Telethon handlers, registered eagerly at `connect()` — `_on_new_message` publishes into both incoming buses (`_bus_all` always, `_bus` when `is_private`); publishing into a bus without subscribers is a no-op. `dialogs(dm_only=True)` (default) returns DMs only (`_is_dm_entity`); `dm_only=False` returns everything, each `Dialog` with `kind` (`dm|group|channel|bot`). `Dialog.id` is the **marked** peer id (negative for groups/channels) — the same value events carry in `chat_id`, accepted by `history`/`send_text`. **Every network call routes through `run_with_flood_wait_retry`.**
- **`watch.py` — `DeletionWatcher`** (`tg-messenger watch`): backs up own deleted messages (e.g. removed by group moderator bots) to Saved Messages. A bounded cache of recent own messages (`OrderedDict`, 1000) is the only way to recognise OUR messages among deletions — `deleted_ids` carry no author; matching pops the entry (no duplicate notifications). Events without `chat_id` (DMs/small groups) must never match channel cache entries (`CHANNEL_ID_THRESHOLD` on marked ids) — per-channel message ids collide with the global id space. Messages in the self-dialog are not cached (no notification loop). `run()` uses `asyncio.gather`, NOT `TaskGroup` — TaskGroup wraps KeyboardInterrupt into BaseExceptionGroup and breaks the CLI Ctrl+C pattern. Best-effort by design: Telegram doesn't always send the deletion update; cache is in-memory.
- **`flood.py`**: dependency-free FloodWait retry. Transient waits (≤60s, within a 120s budget) are slept-and-retried; anything else raises `HandledFloodWaitError`. Other exceptions propagate. Vendored from the parent `tg_content_factory` project — keep it pool/DB-free.
- **`events.py` — `EventBus`**: asyncio fan-out. One Telethon `NewMessage` handler `publish()`es; each UI `subscribe()`s its own bounded queue. **Publishing never blocks** — a full subscriber queue drops its oldest item so a slow consumer can't stall the Telethon loop.
- **`auth.py`**: `SessionStore` persists Telethon `StringSession` strings as 0600 plain-text files under `~/.tg_messenger/sessions/` (no SQLite). An external session string can be injected and is never written to disk. `LoginFlow` is the two-step phone→code→2FA sign-in; `phone_code_hash` stays bound to the same client/session.
- **`models.py`**: Pydantic v2 domain models (`Dialog`, `Message`, `User`, `MediaRef`, `IncomingEvent`) shared across all interfaces. UIs render these, never raw Telethon objects — mapping happens in `StandaloneTelegramClient._to_message`.
- **`logsetup.py`** — `setup_logging(verbose=, console=)`: rotating file log (always, INFO+; DEBUG with `-v`) + stderr handler (ERROR+ only, one-line, skips `tg_messenger.cli` records — the CLI speaks via click; tracebacks go to the file only). Every CLI invocation calls it; the `tui` command re-runs it with `console=False` (stderr corrupts the alternate screen). Tests are isolated via the autouse `TG_LOG_DIR` fixture in conftest. **No silent failures**: anything caught-and-suppressed must be logged (`logger.exception`/`warning`) — see `_on_new_message`, `EventBus.publish` drops, the chat listener task, SSE streams and the web `Exception` handler.

### Interfaces
All three offer a DM/groups split over the same core API (`?tab=` / Tabs / `--groups`); the groups view is every non-DM dialog (groups, supergroups, broadcast channels, bots).
- **`cli/main.py`**: click group. `make_client(**kwargs)` builds the client from env. `dialogs --groups` lists non-DM dialogs with a `[kind]` marker.
- **`web/app.py`**: FastAPI + server-rendered HTMX fragments + SSE for live messages (`sse_event_stream`, fed by `listen_all()` — group streams work too). `build_app(client=..., session_name=...)`. The client connects/disconnects in the FastAPI lifespan. `GET /dialogs?tab=dm|groups` (unknown tab falls back to dm); the tab buttons live in `chat.html`.
- **`tui/app.py`**: Textual app, `MessengerTUI(session_name=...)`. Two non-obvious constraints: `on_mount` resets the loop's task factory **before** the client exists — Textual's `eager_task_factory` (py3.12+) breaks Telethon, see the comment in `on_mount`; don't remove or move that line. And all network work goes through `self.run_worker(...)`, never `await` in handlers — awaiting stalls the message pump. DM/Группы are `Tabs` above the single `#dialogs` ListView; `on_tabs_tab_activated` is gated by `self._started` (Tabs fires at mount, before the client exists; the flag is NOT named `_ready` — Textual's `App` already has a `_ready` coroutine) and reloads via a worker.

### Agent (`src/tg_messenger/agent/`)
`tg-messenger agent` auto-replies to incoming DMs — **deliberately DM-only** (it consumes `listen()` and `dialogs(dm_only=True)`; group messages never trigger it even from allowlisted users): a LangGraph router classifies each message as **chat** (single `init_chat_model` call), **task** (a `deepagents.create_deep_agent` with Telegram tools + web search) or a **custom intent** from `agent.json`, then replies via `client.send_text`. Photos go to a **vision** model (`TG_AGENT_VISION_MODEL`, falls back to the main model — which then must be multimodal); voice messages are detected and skipped with an INFO log. Configured from env: `TG_AGENT_MODEL` (`provider:model`), `TG_AGENT_ALLOWLIST` (`*` or ids/@usernames — empty is a startup error by design, `*` cannot be mixed with other entries), `TG_AGENT_SEARCH` (duckduckgo/tavily/exa/brave, lazy-imported in `search.py`), plus custom intents from `TG_AGENT_CONFIG` or `./agent.json` (see `agent.json.example`; validated fail-fast in `load_intents`, names must not collide with `RESERVED_INTENT_NAMES`). LangSmith tracing is env-only — `LANGSMITH_TRACING`/`_API_KEY`/`_PROJECT`; langchain/langgraph pick them up themselves, no graph code involved. `langsmith_tracing_enabled` (`config.py`) makes the `agent` command fail fast when tracing is on without a key and echo the status when on. **Accepted v1 risk**: the allowlist controls who can *trigger* the agent, not what it can do — an allowlisted user can have the agent read/send in ANY dialog (full-trust model, documented in `.env.example`).

- **`orchestrator.py` — `Orchestrator`**: the real LangGraph graph (route → vision | classify → chat | task | custom intents; route nodes are built from `IntentSpec`s at graph build time) with per-dialog memory (`InMemorySaver`, `thread_id` = dialog id). It never calls a model itself — `classify_fn`/`chat_fn`/`task_agent`/`vision_fn` are injected; only the deep agent's **final** answer is appended to dialog state (tool chatter never leaks). Two non-obvious invariants: images never enter checkpointed state — history keeps a text placeholder, the multimodal message goes to `vision_fn` via `_pending_images` (valid only while the runner is strictly sequential); a custom intent's `system_prompt` is prefixed to the user message **in the call payload only** (a mid-list SystemMessage breaks some providers), history keeps the original text.
- **`factory.py`** is the ONLY module importing the LLM stack (`init_chat_model`, `create_deep_agent`) — keep it that way; deepagents API drift stays contained here. `runner.py`/`tools.py`/`config.py`/`search.py`/`media.py` are stdlib+core only. The classifier prompt is generated from the intent list (`build_classify_prompt`); unknown classifier output degrades to `chat` with a warning (both in factory and, defensively, in the orchestrator).
- **`runner.py` — `AgentRunner`**: listen → skip (out=True / not in allowlist / voice / no text) → dispatch (photo → `media.download_image` → `handle(..., image=)`; text → `handle`) → reply. The allowlist check runs **before** any download; `download_image` enforces `MAX_IMAGE_BYTES` (declared size pre-download AND actual bytes). A failing message is logged (`logger.exception`) and the loop continues. No reply loops: core subscribes with `NewMessage(incoming=True)`, plus a defensive `out` check. While handling, the dialog shows a 'typing…' indicator via `client.typing()` — best-effort **by core contract** (`_SafeChatAction` logs its own failures and never raises), so callers use a bare `async with` without defensive wrappers.
- Tools in `tools.py` are plain async functions over client methods (flood-wait retry included for free); their docstrings/annotations ARE the tool schema deepagents shows the model.

### Test seams (how the network is faked)
Tests inject a fake Telethon client — never hit the network. Mirror these patterns when adding tests:
- **Core**: pass `client_factory=lambda session, api_id, api_hash: fake_client` to `StandaloneTelegramClient`.
- **Web**: `build_app(client=stub)`.
- **CLI**: `monkeypatch.setattr(cli_main, "make_client", lambda **kw: stub)`.
- **TUI**: `MessengerTUI(client=stub)`.
- **Agent**: `Orchestrator(classify_fn=fake, chat_fn=fake, task_agent=stub_with_ainvoke)`; factory tests monkeypatch `factory.init_chat_model`/`factory.create_deep_agent`; CLI patches `cli_main.make_agent_runner`. Agent test modules guard with `pytest.importorskip("langgraph"/"deepagents")` — the suite stays green on a plain `.[dev]` install (they skip). Tests never call a real LLM.
- `tests/conftest.py` provides `FakeTelethonClient` (records `sent`/`downloads`, can `push_event` into registered handlers — dispatched by builder type like real Telethon: `MessageDeleted` by the `deleted_ids` attribute, `NewMessage(incoming=)/(outgoing=)` by `event.message.out`) and a `session_dir` tmp fixture.
- `tests/__init__.py` is deliberately empty — don't delete it. Without it `tests/` is a namespace package, and a stray top-level `tests` package in site-packages (ultralytics and yfinance_cache ship one) wins the import resolution, breaking `from tests.conftest import ...` at collection time.
