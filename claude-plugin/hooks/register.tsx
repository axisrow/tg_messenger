import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { TgMessage } from '../types'

/**
 * tg-messenger mod — pane with a real serve transport (issues #246, #247).
 *
 * `/tg` opens a bottom pane (the cc-arcade kind): `$.ui.open` asks for it,
 * a `ui.render` hook on `{component: 'Pane', requestId}` draws it with JSX.
 * With `serveUrl`/`webPass`/`dialogId` configured the composer POSTs to
 * `tg-messenger serve` (`/login` → HMAC cookie, `POST /send`) and the pane
 * follows the dialog's SSE stream; without them it degrades to the old
 * local-only append with a one-line toast.
 *
 * The transport lives in this same file on purpose: the engine follows `$`
 * only into functions declared here, never across an import. Every call is
 * one host `curl` child through `$.process.spawn`, because `$.http.fetch`
 * reads the whole body before resolving and so can neither carry the login
 * 303 (cookie) nor hold the endless SSE stream. The password never appears
 * in argv (the form body goes to curl over stdin) and neither it nor the
 * cookie is ever logged.
 */

const PANE = 'tg'

const draft = atom({ plugin: 'tg-messenger', key: 'draft' } as const, 0)

const messages = atom(
  { plugin: 'tg-messenger', key: 'messages' } as const,
  [{ text: 'hello world — the bridge lands here', out: false }] as TgMessage[],
)

// --- serve transport ---------------------------------------------------------

type ServeConfig = { serveUrl: string; webPass: string; dialogId: string }

/**
 * Reads the `userConfig` options. `ready: false` → the mod stays in degraded
 * no-op mode; `reason` names the missing/invalid field for the one-line toast.
 */
function readConfig(options: Readonly<Record<string, unknown>>): {
  cfg: ServeConfig
  ready: boolean
  reason: string
} {
  const pick = (name: string): string =>
    typeof options[name] === 'string' ? (options[name] as string).trim() : ''
  const serveUrl = pick('serveUrl').replace(/\/+$/, '')
  const webPass = pick('webPass')
  const dialogId = pick('dialogId')
  const missing = !serveUrl
    ? 'serveUrl'
    : !webPass
      ? 'webPass'
      : !/^-?\d+$/.test(dialogId)
        ? 'dialogId'
        : ''
  return { cfg: { serveUrl, webPass, dialogId }, ready: missing === '', reason: missing }
}

// HMAC cookie of the current serve session ("tg_session=…"). Module state: per
// activation — a reload (a config change included) simply logs in again.
let cookie = ''

type CurlResponse = { status: number; setCookie: string; body: string }

/** Runs one curl child to completion and parses its `-i` response. */
async function curlOnce(
  $: EngineInterface,
  argv: readonly string[],
  input?: string,
): Promise<CurlResponse> {
  let out = ''
  let err = ''
  for await (const chunk of $.process.spawn({ argv, input })) {
    if (chunk.stream === 'stdout') out += chunk.text
    else err += chunk.text
  }
  if (!out && err.trim()) throw new Error(err.trim().slice(0, 200))
  const sep = out.indexOf('\r\n\r\n')
  const head = sep >= 0 ? out.slice(0, sep) : out
  const lines = head.split('\r\n')
  const status = Number.parseInt(lines[0]?.split(' ')[1] ?? '', 10) || 0
  const setCookie =
    lines.find(l => l.toLowerCase().startsWith('set-cookie:'))?.slice('set-cookie:'.length).trim() ??
    ''
  return { status, setCookie, body: sep >= 0 ? out.slice(sep + 4) : '' }
}

/** POSTs the login form (password over stdin) and keeps the HMAC cookie in module state. */
async function ensureLogin($: EngineInterface, cfg: ServeConfig): Promise<void> {
  if (cookie) return
  const res = await curlOnce(
    $,
    [
      'curl',
      '-isS',
      '-X',
      'POST',
      '-H',
      'content-type: application/x-www-form-urlencoded',
      '--data-binary',
      '@-',
      `${cfg.serveUrl}/login`,
    ],
    `password=${encodeURIComponent(cfg.webPass)}`,
  )
  // the pair up to the first attribute ("tg_session=…") is the whole Cookie header
  const pair = res.setCookie.split(';')[0] ?? ''
  if (!pair.startsWith('tg_session=')) {
    throw new Error(`login failed (HTTP ${res.status})`)
  }
  cookie = pair
}

