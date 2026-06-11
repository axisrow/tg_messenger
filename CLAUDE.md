# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install
pip install -e ".[dev]"          # core + CLI + web + tui + test/lint toolchain ([dev] pulls [web,tui])
pip install -e ".[dev,agent]"   # + LLM stack for the agent layer (langchain/langgraph/deepagents)
# As a library: base = core+CLI only; web/tui are extras ([web]/[tui]/[all]); CLI imports them
#   lazily and `serve`/`tui` fail with a "pip install 'tg-messenger[web]'" hint when absent.

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

## Project-wide invariants

- **No silent failures — project-level rule.** Every caught-and-suppressed exception MUST be logged (`logger.exception`, or `logger.warning` for expected drops). A bare `except: pass` is a review bug. This applies to every module, not just the examples listed under logsetup below.
- Every network call goes through `run_with_flood_wait_retry`; never `get_entity('@username')` in loops (~50 resolves in a row → flood).
- UIs render Pydantic models from core — never raw Telethon objects.
- Tests: fakes from conftest only — no network, no real LLM, no real `sleep` (inject `clock`/`rng`); `filterwarnings=error`.
- **Every PR must contain a closing keyword (`Closes #N`)** for its issue; one issue = one closing PR.
- Secrets, session strings, phone numbers and login codes never reach logs or the repository.

## Target structure (roadmap)

Issues #8–#26 (decomposition of umbrella #6, see its table-of-contents comments) add the modules below. **Placement rules — agents must not improvise locations:**
- Services that sit *above* the client (listen + act loops) live in `core/` next to `watch.py` — NOT inside `client.py`, which stays a thin wrapper.
- LLM calls exist ONLY behind `agent/factory.py` injection; new agent features (e.g. suggest) receive callables, never import langchain/deepagents themselves.
- HTTP to tg_content_factory lives ONLY in `interop/` (httpx, optional extra); `core/` never imports httpx.

