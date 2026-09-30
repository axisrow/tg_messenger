# tg-messenger mod for Claude Code

Skeleton of a Claude Code **mod** (issue #244): a plugin whose behavior lives
entirely in a hooks module — one `register(on, options)` entry point in
`hooks/register.ts`, TypeScript loaded straight from source (no build step).

## How mods work

- A handler is `($, e, next)`: `$` is the engine API (`$.command`, `$.ui`,
  `$.process`, `$.fs`, `$.store`, `$.env`, `$.clock`, `$.session`), `e` the
  event payload, `next` the rest of the pipeline.
- This skeleton proves the pieces the Telegram bridge needs:
  - `session.start` → `$.command.register` puts `/tg` in the composer;
  - `command.run {command: 'tg'}` opens a **pane** (`$.ui.open`) below the
    prompt, drawn by a `ui.render {component: 'Pane'}` hook (JSX: `Box`,
    `Text`, `Button` from `$.ui.resolve(e)`) — the messenger UI lands there.
- Reference mods (with docs): [anthropics/claude-code `mods/`](https://github.com/anthropics/claude-code/tree/main/mods).

## Run

Function hooks are early access — the env flag is required until that changes:

```bash
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir claude-plugin -p "/tg"
```

Or in an interactive session:

```bash
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir claude-plugin
```

then type `/tg` — a pane opens below the prompt (ping → toast, close/Esc → dismissed).

## Install

This repo is a Claude Code **marketplace** (`.claude-plugin/marketplace.json` at the root):

```bash
claude plugin marketplace add axisrow/tg_messenger
claude plugin install tg-messenger@tg-messenger
```

Function hooks are early access — until that changes, the installing user needs
`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` in their environment (see above). Plugin
options (the serve URL / password when the bridge lands) are set with
`claude plugin configure tg-messenger`.

## Transport (issues #246, #247)

With `serveUrl` / `webPass` / `dialog` configured (`claude plugin configure
tg-messenger`; values arrive as the `options` argument of `register`), the
pane is live: the composer POSTs to `tg-messenger serve` (`/login` → HMAC
cookie, `POST /send`) and the dialog's SSE stream (`/stream/{id}`) appends
incoming lines. Without config the pane degrades to a local-only scratchpad
with a one-line toast. The transport lives in `hooks/register.tsx` (the
engine follows `$` only within the file that declares it) — one host `curl`
child per call, no dependencies; the password never appears in argv or logs.

## Dialog targeting (#248)

The `dialog` option picks which Telegram dialog the pane talks to: a marked
numeric dialog id (negative for groups, as shown by `tg-messenger` dialogs).
`@usernames` are accepted syntactically but **not resolvable in v1** — serve
has no dialog-list endpoint the mod could resolve against (and per-message
resolving would break flood discipline), so a username is rejected with a
clear toast and the pane stays in the dead-safe no-send mode (composer
visible, sends fail fast, nothing auto-retries). The same applies to any
malformed value; with the option unset the pane header shows how to set it.
The pre-#248 `dialogId` option spelling still works.
