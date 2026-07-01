# tg_messenger

[![CI](https://github.com/axisrow/tg_messenger/actions/workflows/ci.yml/badge.svg)](https://github.com/axisrow/tg_messenger/actions/workflows/ci.yml)

Standalone, reusable Telegram messenger client for manual chatting in DMs.
Three interfaces (CLI / TUI / Web) over a shared UI-agnostic core, built on Telethon.

- Runs standalone in its own venv (`tg-messenger ...`).
- Reusable as an external dependency: `from tg_messenger.core import StandaloneTelegramClient`.
- Own independent StringSession storage (`~/.tg/`), with optional injection of an
  externally supplied session string.

See `PLAN.md` for the full design and the TDD build sequence.

## Quickstart

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/tg-messenger --help
```

The base install is **core + CLI** — only `telethon + pydantic + click`, so the client
stays a lightweight import for other projects (the goal behind issue #6). Everything
heavier is an opt-in extra, pulled in **only when you need that feature**. Forget one and
the command fails with a copy-paste install hint, never a raw `ModuleNotFoundError`.

```bash
pip install tg-messenger          # core + CLI — import StandaloneTelegramClient
pip install 'tg-messenger[web]'   # + FastAPI web UI  (tg-messenger serve)
pip install 'tg-messenger[tui]'   # + Textual TUI     (tg-messenger tui)
pip install 'tg-messenger[all]'   # everything below at once
```

| Extra | Pulls in | Needed for |
|---|---|---|
| `[web]` | fastapi, uvicorn, jinja2 | `tg-messenger serve` |
| `[tui]` | textual | `tg-messenger tui` |
| `[crypto]` | cryptography | at-rest session encryption / SSO with the factory |
| `[agent]` | langchain, langgraph, deepagents | `tg-messenger agent` (LLM auto-reply) |
| `[interop]` | httpx | `tg-messenger worker` — task exchange with tg_content_factory |

`[interop]` is just `httpx` on purpose: `core/` never talks HTTP, so the heavy `[agent]`
LLM stack and the worker's HTTP client are separate installs. `[dev]` pulls
`[web,tui,crypto]` so the full test/lint toolchain and every interface run locally.

## Running it

**1. Set your Telegram API credentials.** Get an `api_id` / `api_hash` from
<https://my.telegram.org> and put them in a `.env` in the current directory (auto-loaded;
real environment variables win) — see `.env.example`:

```bash
TG_API_ID=12345678
TG_API_HASH=abcdef1234567890abcdef1234567890
```

Without these the CLI exits with an error — they are required for every command that
touches Telegram.

**2. Log in** (phone → code → optional 2FA password). The code arrives in your Telegram
app, not by SMS:

```bash
tg-messenger login --phone +1234567890
```

The session is saved under `~/.tg/sessions/`, so you only log in once. (You can
also log in interactively from the Web or TUI — see below.) Everything the app
persists — sessions, logs and per-profile SQLite — lives under a single root
`~/.tg/`. Override it with `TG_HOME`; if `~/.tg/` doesn't exist yet but the
legacy `~/.tg_messenger/` does, that older directory is read in place (no data is
moved), so an existing login keeps working.

**3. Start an interface** — same core, pick whichever you like:

```bash
tg-messenger chat            # interactive terminal REPL — see incoming, send replies
tg-messenger tui             # full-screen Textual UI         (needs [tui])
tg-messenger serve           # web UI on http://127.0.0.1:8090 (needs [web])
```

Or run one-off commands without a UI:

```bash
tg-messenger dialogs              # list your DMs (--groups for groups/channels)
tg-messenger read 7               # print history of dialog 7
tg-messenger send 7 "hello"       # send a message
tg-messenger --help               # every command
```

Add `-v` for DEBUG logging, `--profile NAME` to target a specific account (see
[Multiple accounts](#multiple-accounts-profiles)).

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
        external_session="...",   # or session_name=... for on-disk StringSession
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

## Search

Every dialog shows its id (`id — title`), and every front-end can search.

**Find a dialog** (by title, `@username`, or id — filtered locally over the
already-loaded list, no extra request):

```bash
tg-messenger dialogs --find ann        # DMs whose title/username/id matches "ann"
tg-messenger dialogs --groups --find dev
```

The web UI has a search box above the dialog list; the TUI filters the list as you
type into its search field.

**Search messages inside a dialog** (Telegram's own server-side search):

```bash
tg-messenger search 7 "invoice"        # last messages in dialog 7 matching "invoice"
tg-messenger search -1001234567 "lunch" --limit 50
```

The web UI exposes the same via `GET /dialogs/{id}/search?q=`. There is intentionally
no global content search across all chats — that belongs to `tg_content_factory`.

## Sending media

Send a file, photo, video, GIF or voice note through any front-end.

**CLI** — `send` takes `--file` plus optional media modifiers:

```bash
tg-messenger send 7 "look at this" --file ./photo.jpg     # caption from TEXT
tg-messenger send 7 --file ./report.pdf --caption "Q3"    # caption from --caption
tg-messenger send 7 --file ./note.ogg --voice             # send as a voice note
tg-messenger send 7 --file ./clip.mp4 --video-note        # round video note
tg-messenger send 7 --file ./photo.jpg --as-file          # plain document, no preview
```

`--voice`, `--video-note` and `--as-file` are mutually exclusive. A missing path
fails fast (no network call) with a clear error.

**TUI** — the composer understands an `@PATH` syntax: type `@` followed by a path
(quote it if it has spaces) and an optional caption.

```
@./photo.jpg                     # send the file, no caption
@"~/My Pics/cat.png" so cute     # quoted path + caption
@/tmp/report.pdf Q3 results      # path + caption
```

A non-existent path is reported in the TUI (a toast) and nothing is sent. A message
not starting with `@` is sent as plain text as before.

**Web** — the 📎 button by the composer opens a file picker; the current composer text
becomes the caption. Uploads are capped at `TG_WEB_MAX_UPLOAD_MB` (default 50) — a
larger file is rejected with HTTP 413, an empty one with 400.

## Logging in from the Web or TUI

You don't have to use the CLI `login` command — both the Web and the TUI can sign
you in interactively (phone → code → optional 2FA password):

- **Web** — when the served session isn't logged in, every page redirects to the
  `/tg-login` wizard: enter your phone, then the code Telegram sends (the page tells
  you where it went — usually the in-app "Telegram" service chat), then a 2FA
  password if your account has one. On success the session is saved and you land in
  the chat. With `TG_WEB_PASS` set, `/tg-login` sits **behind** the web password.
- **TUI** — `tg-messenger tui` against a logged-out session opens a login screen
  instead of exiting: type the phone, press Enter, type the code, press Enter (and
  the 2FA password if asked). A wrong code is reported and you can retry; Ctrl+C
  quits cleanly. On success the dialog list loads as usual.

The whole flow runs over one connected client (Telegram binds the login `code` to
that single session), and the phone number and code are never written to the logs.

## Web authorization

Set `TG_WEB_PASS` to put the whole web UI behind a password. With it, every route
sits behind an HMAC-cookie session: `GET /login` shows a password form, a correct
password (compared in constant time) sets a signed cookie valid for 7 days, and
`GET /logout` clears it. The cookie is signed with a per-process random key
(`secrets.token_bytes`), so a restart invalidates all sessions and the value cannot
be forged or extended. A wrong password is logged (WARNING with the client IP) and
delayed before a 401. Unauthenticated requests redirect browsers to `/login` and
return 401 to API/SSE callers (including `GET /stream/{id}`).

Without `TG_WEB_PASS`, a bind to `127.0.0.1` is unauthenticated as before, but
binding to a non-localhost host (e.g. `--host 0.0.0.0`) is **refused** — set the
password or pass `--insecure` (a deliberate, logged bypass).

```bash
TG_WEB_PASS=secret tg-messenger serve --host 0.0.0.0
```

> This adds authentication, not transport encryption. There is no built-in HTTPS:
> terminate TLS at a reverse proxy (nginx/caddy) in front of the server.

## Multiple accounts (profiles)

Each saved login is a *profile* (a session file under `~/.tg/sessions/`).
Log in to as many as you like and pick one per run with the global `--profile` flag:

```bash
tg-messenger --profile work login --phone +1...   # create/replace the "work" profile
tg-messenger --profile personal login --phone +1...
tg-messenger profiles                             # list saved profiles
tg-messenger --profile work dialogs               # any command targets a profile
tg-messenger --profile personal serve             # CLI / TUI / web all accept --profile
```

With more than one profile and **no** `--profile`, the CLI and TUI pop a selection
menu; a non-interactive shell errors instead of guessing. One process serves one
profile, and each non-default profile gets its own log file
(`~/.tg/logs/tg_messenger_<profile>.log`). The web exposes a read-only
`GET /profiles` listing saved profiles with the active one flagged.

## Session encryption & SSO with tg_content_factory

By default sessions live as plaintext `0600` files under `~/.tg/sessions/`.
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

## Interop with tg_content_factory (worker + agent tools)

Two cooperating projects, split by role: **tg_messenger is the hands** (it reads
and sends messages through your account) and **[tg_content_factory](https://github.com/axisrow/tg_content_factory)
is the memory + search** (it indexes conversations and holds a task queue). They
talk over HTTP — and httpx lives **only** in `tg_messenger/interop/`, never in the
core.

Install the extra:

```bash
pip install 'tg-messenger[interop]'
```

Run the worker — it claims tasks from the factory, executes them and reports back:

```bash
tg-messenger worker --factory-url http://127.0.0.1:8000 \
    --types dm_reply,chat_answer,fetch_history --interval 5
```

Task types: `dm_reply`/`chat_answer` (`{peer, text}` → send a message; with
`{peer, prompt}` the optional `[agent]` answers first), `fetch_history` and
`fetch_dialogs` (read and return serialized models). Auth to the factory is HTTP
Basic (empty username + `TG_FACTORY_PASSWORD`).

When `TG_FACTORY_URL` is set, the AI agent also gains two tools — `factory_search`
(recall from the factory's archive, beyond Telegram's recent history) and
`factory_create_task` (enqueue background work).

**Worked example — "where to go in St. Petersburg":** you DM the agent *"посоветуй,
куда сходить на экскурсию в Питере"*; it calls `factory_search` to pull what your
chats already said about СПб excursions from the factory's index, optionally
`factory_create_task` to have the factory compile a richer answer, and replies with
a grounded recommendation — memory (factory) plus hands (messenger).

**Accepted risk (v1, full trust):** tasks from the factory run on your account with
no source authorization beyond the shared password — whoever can enqueue tasks
drives your "hands". Trust the factory as you trust yourself; keep the password in
env only, never in logs or the repo.

## Outgoing rate limit (automated senders)

Several commands send on your behalf in the background — `agent`, `heartbeat run`,
`worker`, `ghostwrite`, and `moderate` (warn/notice actions). A systematically high
send rate is the main account-ban risk (worse than any single FloodWait), so a
token-bucket caps every outgoing message in the process.

The default cap is **20 messages/minute**. You can override it with `TG_SEND_RATE`;
setting `TG_SEND_RATE=0` explicitly turns the cap **off** (no ceiling). When it is
off, automated sender commands log a WARNING on start so the unbounded state is never
silent:

```bash
TG_SEND_RATE=0 tg-messenger agent
```

**Scope: the cap is per-process, not per-account.** Each running command (a separate
`agent`, `worker`, `serve`, `tui`, …) holds its own bucket, so two senders running at
once can put up to `2 × TG_SEND_RATE` on the same account. If you run several senders
in parallel, size `TG_SEND_RATE` with that multiplication in mind (or run one at a time).

When the cap is reached, a send **waits** for the next token (nothing is lost) and
logs a WARNING — it never errors. Reads (dialogs/history) are not limited.
