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
claude plugin configure tg-messenger   # profile + dialog
```

Function hooks are early access — until that changes, the installing user needs
`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` in their environment (see above).

## Transport

The pane drives the project's own CLI (`tg-messenger`) — no HTTP, no secrets:

- **history** — `--profile P read <id> --limit 50` on pane open;
- **live** — one long-running `--profile P listen --ids --out` child (incoming
  DMs + own messages from other devices), reconnected with backoff;
- **send** — `--profile P send <id> <text>`, media via `send <id> --file …`;
- **react** — `--profile P react <id> <msg-id> <emoji>`.

`tg-messenger` must be installed and logged in for the configured profile
(`tg-messenger login --profile P`). Without a full config the pane degrades to
a local-only scratchpad with a one-line toast. The transport lives in
`hooks/register.tsx` (the engine follows `$` only within the file that
declares it); every call is one child process over an argv array.

## Config (#248)

Two options (`claude plugin configure tg-messenger`):

- **profile** (optional) — the saved tg-messenger profile to use. When empty,
  the pane resolves it at boot from `tg-messenger profiles`: exactly one valid
  (`✓ ok`) saved profile is picked and shown in the header; zero valid → a
  clear "run `tg-messenger login`" error; several valid → the list plus a hint
  to configure. The mod never picks an account silently — the single-profile
  pick is logged and rendered.
- **dialog** — which dialog the pane talks to: a marked numeric dialog id
  (negative for groups, as shown by `tg-messenger dialogs`) or `@username`
  (resolved once per pane open via the cached dialog list — never per
  message). Group dialogs are history-only: the CLI's `listen` streams DMs
  only (v1). Anything malformed → clear toast, dead-safe no-send mode.

