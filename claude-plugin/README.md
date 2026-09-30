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

## Next (issue #244)

Replace the HELLO branch in `hooks/register.ts` with the real bridge:
incoming DMs from `tg-messenger serve` (SSE `/stream/{id}`) surfaced into the
session, replies via `POST /dialogs/{id}/send`. Serve URL / `TG_WEB_PASS`
arrive as plugin options (`userConfig` in `plugin.json` + the `options`
argument of `register`).
