# 05 — Claude Code Mod: Real-Account Acceptance (manual)

Human-run acceptance for the `/tg` pane mod (`claude-plugin/`, epic #244).
Same discipline as the rest of `scripts/e2e/`: never wired into CI or pytest,
run by an operator against a real Telegram account.

Safety rules, non-negotiable:

- Use the **already-authorized test account** — never log in, never request a
  code, never touch any personal account.
- Test messages go **only to test/self dialogs** — Saved Messages of the test
  account. Never send to real people.
- The operator needs a second logged-in client of the same test account
  (phone/desktop) to produce an incoming message.

## Prerequisites

```bash
# fresh code under test (this checkout), throwaway password for the run
TG_WEB_PASS=<throwaway> PYTHONPATH="$PWD/src" \
  tg-messenger --profile <test-profile> serve --port 18090
```

- `TG_WEB_PASS` set to a **throwaway** value for the run (serve refuses
  non-localhost without it; the value must never appear in output).
- Plugin configured in Claude Code:

```bash
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude
# then, inside the session (or via the CLI):
claude plugin configure tg-messenger   # serveUrl, webPass, dialog
```

  - `serveUrl` = `http://127.0.0.1:18090`
  - `webPass` = the throwaway `TG_WEB_PASS`
  - `dialog` = the numeric Saved Messages id of the test account
    (its own user id; same value discipline as `E2E_SAVED_ID` in `README.md`)

- Start the session with the mod loaded:

```bash
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude
# type /tg — the pane opens below the prompt
```

## Checklist

Record PASS / FAIL / SKIP for each; any FAIL blocks the epic.

### 1. Transport up

- [ ] serve starts and stays up on the chosen port
- [ ] `/tg` opens the pane without errors; header shows the live dialog
      (not the dimmed "set the dialog" line)

### 2. Incoming (real event → pane, within seconds)

- [ ] from the second client, post a short marked message to the test
      account's Saved Messages (e.g. `e2e-mod-in-<timestamp>`)
- [ ] the message appears in the pane **within ~5 s**, no manual refresh

### 3. Outgoing (pane → Telegram)

- [ ] type a reply in the pane composer (e.g. `e2e-mod-out-<timestamp>`)
      and send
- [ ] the message arrives in the Telegram dialog (verify in the second
      client), and shows as sent in the pane
- [ ] clean up: delete both e2e messages from Saved Messages afterwards

### 4. Explicit-send only

- [ ] with the pane open and idle, **nothing** is ever sent without the
      operator submitting the composer (watch the dialog for a minute)

### 5. Secret hygiene

- [ ] the pane output contains neither `TG_WEB_PASS` nor any phone number
      nor any session string
- [ ] serve log (`~/.tg_messenger/logs/`) for the run contains neither
- [ ] the Claude session transcript/pane copy contains neither
      (the password reaches the host only via stdin to `curl`, never argv)

## Parity stubs (deliberately not checked here)

- **Group chats** — v1 targets a single dialog id; group pane behavior
  (mentions, admin actions) is out of scope.
- **Media** — composer `@path` sends exist (#256) but real-account media
  round-trip is covered by `02_saved_messages.sh`, not this checklist.
- **Reconnect under network loss** — killing the network mid-stream and
  watching SSE resume is a separate guided scenario; the pane keeps its
  last state and the operator can reopen `/tg`.
- **`@username` dialog targeting** — rejected by design in v1 (#248);
  a toast path, not an acceptance item.

## Results template

```
date:
profile: <test account, NOT any personal account>
serve commit: <git rev-parse HEAD>
1 transport: PASS/FAIL
2 incoming:  PASS/FAIL   latency: <s>
3 outgoing:  PASS/FAIL
4 explicit-send: PASS/FAIL
5 secrets:   PASS/FAIL
notes:
```

The epic (#244) closes when this lands green.