/**
 * POSTs the outgoing message to /send (form dialog_id+text, the server's
 * same-origin header, cookie). Resolves only after the server answered with
 * the sent-bubble fragment; anything else (error fragment, a redirect to the
 * login wizard when the Telegram session itself is logged out) throws.
 */
/** One POST /send attempt with the current cookie. */
function postSend($: EngineInterface, cfg: ServeConfig, text: string): Promise<CurlResponse> {
  return curlOnce(
    $,
    [
      'curl',
      '-isS',
      '-X',
      'POST',
      '-H',
      'content-type: application/x-www-form-urlencoded',
      '-H',
      'x-tg-messenger-csrf: 1',
      '-H',
      `Cookie: ${cookie}`,
      '--data-binary',
      '@-',
      `${cfg.serveUrl}/send`,
    ],
    `dialog_id=${encodeURIComponent(cfg.dialogId)}&text=${encodeURIComponent(text)}`,
  )
}

/**
 * POSTs the outgoing message to /send (form dialog_id+text, the server's
 * same-origin header, cookie). Resolves only after the server answered with
 * the sent-bubble fragment; anything else (error fragment, a redirect to the
 * login wizard when the Telegram session itself is logged out) throws.
 */
async function sendText($: EngineInterface, cfg: ServeConfig, text: string): Promise<void> {
  await ensureLogin($, cfg)
  let res = await postSend($, cfg, text)
  if (res.status === 401) {
    // a serve restart regenerates its cookie key — the kept cookie is dead:
    // re-login once and retry before giving up
    forgetCookie()
    await ensureLogin($, cfg)
    res = await postSend($, cfg, text)
  }
  if (res.status !== 200 || !res.body.startsWith('<div class="msg ')) {
    // the server's error fragment text ("Select a dialog first.", read-only
    // chat, …) beats a bare status in the toast
    const detail = /<div class="error"[^>]*>([\s\S]*?)<\/div>/.exec(res.body)?.[1]
    throw new Error(detail ? `send failed: ${detail}` : `send failed (HTTP ${res.status})`)
  }
}

type StreamFrame = { id?: number; text?: string; out?: boolean; type?: string }

/** Drops the cookie so the next `ensureLogin` starts a fresh serve session. */
function forgetCookie(): void {
  cookie = ''
}

/**
 * Subscribes to GET /stream/{dialog} and yields parsed `data:` frames until
 * the stream drops (server restart, network, `-f` on a bad status). The
 * caller reconnects with backoff and a fresh login per attempt.
 */
async function* streamFrames(
  $: EngineInterface,
  cfg: ServeConfig,
): AsyncGenerator<StreamFrame> {
  await ensureLogin($, cfg)
  // ponytail: the cookie rides argv (visible in this Mac's ps); move to
  // `curl -H @file` if another local user ever becomes a real threat model
  const child = $.process.spawn({
    argv: [
      'curl',
      '-NsSf',
      '-H',
      `Cookie: ${cookie}`,
      '-H',
      'Accept: text/event-stream',
      `${cfg.serveUrl}/stream/${cfg.dialogId}`,
    ],
  })
  let buffer = ''
  for await (const chunk of child) {
    if (chunk.stream === 'stderr') {
      $.ui.log(`tg-messenger: stream: ${chunk.text.trim()}`)
      continue
    }
    buffer += chunk.text
    let end: number
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      const data = raw
        .split('\n')
        .filter(l => l.startsWith('data:'))
        .map(l => l.slice('data:'.length).trim())
        .join('\n')
      if (!data) continue
      try {
        yield JSON.parse(data) as StreamFrame
      } catch {
        // a frame we cannot read is not ours — skip it
      }
    }
  }
}

// --- pane --------------------------------------------------------------------

// one live SSE subscription per activation: pane reopen and session re-seat
// reuse it instead of stacking another curl child on a quiet dialog
let streamAlive = false

