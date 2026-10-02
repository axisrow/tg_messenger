# 05 — Claude Code Mod: Real-Account Acceptance (manual)

Human-run acceptance for the `/tg` pane mod (`claude-plugin/`, epic #244).
Same discipline as the rest of `scripts/e2e/`: never wired into CI or pytest,
run by an operator against a real Telegram account.

Safety rules, non-negotiable:

- Use the **already-authorized test account** — never log in, never request a
  code, never touch any personal account.
- Test messages go **only to test/self dialogs** — Saved Messages of the test
  account. Never send to real people.
- Independent verification runs through the same CLI the mod uses:
  `tg-messenger --profile <test-profile> read <dialog> --limit 3`.

## Prerequisites

- `tg-messenger` installed and the test profile already logged in
  (`tg-messenger profiles` shows `name ✓ ok`).
- Plugin configured in Claude Code (no passwords involved):

```bash
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude
# then, inside the session (or via the CLI):
claude plugin configure tg-messenger   # profile, dialog
```

  - `profile` = the test profile name (e.g. `default`);
  - `dialog` = the numeric Saved Messages id of the test account
    (its own user id; same value discipline as `E2E_SAVED_ID` in `README.md`).

- Start the session with the mod loaded:

```bash
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude
# type /tg — the pane opens below the prompt
```

## Checklist

Record PASS / FAIL / SKIP for each; any FAIL blocks the epic.

### 1. Transport up

- [ ] `/tg` opens the pane without errors; header shows `<profile> · <dialog>`
      (not the dimmed "not configured" line)
- [ ] recent history of the dialog appears in the pane within seconds
      (the mod runs `read --limit 50` on open)

### 2. Incoming (real event → pane, within seconds)

- [ ] from a second client of the SAME test account, post a short marked
      message to the test account's Saved Messages (e.g.
      `e2e-mod-in-<timestamp>`)
- [ ] the message appears in the pane **within ~5 s**, no manual refresh
      (the pane's `listen --out` child prints it as a `→` line)

### 3. Outgoing (pane → Telegram)

- [ ] type a reply in the pane composer (e.g. `e2e-mod-out-<timestamp>`)
      and send
- [ ] the message arrives in the Telegram dialog — verify independently:
      `tg-messenger --profile <test-profile> read <dialog> --limit 3`
- [ ] clean up: delete both e2e messages afterwards
      (`tg-messenger --profile <test-profile> delete <dialog> <ids> --yes`)

### 4. Media (@path)

- [ ] `@/tmp/e2e-mod.txt hello from e2e` in the composer sends the file
      (verify: the CLI `read` shows the media message)

### 5. Explicit-send only

- [ ] with the pane open and idle, **nothing** is ever sent without the
      operator submitting the composer (watch the dialog for a minute)

### 6. Secret hygiene

- [ ] the pane output contains no session strings or phone numbers (there is
      no password in this transport at all — nothing to leak)

## Parity stubs (deliberately not checked here)

- **Group chats (live)** — `listen` streams DMs only; a group dialog is
  history-only with a one-line toast. Group live feed is a follow-up.
- **Incoming reactions display** — the CLI does not stream reactions; the
  pane shows only reactions this mod itself attached. Send (`react`) works.
- **Reconnect under network loss** — killing the network mid-stream and
  watching the pane resume is a separate guided scenario; the pane keeps its
  last state (the `listen` child reconnects with backoff) and the operator
  can reopen `/tg`.
- **Voice/video-note sends** — the composer exposes only `@path [caption]`;
  the voice/video-note flags of `send` are not exposed (v1).

## Results template

```
date:
profile: <test account, NOT any personal account>
mod commit: <git rev-parse HEAD>
1 transport: PASS/FAIL
2 incoming:  PASS/FAIL   latency: <s>
3 outgoing:  PASS/FAIL
4 media:     PASS/FAIL
5 explicit-send: PASS/FAIL
6 secrets:   PASS/FAIL
notes:
```

The epic (#244) closes when this lands green.