```
src/tg_messenger/
├── core/                  # all Telegram logic; no UI/LLM/httpx imports
│   ├── client.py          # thin client (issue hooks: #8 cache, #15 actions, #25 acquire)
│   ├── auth.py            # SessionStore, LoginFlow; + LoginSession (#26)
│   ├── session_cipher.py  # NEW #10 — Fernet enc:v2:, byte-compatible with tg_content_factory
│   ├── cache.py           # NEW #8 — TTLCache + single-flight
│   ├── ratelimit.py       # NEW #25 — outgoing token-bucket
│   ├── storage.py         # NEW #13 — SQLite (migrations, kv)
│   ├── search.py          # NEW #12 — dialog filtering (pure functions)
│   ├── usernames.py       # NEW #22 — username generate/check/set
│   ├── moderation.py      # NEW #16 — ModerationEngine (service, watch.py pattern)
│   ├── heartbeat.py       # NEW #19 — HeartbeatService (service)
│   └── watch.py / events.py / models.py / flood.py / logsetup.py   # existing (#14 extends events/models)
├── agent/
│   ├── suggest.py         # NEW #17 — Suggester (LLM injected via factory.py)
│   └── orchestrator.py / runner.py / factory.py / tools.py / config.py / search.py / media.py
├── interop/               # NEW #20 — the ONLY place talking HTTP to tg_content_factory
│   ├── factory_client.py
│   └── worker.py
├── cli/main.py            # new commands: profiles(#11) search(#12) moderate(#16) heartbeat(#19)
│                          #   worker(#20) username(#22) suggest(#17)
├── web/                   # + auth middleware(#24), /tg-login wizard(#26)
└── tui/app.py             # + profile screen(#11), login screen(#26), @file send(#21)
.github/workflows/ci.yml   # NEW #23
```

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
- **`client.py` — `StandaloneTelegramClient`**: the only thing the UIs talk to. Thin async wrapper over one Telethon client exposing `dialogs()`, `history()`, `send_text()`, `send_media()`, `download_media()`/`download_message_media()`, `get_me()`, `entity_title()`, `send_reaction()` (`SendReactionRequest` via retry), and **seven** event streams (all the same lazy-bus + drop-oldest fan-out pattern; an unsubscribed bus is a no-op publish): `listen()` (incoming from private chats only — DMs + bots), `listen_all()` (incoming from EVERY chat, groups/channels included — the UIs' live feed), `listen_outgoing()` (own messages from any device, groups INCLUDED — no DM filter), `listen_deleted()` (`MessagesDeletedEvent`; Telegram names the chat only for channels/supergroups), and the #14 trio `listen_chat_actions()` (`ChatActionEvent` — joins/leaves/kicks/title/pin/photo via `events.ChatAction`; kind from the event's `user_joined`/`user_added`→`join`, `user_kicked`→`kick`, `user_left`→`leave`, `new_title`/`new_pin`/`new_photo`, else `other`; `user`/`actor` best-effort), `listen_reads()` (`MessageReadEvent`; `outbox=True` = the OTHER party read OUR messages up to `max_id`) and `listen_reactions()` (`ReactionEvent` via `events.Raw(UpdateMessageReactions)` — changed standard emoji `emoticon` comes from `recent_reactions`, aggregate `results` are not used; custom/premium reactions and unreadable shapes → `None` with a warning; `actor_id` best-effort `None`). `IncomingEvent.album_id` carries the message's `grouped_id` (v1 only marks albums — no aggregator). Telethon handlers registered eagerly at `connect()` — `_on_new_message` publishes into both incoming buses (`_bus_all` always, `_bus` when `is_private`); each #14 handler maps the raw Telethon event to its Pydantic model in a try/except that logs and keeps the stream alive (reactions `logger.warning`, the rest `logger.exception`). `dialogs(dm_only=True)` (default) returns DMs only (filtered `kind == "dm"`); `dm_only=False` returns everything, each `Dialog` with `kind` (`dm|group|channel|bot`); `group_dialogs()` returns every non-DM dialog (the UIs' "Группы" tab — the `kind != "dm"` filter lives here once, not in each front-end). `Dialog.id` is the **marked** peer id (negative for groups/channels) — the same value events carry in `chat_id`, accepted by `history`/`send_text`. **Every network call routes through `run_with_flood_wait_retry`.** **Read anti-flood (cache.py, single-flight):** every user-triggered read goes through a TTL cache — `dialogs()` is ONE full mapped list (all kinds, `maxsize=1`, TTL 30s) and `dm_only` filters from it (tab switching after the first load = 0 network calls); `history()` is keyed `(int(peer), limit, offset_id)` (TTL 15s, `maxsize=64`). Both return a copy (UIs render, never mutate). The dialogs cache is NOT invalidated by events (an active group would defeat it — that was the incident). Every write and every live event invalidates `history` for its peer: `send_text`/`send_media` after a successful send; `_on_new_message`/`_on_outgoing_message` right after computing `dialog_id`, BEFORE mapping (a broken event still drops stale history); `_on_deleted` by `chat_id` if present, else the whole history cache. `download_message_media` is NOT cached (a point fetch by id). `flood_sleep_threshold=0` in `_default_factory` — Telethon never sleeps silently on a FloodWait, every wait surfaces through `run_with_flood_wait_retry`. **Username resolution is expensive (~50 in a row → flood): never `get_entity('@username')` in loops — the agent allowlist resolves through the dialog list once; `watch` memoises titles itself.**
  **Message actions (#15):** `send_text(peer, text, reply_to=None)` quotes a message; `forward(from_peer, ids, to_peer)`, `edit_text(peer, id, text)`, `delete_messages(peer, ids, revoke=True)` and `mark_read(peer, max_id=None)` are thin retry-wrapped Telethon calls. Every message mutation (send/forward/edit/delete) invalidates the `history` cache of its own peer(s) — `forward` invalidates BOTH the source and destination, and logs/filters partial `None` results so already-forwarded messages are not retried as failures. `mark_read` is NOT a mutation of history, but after a successful ack it invalidates the dialogs cache so unread badges do not stay stale; auto-read in web/TUI passes the max message id from the loaded history snapshot so newer unseen messages stay unread. It is best-effort (failures are logged, never raised — web: tracked background task, never awaited in the messages route; TUI: a `_mark_read` worker after history loads, never awaited in a handler). `Dialog.unread` (from telethon `dialog.unread_count`) and `Message.reply_to_id` (best-effort `raw.reply_to.reply_to_msg_id`) are the supporting model fields; UIs render the unread count as a badge (web `<span class="unread">`, TUI `(N)`).
  Delete safety: before Telethon deletion, `delete_messages` fetches the requested ids from the target peer and rejects missing/mismatched messages; `revoke=False`/CLI `--for-me` is rejected for channels/supergroups because Telegram deletes there for everyone.
- **`watch.py` — `DeletionWatcher`** (`tg-messenger watch`): backs up own deleted messages (e.g. removed by group moderator bots) to Saved Messages. A bounded cache of recent own messages (`OrderedDict`, 1000) is the only way to recognise OUR messages among deletions — `deleted_ids` carry no author; matching pops the entry (no duplicate notifications). Events without `chat_id` (DMs/small groups) must never match channel cache entries (`CHANNEL_ID_THRESHOLD` on marked ids) — per-channel message ids collide with the global id space. Messages in the self-dialog are not cached (no notification loop). `run()` uses `asyncio.gather`, NOT `TaskGroup` — TaskGroup wraps KeyboardInterrupt into BaseExceptionGroup and breaks the CLI Ctrl+C pattern. Best-effort by design: Telegram doesn't always send the deletion update; cache is in-memory.
- **`flood.py`**: dependency-free FloodWait retry. Transient waits (≤60s, within a 120s budget) are slept-and-retried; anything else raises `HandledFloodWaitError`. Other exceptions propagate. Vendored from the parent `tg_content_factory` project — keep it pool/DB-free.
- **`cache.py` — `TTLCache`**: dependency-free TTL + maxsize-eviction (oldest first, `OrderedDict` — pattern from `watch.py`) + async single-flight `get_or_fetch` (per-key `asyncio.Lock`, double-check under the lock, a failing fetch is neither cached nor left holding a lock). `clock` is injected (`time.monotonic` default) — tests advance a fake clock, never sleep. Used by `client.py` for the dialogs/history read caches.
- **`search.py` — `filter_dialogs(dialogs, query)`**: pure, network-free dialog filtering over an already-fetched `list[Dialog]` (the #8 read cache, so the UIs filter without a request — title substring case-insensitive, username with/without `@` exact-or-prefix, id exact plus the positive form of a marked id; empty query → all). Message search lives on the client: **`search_messages(peer, query, limit=20)`** = `iter_messages(search=query)` through `run_with_flood_wait_retry`, NOT cached (a point lookup, not a re-read page). Global content search across all chats is deliberately absent — that's tg_content_factory's job (umbrella #6).
- **`events.py` — `EventBus`**: asyncio fan-out. One Telethon `NewMessage` handler `publish()`es; each UI `subscribe()`s its own bounded queue. **Publishing never blocks** — a full subscriber queue drops its oldest item so a slow consumer can't stall the Telethon loop.
- **`auth.py`**: `SessionStore` persists Telethon `StringSession` strings as 0600 files under `~/.tg_messenger/sessions/` (no SQLite). An external session string can be injected and is never written to disk. **Optional at-rest encryption** (`session_cipher.py`): with `encryption_key` (CLI/UIs pass env `SESSION_ENCRYPTION_KEY`) sessions are stored as `enc:v2:` Fernet tokens — **byte-compatible with tg_content_factory, so a shared key = SSO**; reading a plaintext file under a key lazily rewrites it encrypted; reading an encrypted file with no key raises with a `SESSION_ENCRYPTION_KEY` hint. `enc:v1:` is read-only legacy. **Session strings never reach logs.** `LoginFlow` is the two-step phone→code→2FA sign-in; `phone_code_hash` stays bound to the same client/session. CLI `login --export-session`/`--import-session` move a session between machines/projects.
- **`session_cipher.py`** — `encrypt_session`/`decrypt_session`/`is_encrypted`: dependency-light Fernet wrapper. `enc:v2:` = Fernet over PBKDF2-HMAC-SHA256(secret, salt=`b"tg_session_key_v2"`, 200k iters, 32 bytes) — the factory's scheme reproduced byte-for-byte (a compat test re-derives the key with the constants hardcoded independently). `cryptography` is the `[crypto]` extra, imported lazily — a missing extra under a configured key raises a `pip install 'tg-messenger[crypto]'` `ValueError`, not an ImportError traceback; plaintext passes through so encryption stays opt-in.
- **`storage.py`** — `Storage`: stdlib `sqlite3` behind `asyncio.to_thread`, serialised by one `asyncio.Lock` over a single `check_same_thread=False` connection (concurrent `gather` callers never race or deadlock; `close()` also takes the lock). WAL + `foreign_keys=ON`. Consumers (#16/#17/#19) `register_migrations([...])`; unapplied migrations are tracked by stable statement ids in `_tg_messenger_migrations` and applied inside one transaction (`PRAGMA user_version` mirrors the applied count for inspection; a failing batch rolls back and metadata doesn't advance). The `kv` table (JSON values) is always present and un-versioned; `get_value`/`set_value` cover small odds and ends. `default_db_path(profile)` = `~/.tg_messenger/<safe-profile>.db` (one DB per profile, #11). **The TTL read cache does NOT live here** — it stays in-memory (#8); `client.py` does not depend on storage (the client stays light; storage is a sibling for services, watch.py pattern).
- **`models.py`**: Pydantic v2 domain models (`Dialog`, `Message`, `User`, `MediaRef`, `IncomingEvent`) shared across all interfaces. UIs render these, never raw Telethon objects — mapping happens in `StandaloneTelegramClient._to_message`.
- **`logsetup.py`** — `setup_logging(verbose=, console=)`: rotating file log (always, INFO+; DEBUG with `-v`) + stderr handler (ERROR+ only, one-line, skips `tg_messenger.cli` records — the CLI speaks via click; tracebacks go to the file only). Every CLI invocation calls it; the `tui` command re-runs it with `console=False` (stderr corrupts the alternate screen). Tests are isolated via the autouse `TG_LOG_DIR` fixture in conftest. **No silent failures**: anything caught-and-suppressed must be logged (`logger.exception`/`warning`) — see `_on_new_message`, `EventBus.publish` drops, the chat listener task, SSE streams and the web `Exception` handler.

### Interfaces
All three offer a DM/groups split over the same core API (`?tab=` / Tabs / `--groups`); the groups view is every non-DM dialog (groups, supergroups, broadcast channels, bots), served by `client.group_dialogs()` — the front-ends never re-filter by kind themselves. Each front-end also exposes dialog id (`{id} — title`) and search via `core.search`: web has a `?q=` filter on `/dialogs` plus a `/dialogs/{id}/search?q=` message search and an `<input name="q">` over the list; CLI has `dialogs --find QUERY` (local filter, composes with `--groups`) and a `search PEER QUERY [--limit]` command; the TUI filters the loaded dialog list locally via an `Input#search` (in-dialog message search in the TUI is a v1 deferral). Dialog filtering is always `filter_dialogs` over the cached list — never a network call.
- **Multilogin (`--profile`, #11)**: `--profile NAME` is a sweeping global option (CLI/TUI/serve) mapped onto `session_name`; a profile is just a saved session file (`SessionStore.list_profiles()`). With no flag and >1 saved profile, the CLI and TUI pop an interactive selection menu, while a non-interactive stdin is a clear error (`pass --profile NAME`); 0 or 1 profile resolves silently. One process = one profile (the active one is fixed at startup). The log file is isolated per non-default profile (`tg_messenger_<profile>.log`, set in `logsetup.setup_logging(profile=)`) so two accounts don't interleave. `tg-messenger profiles` lists them; the web exposes a read-only `GET /profiles` (saved names + the active one flagged), `SessionStore` dir overridable via `TG_SESSION_DIR`.
- **`cli/main.py`**: click group. `make_client(**kwargs)` builds the client from env. `dialogs --groups` lists non-DM dialogs with a `[kind]` marker. **Packaging:** web/tui are optional extras — `serve`/`tui` import `tg_messenger.web`/`tg_messenger.tui` lazily inside the command and, on `ImportError`, raise a `ClickException` with a `pip install 'tg-messenger[web]'`/`[tui]` hint (base install must not pull fastapi/textual — locked by `tests/test_packaging.py`, a fresh-subprocess `sys.modules` check). The public API (`tg_messenger.__all__`, `tg_messenger.core.__all__`) is snapshot-pinned in the same test; `src/tg_messenger/py.typed` ships the typing marker.
- **`web/app.py`**: FastAPI + server-rendered HTMX fragments + SSE for live messages (`sse_event_stream`, fed by `listen_all()` — group streams work too). `build_app(client=..., session_name=...)`. The client connects/disconnects in the FastAPI lifespan. `GET /dialogs?tab=dm|groups` (unknown tab falls back to dm); the tab buttons live in `chat.html`. `GET /profiles` is a read-only list of saved profiles (the served `session_name` flagged active).
- **`tui/app.py`**: Textual app, `MessengerTUI(session_name=...)`. Two non-obvious constraints: `on_mount` resets the loop's task factory **before** the client exists — Textual's `eager_task_factory` (py3.12+) breaks Telethon, see the comment in `on_mount`; don't remove or move that line. And all network work goes through `self.run_worker(...)`, never `await` in handlers — awaiting stalls the message pump. DM/Группы are a `SidebarTabs` (a `Tabs` subclass) above the single `#dialogs` `DialogListView`; `on_tabs_tab_activated` is gated by `self._started` (Tabs fires at mount, before the client exists; the flag is NOT named `_ready` — Textual's `App` already has a `_ready` coroutine) and reloads via a worker. `SidebarTabs` adds a `down`/`enter` binding that hands focus to `#dialogs` (and selects the first item) — Textual's `Tabs` only binds left/right, so without it you'd have to Tab past the strip to reach the list. The list is a `DialogListView` (a `ListView` subclass): `up` at the first item (or empty selection) jumps focus back to `#tabs` (the symmetric counterpart); anywhere else `up` scrolls the list as usual (defers to `cursor_up`).

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
- `tests/conftest.py` provides `FakeTelethonClient` (records `sent`/`downloads`, counts network reads via `iter_dialogs_calls`/`iter_messages_calls` for the TTL-cache tests, can `push_event` into registered handlers — dispatched by builder type like real Telethon: `MessageDeleted` by the `deleted_ids` attribute, `NewMessage(incoming=)/(outgoing=)` by `event.message.out`) and a `session_dir` tmp fixture. Cache tests inject a fake clock (`t = {"now": 0.0}`, `clock=lambda: t["now"]`) — time never really passes — and exercise concurrency with `asyncio.gather` + `asyncio.Event`, never a real `sleep`.
- `tests/__init__.py` is deliberately empty — don't delete it. Without it `tests/` is a namespace package, and a stray top-level `tests` package in site-packages (ultralytics and yfinance_cache ship one) wins the import resolution, breaking `from tests.conftest import ...` at collection time.