const startStream = ($: EngineInterface, cfg: ServeConfig) => {
  if (streamAlive) return
  streamAlive = true
  void (async () => {
    let backoff = 1000
    let lost = false // the panel line is on state change, not every retry
    while (true) {
      try {
        // a fresh login per attempt: a serve restart invalidates its cookies
        forgetCookie()
        for await (const frame of streamFrames($, cfg)) {
          backoff = 1000 // a live frame proves the link works
          lost = false
          // typed frames (translation/reaction) and our own echoes are not pane
          // lines — the composer already appended what this mod itself sent
          if (frame.type || frame.out || !frame.text) continue
          const text = frame.text
          await update($, messages, all => [...all, { text, out: false }].slice(-100) as TgMessage[])
        }
      } catch (error) {
        $.ui.log(`tg-messenger: stream error: ${String(error)}`)
        if (!lost) {
          lost = true
          const message = error instanceof Error ? error.message : String(error)
          await update($, messages, all =>
            [...all, { text: `bridge error: ${message}`, out: false, system: true }].slice(-100) as TgMessage[],
          )
        }
      }
      if (!lost) {
        lost = true
        const wait = Math.round(backoff / 1000)
        await update($, messages, all =>
          [...all, { text: `stream lost — retrying in ${wait} s`, out: false, system: true }].slice(-100) as TgMessage[],
        )
      }
      await $.clock.sleep(backoff)
      backoff = Math.min(backoff * 2, 30000)
    }
  })()
}

export const register: Register = (on, options) => {
  const { cfg, ready, reason } = readConfig(options)

  on('session.start', async ($, e, next) => {
    try {
      await $.command.register({
        name: 'tg',
        description: 'Telegram bridge: read recent messages and send replies',
      })
    } catch (error) {
      // ponytail: log-and-keep-going so a name collision never kills the session
      $.ui.log(`tg-messenger: /tg not registered: ${String(error)}`)
    }

    // hot reload: a pane left open keeps the previous drawing — re-seat it
    const panes = await $.ui.panes().catch(() => [])

    if (panes.some(p => p.id === PANE)) {
      void $.ui.open({ id: PANE, title: 'tg-messenger', closeOnEscape: true, focus: true })
      if (ready) startStream($, cfg)
    }

    return next(e)
  })

  on('command.run', { command: 'tg' }, async $ => {
    await $.ui.open({ id: PANE, title: 'tg-messenger', closeOnEscape: true, focus: true })
    if (ready) startStream($, cfg)

    return { text: 'tg-messenger: pane opened below the prompt.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const list = await read($, messages)
    // body height of the pane (docked panes are full-height): diff reads e.props.scroll.bodyRows
    const props = (e as { props?: { scroll?: { bodyRows?: number }; bodyColumns?: number } }).props
    const rows = props?.scroll?.bodyRows ?? e.viewport?.rows ?? 20
    const cols = (props?.bodyColumns ?? e.viewport?.columns ?? 80) - 2
    const shown = list.slice(-50)

    return (
      <Box flexDirection="column" flexGrow={1} gap={1} padding={1} paddingBottom={0}>
        <Box gap={2}>
          <Text bold>tg-messenger</Text>
          <Button role="dismiss" onPress={() => $.ui.close({ id: PANE })}>
            close
          </Button>
        </Box>
        <Box flexDirection="column" flexGrow={1} justifyContent="flex-end" overflow="hidden">
          {shown.map(m => (
            <Text dimColor={!m.out} wrap="wrap">
              {m.out ? `→ ${m.text}` : m.system ? `· ${m.text}` : `← ${m.text}`}
            </Text>
          ))}
        </Box>
        <Box flexDirection="column" gap={0}>
          <Text dimColor>{'─'.repeat(Math.max(1, cols))}</Text>
          <Box
            height={Math.min(5, Math.max(1, Math.ceil(((await read($, draft)) + 1) / Math.max(4, cols - 4))))}
            overflow="hidden"
          >
            <Input
            key="composer"
            placeholder={ready ? 'сообщение' : 'сообщение (уйдёт в никуда)'}
            submitLabel="send"
            onInput={value => {
              void update($, draft, () => value.length)
            }}
            onSubmit={value => {
              const text = value.trim()
              void update($, draft, () => 0)

              if (!text) return

              if (!ready) {
                // degraded, not broken: keep the local append, just say it went nowhere
                $.ui.toast(`tg-messenger: ${reason} not configured — not sent`)
                void update($, messages, all => [...all, { text, out: true }].slice(-100) as TgMessage[])
                return
              }

              void (async () => {
                try {
                  await sendText($, cfg, text)
                  await update($, messages, all => [...all, { text, out: true }].slice(-100) as TgMessage[])
                } catch (error) {
                  const message = error instanceof Error ? error.message : String(error)
                  $.ui.toast(`tg-messenger: ${message}`)
                  void update($, messages, all =>
                    [...all, { text: message, out: false, system: true }].slice(-100) as TgMessage[],
                  )
                }
              })()
            }}
            />
          </Box>
        </Box>
      </Box>
    )
  })
}
