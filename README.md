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

Each saved login is a *profile* (a session file under `~/.tg_messenger/sessions/`).
Log in to as many as you like and pick one per run with the global `--profile` flag:

```bash
tg-messenger login --profile work --phone +1...   # create/replace the "work" profile
tg-messenger login --profile personal --phone +1...
tg-messenger profiles                             # list saved profiles
tg-messenger --profile work dialogs               # any command targets a profile
tg-messenger --profile personal serve             # CLI / TUI / web all accept --profile
```

With more than one profile and **no** `--profile`, the CLI and TUI pop a selection
menu; a non-interactive shell errors instead of guessing. One process serves one
profile, and each non-default profile gets its own log file
(`~/.tg_messenger/logs/tg_messenger_<profile>.log`). The web exposes a read-only
`GET /profiles` listing saved profiles with the active one flagged.

## Reply suggester

`tg-messenger suggest` drafts a reply in the style of your past messages with a contact —
a **draft for you to review and edit**, never an auto-reply (full automation is a separate
feature). Needs the `[agent]` extra and `TG_AGENT_MODEL` (same as the agent).

```bash
tg-messenger suggest 7            # print a draft reply for dialog 7
tg-messenger suggest 7 --send     # send the draft as-is
tg-messenger suggest --learn 7    # (re)build the contact's style profile from history
```

The draft also appears in the web UI (the 💡 Suggest button by the composer) and the TUI
(an incoming message in the open DM shows a 💡 hint — press Tab to accept it).

**Privacy:** generating a draft sends the recent conversation history — and the style profile
built from your own replies — to your configured LLM provider (and, with LangSmith tracing on,
into the traces). Learning is always an explicit per-contact command; nothing is scanned in the
background.

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
