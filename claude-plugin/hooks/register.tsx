import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { TgDialog, TgMessage } from '../types'

/**
 * tg-messenger mod — pane over the tg-messenger CLI.
 *
 * `/tg` opens a bottom pane (the cc-arcade kind): `$.ui.open` asks for it,
 * a `ui.render` hook on `{component: 'Pane', requestId}` draws it with JSX.
 * The transport is the project's own CLI (`tg-messenger …`) spawned per call
 * through `$.process.spawn` — no HTTP, no passwords, nothing in argv but a
 * profile name and a dialog id:
 *
 *   history   `--profile P read <id|@user> --limit 50`   (once per pane open)
 *   live      `--profile P listen --ids --out`     (one long-running child)
 *   send      `--profile P send <id|@user> <text> | send <id|@user> --file <path>`
 *   react     `--profile P react <id|@user> <msg-id> <emoji>`
 *
 * An `@username` dialog goes to every child VERBATIM (#268): each spawn is a
 * fresh cold process, and a numeric id there makes Telethon page the whole
 * dialog list — one ResolveUsername RPC beats that. The resolved numeric id
 * feeds the live-stream line filter only.
 *
 * `profile` comes from the plugin's userConfig; an empty one is auto-resolved
 * at pane boot from `tg-messenger profiles` — exactly one valid saved profile
 * is picked (logged + shown in the header), zero or several is a refusal. The
 * mod never picks an account silently. Without a configured dialog the pane
 * boots into the DIALOG PICKER — the pick completes the setup; an invalid
 * dialog or an unresolvable profile degrade to the local-only append.
 *
 * The transport lives in this same file on purpose: the engine follows `$`
 * only into functions declared here, never across an import.
 */

const PANE = 'tg'

const draft = atom({ plugin: 'tg-messenger', key: 'draft' } as const, 0)

// message id whose reaction palette is open (-1 = none) — the web's toggle pattern
const paletteFor = atom({ plugin: 'tg-messenger', key: 'paletteFor' } as const, -1)

// the same 4 presets as web/TUI REACTION_PRESETS
const REACTION_PRESETS = ['👍', '❤️', '🔥', '😂'] as const

const messages = atom({ plugin: 'tg-messenger', key: 'messages' } as const, [] as TgMessage[])

// #256: text of a FAILED send, restored into the composer until the first
// keystroke touches it (cleared in onInput). Rendered as `value` only while
// non-empty, so stream-frame redraws never clobber live typing.
const pendingSend = atom({ plugin: 'tg-messenger', key: 'pendingSend' } as const, '')

// pane view: the open dialog or the dialog picker; null list = not loaded yet
const view = atom({ plugin: 'tg-messenger', key: 'view' } as const, 'chat' as 'chat' | 'dialogs')
const dialogList = atom({ plugin: 'tg-messenger', key: 'dialogList' } as const, null as TgDialog[] | null)

// loading indicator: what is being fetched right now. Deliberately STATIC — an
// animated ticker (~120 ms redraws) made the async render hook overlap itself
// and the interleaved output tore wrapped lines of different messages together.
// register() clears the atom: a reload mid-load must not leave it stuck.
const loading = atom({ plugin: 'tg-messenger', key: 'loading' } as const, null as 'history' | 'dialogs' | null)

async function spinWhile($: EngineInterface, kind: 'history' | 'dialogs', run: () => Promise<unknown>): Promise<void> {
  await update($, loading, () => kind)
  try {
    await run()
  } finally {
    await update($, loading, () => null)
  }
}

// message ids this mod itself sent (for `--out` echo suppression) — the TUI's
// `_sent_ids` pattern: membership-only, never popped (a pop would re-duplicate
// a reconnect re-echo), bounded FIFO
const sentIds = new Set<number>()

function rememberSent(id: number): void {
  sentIds.add(id)
  if (sentIds.size > 500) sentIds.delete(sentIds.values().next().value as number)
}

// --- CLI transport -----------------------------------------------------------

type ModConfig = {
  profile: string
  /** what the user configured (numeric id or @username) — shown in the header */
  target: string
  /** resolved numeric dialog id (set by `bootPane`), '' until then */
  resolvedId: string
  /** true when the resolved dialog is a group/channel (marked negative) */
  isGroup: boolean
  /** config validity: a usable target. `switchDialog` flips it when the pick
   * completes a picker boot — the render reads it LIVE, so the pane comes
   * alive mid-session without a reopen. */
  ready: boolean
  /** why not ready — dim in the header, and the refused-send toast */
  reason: string
  /** history re-read interval (ms) — the live stream's safety net; kit tests pass a small one */
  pollMs: number
  /** port of the warm `serve` daemon (set by `ensureDaemon`); unset = cold CLI path */
  daemonPort?: number
}

/**
 * Per-call peer ref. An `@username` target goes through VERBATIM: every spawned
 * CLI child is a fresh cold process, and a numeric id there makes Telethon page
 * the whole dialog list (#268) — the `@` ref is one ResolveUsername RPC. The
 * resolved numeric id stays for the live-stream line filter only.
 */
function peerRef(cfg: ModConfig): string {
  return cfg.target.startsWith('@') ? cfg.target : cfg.resolvedId
}

type CliResult = { stdout: string; stderr: string }

/** Runs one `tg-messenger` child to completion and collects its streams. */
async function runCli($: EngineInterface, args: readonly string[]): Promise<CliResult> {
  let stdout = ''
  let stderr = ''
  for await (const chunk of $.process.spawn({ argv: ['tg-messenger', ...args] })) {
    if (chunk.stream === 'stdout') stdout += chunk.text
    else stderr += chunk.text
  }
  return { stdout, stderr }
}

/** Click's failure line (`Error: <text>` on stderr, exit 1) — or null on success. */
function cliError(stderr: string): string | null {
  const m = /^Error: (.*)$/m.exec(stderr)
  return m ? m[1] : null
}

// --- warm daemon transport ----------------------------------------------------
//
// A cold CLI child per action costs seconds (python boot + a full MTProto
// handshake — measured 6.2 s on this machine's lossy route, vs 1.1 s for one
// RPC on a live connection and ~1 ms cached). So the pane keeps ONE `serve`
// daemon per profile and reads/sends over localhost HTTP; every warm call
// falls back to the cold CLI path when the daemon is not (yet) answering. The
// daemon is a child of the pane's environment — teardown cancels it like the
// stream, and the catch guards below keep that cancellation off the worker.

/** Deterministic per-profile port: two panes on one account share the daemon.
 * A wide band keeps two DIFFERENT profiles from colliding — a collision can
 * never go warm (the identity probe rejects the foreign daemon), so it is
 * worth making astronomically unlikely. */
function daemonPortFor(profile: string): number {
  let h = 0
  for (let i = 0; i < profile.length; i++) h = (h * 31 + profile.charCodeAt(i)) >>> 0
  return 18080 + (h % 40000)
}

const WARM_POLL_MS = 4000

/** Ports with a daemon boot in flight — keyed by port so panes on different
 * profiles never block each other's warm path. */
const daemonBooting = new Set<number>()

/** One curl to the daemon; null = no daemon / request failed (caller falls
 * back to the CLI). `statusOnly` swaps the body for the HTTP code — a 204
 * success and a -f swallowed error are otherwise indistinguishable. */
async function runApiRaw(
  $: EngineInterface,
  port: number | undefined,
  method: 'GET' | 'POST',
  path: string,
  form: readonly string[] = [],
  statusOnly = false,
): Promise<string | null> {
  if (!port) return null
  const argv = [
    'curl', '-sf', '-m', '20', '-X', method,
    '-H', 'x-tg-messenger-csrf: 1',
    '-H', 'Accept: application/json',
  ]
  if (statusOnly) argv.push('-o', '/dev/null', '-w', '%{http_code}')
  for (const pair of form) argv.push('--data-urlencode', pair)
  argv.push(`http://127.0.0.1:${port}${path}`)
  let stdout = ''
  try {
    for await (const chunk of $.process.spawn({ argv })) {
      if (chunk.stream === 'stdout') stdout += chunk.text
    }
  } catch {
    return null
  }
  return stdout
}

/** JSON GET against the daemon's API; null = no daemon / not JSON. */
async function runApiJson($: EngineInterface, cfg: ModConfig, path: string): Promise<unknown | null> {
  const body = await runApiRaw($, cfg.daemonPort, 'GET', path)
  if (body === null || body.trim() === '') return null
  try {
    return JSON.parse(body)
  } catch {
    return null
  }
}

/** True when the daemon on `port` serves OUR profile — /api/health answers
 * the profile name, so a port collision with another account's daemon never
 * adopts the wrong one. Cheaper than a data route: no client calls at all. */
async function daemonAlive($: EngineInterface, port: number, profile: string): Promise<boolean> {
  const body = await runApiRaw($, port, 'GET', '/api/health')
  if (body === null) return false
  try {
    return (JSON.parse(body) as Record<string, unknown>).profile === profile
  } catch {
    return false
  }
}

/** Adopts an already-running daemon for this profile — one fast probe, safe
 * to await on the boot path (fails in milliseconds when nothing listens). */
async function ensureDaemon($: EngineInterface, cfg: ModConfig): Promise<void> {
  if (cfg.daemonPort || !cfg.profile) return
  const port = daemonPortFor(cfg.profile)
  if (!daemonBooting.has(port) && await daemonAlive($, port, cfg.profile)) cfg.daemonPort = port
}

/** Spawns the profile's daemon and waits (bounded, detached) for it to come
 * up; a real daemon needs its own connect (~seconds) before serving, and the
 * cold CLI path covers that window. An EADDRINUSE race against a sibling
 * pane's daemon is fine: the probe adopts whichever wins the port. */
async function startDaemon($: EngineInterface, cfg: ModConfig): Promise<void> {
  if (!cfg.profile) return
  const port = daemonPortFor(cfg.profile)
  if (cfg.daemonPort || daemonBooting.has(port)) return
  daemonBooting.add(port)
  try {
    void (async () => {
      for await (const chunk of $.process.spawn({
        argv: ['tg-messenger', '--profile', cfg.profile, 'serve', '--host', '127.0.0.1', '--port', String(port)],
      })) {
        if (chunk.stream === 'stderr') continue // uvicorn logs
      }
    })().catch(() => {}) // teardown cancellation must not escape a detached task
    for (let i = 0; i < 40; i++) {
      await waitMs($, 500)
      if (await daemonAlive($, port, cfg.profile)) {
        cfg.daemonPort = port
        return
      }
    }
  } finally {
    daemonBooting.delete(port)
  }
}

/** `read` line: `← [123] text` / `→ [124] my own` (core `message_line`). */
const HISTORY_LINE = /^([←→]) \[(\d+)\] (.*)$/

/**
 * Parses `read` stdout. Multiline bodies hang-indent under the text column by
 * exactly the prefix width — continuation lines are re-joined onto their
 * message. ponytail: a message whose continuation line is shorter than the
 * indent is joined verbatim (leading spaces lost) — text-format ambiguity.
 */
function parseHistory(stdout: string): TgMessage[] {
  const out: TgMessage[] = []
  let prefixLen = 0
  for (const raw of stdout.split('\n')) {
    const m = HISTORY_LINE.exec(raw)
    if (m) {
      out.push({ id: Number(m[2]), text: m[3], out: m[1] === '→' })
      prefixLen = m[1].length + m[2].length + 4 // `← [123] ` = arrow+space+[+id+]+space
      continue
    }
    if (out.length && raw.startsWith(' '.repeat(Math.min(prefixLen, raw.length)))) {
      out[out.length - 1].text += `\n${raw.slice(prefixLen)}`
    }
  }
  return out
}

/** Maps one core `Message` JSON row onto a pane message; null skips junk. */
function messageFromApi(raw: unknown): TgMessage | null {
  if (typeof raw !== 'object' || raw === null) return null
  const m = raw as Record<string, unknown>
  if (typeof m.id !== 'number') return null
  return {
    id: m.id,
    text: typeof m.text === 'string' ? m.text : '[медиа]',
    out: m.out === true,
  }
}

/** Maps one core `Dialog` JSON row onto a picker row; null skips junk. */
function dialogFromApi(raw: unknown): TgDialog | null {
  if (typeof raw !== 'object' || raw === null) return null
  const d = raw as Record<string, unknown>
  if (typeof d.id !== 'number' || typeof d.title !== 'string') return null
  return { id: String(d.id), title: d.title, unread: typeof d.unread === 'number' ? d.unread : 0 }
}

/**
 * Greedy word-wrap at `width` columns. The engine's Text does not re-wrap long
 * lines to the pane width — the paint layer clips them at the edge (the tree
 * holds the full text, the screen loses the tail), so the pane wraps BEFORE
 * drawing and there is never a line wider than the pane. Emoji count as 2
 * UTF-16 units but ≤2 terminal cells, so measuring by JS length errs short —
 * a line may wrap early, never clip.
 */
export function wrapText(text: string, width: number): string {
  const w = Math.max(2, width)
  const out: string[] = []
  for (const para of text.split('\n')) {
    let line = ''
    for (const word of para.split(' ')) {
      // a word longer than the whole width (a URL) hard-breaks at the column
      for (let k = 0; k < word.length; k += w) {
        const piece = word.slice(k, k + w)
        if (!line) line = piece
        else if (line.length + 1 + piece.length <= w) line += ` ${piece}`
        else {
          out.push(line)
          line = piece
        }
      }
    }
    out.push(line)
  }
  return out.join('\n')
}

/**
 * Pads every line with trailing spaces to `width`. FACT: the CLI dump and the
 * parsed tree are clean, while the screen glues fragments of OLDER frames
 * after a row whose new content is shorter — the engine's repaint overwrites
 * a shrunken row without clearing its stale tail, and the damage is stable
 * across repaints (diff baseline poisoned). Padding every drawn line to the
 * full budget makes a repaint overwrite the whole row — there is no stale
 * tail left to preserve. Screen-level, so the test kit (tree-only) can't
 * assert it; the transform itself is unit-tested.
 */
export function padLines(text: string, width: number): string {
  return text
    .split('\n')
    .map(l => (l.length < width ? l + ' '.repeat(width - l.length) : l))
    .join('\n')
}

/**
 * Reads recent history of the dialog and replaces the pane content.
 * Warm path first: the daemon's JSON API with `fresh=1` (the poll runs at a
 * sub-TTL cadence, the daemon's own live-event invalidations can't be relied
 * on when the transport drops updates). Falls back to the cold CLI read.
 * Status noise (`Loading history…`) rides stderr — only Click's `Error:`
 * line means failure.
 */
async function loadHistory($: EngineInterface, cfg: ModConfig): Promise<void> {
  const api = await runApiJson($, cfg, `/api/dialogs/${cfg.resolvedId}/messages?limit=50&fresh=1`)
  if (Array.isArray(api)) {
    await update($, messages, () =>
      api.map(messageFromApi).filter((m): m is TgMessage => m !== null),
    )
    return
  }
  const { stdout, stderr } = await runCli($, [
    '--profile',
    cfg.profile,
    'read',
    peerRef(cfg),
    '--limit',
    '50',
  ])
  const err = cliError(stderr)
  if (err) throw new Error(err)
  await update($, messages, () => parseHistory(stdout) as TgMessage[])
}

/**
 * `listen` line with ids: `← [DIALOG] [MSG] text` (incoming, DMs only) or
 * `→ [DIALOG] [MSG] text` (own messages from any device, `--out`). Returns
 * null for preamble and unknown lines and for this mod's own send echoes.
 * Pure — the caller merges a whole burst into ONE state update.
 */
const STREAM_LINE = /^([←→]) \[(\d+)\] \[(\d+)\] (.*)$/

function parseStreamLine(cfg: ModConfig, line: string): TgMessage | null {
  const m = STREAM_LINE.exec(line)
  if (!m || m[2] !== cfg.resolvedId) return null
  const mid = Number(m[3])
  if (m[1] === '→' && sentIds.has(mid)) return null // this mod's own send, echoed
  return { id: mid, text: m[4], out: m[1] === '→' }
}

// one live subscription per activation: pane reopen and session re-seat
// reuse it instead of stacking another `listen` child on a quiet dialog
let streamAlive = false

const startStream = ($: EngineInterface, cfg: ModConfig) => {
  if (streamAlive) return
  streamAlive = true
  void (async () => {
    try {
      await runStream($, cfg)
    } catch {
      // environment teardown (reload, pane re-open) cancels the loop's pending
      // waits; a rejection escaping a detached task crashes the hooks worker
    } finally {
      // any escape from the loop must leave the bridge revivable by the next /tg
      streamAlive = false
    }
  })()
}

async function runStream($: EngineInterface, cfg: ModConfig): Promise<never> {
  let backoff = 1000
  let lost = false // the panel line is on state change, not every retry
  while (true) {
    try {
      let buffer = ''
      for await (const chunk of $.process.spawn({
        argv: ['tg-messenger', '--profile', cfg.profile, 'listen', '--ids', '--out'],
      })) {
        if (chunk.stream === 'stderr') continue // status noise ("Listening for…")
        buffer += chunk.text
        let nl: number
        const batch: TgMessage[] = []
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl)
          buffer = buffer.slice(nl + 1)
          const msg = parseStreamLine(cfg, line)
          if (msg) batch.push(msg)
        }
        // one update per burst — a burst of per-message updates would trigger
        // just as many renders
        if (batch.length)
          await update($, messages, all =>
            [...all, ...batch].slice(-100) as TgMessage[],
          )
        backoff = 1000 // a live line proves the link works
        lost = false
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
    await waitMs($, backoff)
    backoff = Math.min(backoff * 2, 30000)
  }
}

// the live stream's safety net: on some networks the engine never receives
// server-initiated updates (reproduced on a bare CLI: a connected `listen`
// child stays silent even for cross-account incoming). The pane still
// converges by re-reading history on an interval — same one-per-activation
// guard as the stream; a failed re-read is logged, never fatal.
let pollAlive = false

const startPolling = ($: EngineInterface, cfg: ModConfig) => {
  if (pollAlive) return
  pollAlive = true
  void (async () => {
    try {
      while (true) {
        try {
          // a warm read is one RPC (~1 s), so the poll can tick 4× faster than
          // the cold CLI ever could; cfg.daemonPort is read live — the poll
          // speeds up on its own once the daemon is adopted
          await waitMs($, cfg.daemonPort ? WARM_POLL_MS : cfg.pollMs)
        } catch (error) {
          // a dead wait kills the loop — polling without it would hot-spin the
          // CLI read; say so and stop (the next /tg revives it)
          $.ui.log(`tg-messenger: history poll stopped: ${String(error)}`)
          return
        }
        try {
          await loadHistory($, cfg)
        } catch (error) {
          $.ui.log(`tg-messenger: history poll failed: ${String(error)}`)
        }
      }
    } catch {
      // same as startStream: teardown cancellation must not escape a detached
      // task — an unhandled rejection crashes the hooks worker
    } finally {
      pollAlive = false
    }
  })()
}

/**
 * Resolves the configured target to a numeric dialog id. Numeric passes
 * through; `@username` resolves ONCE here via the cached dialog list —
 * never per message (flood discipline).
 */
async function resolveDialog($: EngineInterface, cfg: ModConfig): Promise<void> {
  if (/^-?\d+$/.test(cfg.target)) {
    cfg.resolvedId = cfg.target
    cfg.isGroup = cfg.target.startsWith('-')
    return
  }
  const { stdout, stderr } = await runCli($, [
    '--profile',
    cfg.profile,
    'dialogs',
    '--find',
    cfg.target,
  ])
  const err = cliError(stderr)
  if (err) throw new Error(err)
  // rows are `id\ttitle…` — a numeric guard keeps any odd stdout line out
  const line = stdout.split('\n').find(l => /^\d+\t/.test(l))
  if (!line) throw new Error(`dialog ${cfg.target} not found`)
  cfg.resolvedId = line.slice(0, line.indexOf('\t'))
  cfg.isGroup = cfg.resolvedId.startsWith('-')
}

/** Loads the DM list for the picker — the 100 most recent dialogs (#270): a full
 * crawl on a huge account takes minutes and floods. Warm daemon first, cold CLI
 * fallback. */
async function loadDialogList($: EngineInterface, cfg: ModConfig): Promise<void> {
  const api = await runApiJson($, cfg, '/api/dialogs?tab=dm')
  if (Array.isArray(api)) {
    await update($, dialogList, () =>
      api.map(dialogFromApi).filter((d): d is TgDialog => d !== null),
    )
    return
  }
  const { stdout, stderr } = await runCli($, [
    '--profile',
    cfg.profile,
    'dialogs',
    '--limit',
    '100',
  ])
  const err = cliError(stderr)
  if (err) throw new Error(err)
  const list = stdout
    .split('\n')
    .map(l => /^(\d+)\t(.+?)(?: \((\d+) unread\))?$/.exec(l))
    .filter(m => m !== null)
    .map(m => ({ id: m[1], title: m[2], unread: Number(m[3] ?? 0) }))
  await update($, dialogList, () => list as TgDialog[] | null)
}

/** Switches the pane to another dialog: history re-reads, the live stream just re-filters. */
async function switchDialog($: EngineInterface, cfg: ModConfig, id: string): Promise<void> {
  cfg.target = id
  cfg.resolvedId = id
  cfg.isGroup = id.startsWith('-')
  // the pick completes a picker boot: readiness is read live by the render,
  // so the pane comes alive right here, no reopen needed
  cfg.ready = true
  cfg.reason = ''
  void ensureDaemon($, cfg) // no-op once adopted or while a spawn is in flight
  void update($, view, () => 'chat' as const)
  void update($, paletteFor, () => -1)
  // drop the previous dialog's text at once — the loading marker replaces it,
  // stale content of another dialog must not linger while the fetch runs
  void update($, messages, () => [] as TgMessage[])
  if (cfg.isGroup)
    $.ui.toast('tg-messenger: group dialog — history only (live feed is DM-only in v1)')
  await spinWhile($, 'history', () => loadHistory($, cfg).catch(error => {
    const message = error instanceof Error ? error.message : String(error)
    $.ui.toast(`tg-messenger: ${message}`)
  }))
  startStream($, cfg)
  startPolling($, cfg)
}

// --- outgoing ------------------------------------------------------------------

/** Sends text; returns the new message id when the transport reported one.
 * Warm path: POST /send on the daemon with `Accept: application/json`
 * (answered `{"id": N}`), then a warm history redraw — one RPC each, no cold
 * child. */
async function sendText($: EngineInterface, cfg: ModConfig, text: string): Promise<number | undefined> {
  const body = await runApiRaw($, cfg.daemonPort, 'POST', '/send', [
    `dialog_id=${cfg.resolvedId}`,
    `text=${text}`,
  ])
  if (body !== null && body.trim() !== '') {
    let id: number | undefined
    try {
      const parsed = JSON.parse(body) as Record<string, unknown>
      id = typeof parsed.id === 'number' ? parsed.id : undefined
    } catch {
      id = undefined
    }
    if (id != null) rememberSent(id)
    await loadHistory($, cfg)
    return id
  }
  const { stdout, stderr } = await runCli($, [
    '--profile',
    cfg.profile,
    'send',
    peerRef(cfg),
    text,
  ])
  const err = cliError(stderr)
  if (err) throw new Error(err)
  const m = /sent\. \[id=(\d+)\]/.exec(stdout)
  const id = m ? Number(m[1]) : undefined
  if (id != null) rememberSent(id)
  return id
}

/** Sends a file (`@PATH [caption]` composer syntax → `send --file/--caption`). */
async function sendMedia($: EngineInterface, cfg: ModConfig, path: string, caption: string | null): Promise<number | undefined> {
  const args = ['--profile', cfg.profile, 'send', peerRef(cfg), '--file', path]
  if (caption) args.push('--caption', caption)
  const { stdout, stderr } = await runCli($, args)
  const err = cliError(stderr)
  if (err) throw new Error(err)
  // media prints the same `sent. [id=N]` — record it so the `--out` echo
  // (text `<media>`/caption) is suppressed next to the optimistic `@path` line
  const m = /sent\. \[id=(\d+)\]/.exec(stdout)
  const id = m ? Number(m[1]) : undefined
  if (id != null) rememberSent(id)
  return id
}

// Wait `ms` by spawning the platform `sleep`: the kit's hook realm never
// pumps host timers and its $ carries no clock (probed — setTimeout callbacks
// never run, $.clock absent), so the only wait that exists in BOTH realms is
// a child process. In the engine it is a real /bin/sleep; in the kit the
// test's spawn stub answers it and paces time itself.
async function waitMs($: EngineInterface, ms: number): Promise<void> {
  for await (const _ of $.process.spawn({ argv: ['sleep', String(ms / 1000)] })) {
    void _
  }
}

/** Reacts to a message (`react DIALOG MSG_ID EMOTICON`). Warm POST first —
 * `statusOnly` tells a 204 success from a swallowed 4xx (then the cold CLI
 * re-runs and surfaces Click's error properly). */
async function sendReaction(
  $: EngineInterface,
  cfg: ModConfig,
  messageId: number,
  emoticon: string,
): Promise<void> {
  const code = await runApiRaw(
    $,
    cfg.daemonPort,
    'POST',
    `/dialogs/${cfg.resolvedId}/reaction`,
    [`message_id=${messageId}`, `emoticon=${emoticon}`],
    true,
  )
  if (code !== null && code.startsWith('2')) return
  const { stderr } = await runCli($, [
    '--profile',
    cfg.profile,
    'react',
    peerRef(cfg),
    String(messageId),
    emoticon,
  ])
  const err = cliError(stderr)
  if (err) throw new Error(err)
}

/** Attaches one emoticon under its target message, skipping a duplicate. */
function withReaction(all: TgMessage[], id: number, emoticon: string): TgMessage[] {
  return all.map(m =>
    m.id === id && !m.reactions?.includes(emoticon)
      ? { ...m, reactions: [...(m.reactions ?? []), emoticon] }
      : m,
  )
}

// --- media command (#250) -----------------------------------------------------

/**
 * Splits off the first shlex token of `s` (posix rules: quotes group, `\`
 * escapes, `#` is a plain char). Returns null on an unbalanced quote —
 * the mirror of `shlex.split`'s ValueError in the TUI parser.
 */
function shlexFirstToken(s: string): { token: string; rest: string } | null {
  let i = 0
  let token = ''
  let started = false
  while (i < s.length) {
    const c = s[i]
    if (c === ' ' || c === '\t') {
      if (started) return { token, rest: s.slice(i + 1) }
      i++
      continue
    }
    started = true
    if (c === "'") {
      i++
      while (i < s.length && s[i] !== "'") token += s[i++]
      if (i >= s.length) return null
      i++
    } else if (c === '"') {
      i++
      while (i < s.length && s[i] !== '"') {
        if (s[i] === '\\' && i + 1 < s.length && ' "\\$`'.includes(s[i + 1])) i++
        token += s[i++]
      }
      if (i >= s.length) return null
      i++
    } else if (c === '\\') {
      if (i + 1 >= s.length) return null
      token += s[i + 1]
      i += 2
    } else {
      token += c
      i++
    }
  }
  return started ? { token, rest: '' } : null
}

/**
 * Parses an `@PATH [caption]` composer command — quote-for-quote parity with
 * `parse_media_command` in `src/tg_messenger/tui/parsing.py`: `@` with no
 * path (or an unbalanced quote) is plain text, the path may be quoted, the
 * remainder verbatim is the caption. Pure — no filesystem.
 */
function parseMediaCommand(
  text: string,
): { path: string; caption: string | null } | null {
  if (!text.startsWith('@')) return null
  const first = shlexFirstToken(text.slice(1))
  if (!first || !first.token) return null
  return { path: first.token, caption: first.rest.trim() || null }
}

// --- pane --------------------------------------------------------------------

/**
 * Parses `tg-messenger profiles` stdout into the VALID profile names
 * (`<name> ✓ ok`; a `✗ broken` session is never auto-picked). The greeting
 * line ("No profiles yet — run: …") matches nothing.
 */
export function parseValidProfiles(stdout: string): string[] {
  const out: string[] = []
  for (const raw of stdout.split('\n')) {
    const name = /^(\S+) ✓ ok$/.exec(raw.trim())?.[1]
    if (name) out.push(name)
  }
  return out
}

/**
 * Resolves the account when `profile` is not configured: exactly one valid
 * saved profile is picked (logged, and shown in the header — never silent);
 * zero or several stays a refusal with the fix in the message.
 */
async function resolveProfile($: EngineInterface, cfg: ModConfig): Promise<void> {
  if (cfg.profile) return
  const { stdout } = await runCli($, ['profiles'])
  const valid = parseValidProfiles(stdout)
  if (valid.length === 1) {
    cfg.profile = valid[0] as string // length checked just above
    $.ui.log(`tg-messenger: profile auto-resolved: ${valid[0]}`)
    return
  }
  if (valid.length === 0)
    throw new Error('no valid profile — run: tg-messenger login  (or claude plugin configure tg-messenger)')
  throw new Error(`several profiles (${valid.join(', ')}) — pick one: claude plugin configure tg-messenger`)
}

/**
 * Reads the `userConfig` options into the mutable config. `profile` is
 * optional — an empty one is auto-resolved from the saved sessions at boot
 * (`resolveProfile`); `dialog` is a marked numeric id or `@username`. An
 * ABSENT dialog is not an error: the pane boots into the dialog picker and
 * `switchDialog` completes the setup (`ready` flips there). Only a
 * set-but-invalid dialog stays dead (`ready: false` + `reason`).
 */
function readConfig(options: Readonly<Record<string, unknown>>): ModConfig {
  const pick = (name: string): string =>
    typeof options[name] === 'string' ? (options[name] as string).trim() : ''
  // `dialogId` is the pre-#248 spelling — honor it so an old config degrades
  // to the picker instead of a dead pane
  const target = pick('dialog') || pick('dialogId')
  const pollMs = typeof options.pollMs === 'number' && options.pollMs > 0 ? options.pollMs : 15000
  let reason = ''
  if (!target) reason = 'dialog not configured — pick one from «диалоги»'
  else if (!/^-?\d+$/.test(target) && !target.startsWith('@'))
    reason = `invalid dialog "${target}" — expected a numeric id or @username`
  return {
    profile: pick('profile'),
    target,
    resolvedId: '',
    isGroup: false,
    ready: reason === '',
    reason,
    pollMs,
  }
}

/**
 * Opens the pane and boots the transport: resolve → history → live stream —
 * or, with no dialog configured, straight into the dialog picker (the pick
 * finishes the boot). A set-but-unusable config gets its toast right here
 * (#248).
 * Top-level declaration: the engine's static checks only let `$` be passed to
 * functions declared at the top of the file.
 */
async function bootPane($: EngineInterface, cfg: ModConfig) {
  $.ui
    .open({ id: PANE, title: 'tg-messenger', closeOnEscape: true, focus: true })
    .catch((error: unknown) => $.ui.log(`tg-messenger: open failed: ${String(error)}`))
  if (!cfg.ready && cfg.target) {
    // set but unusable (invalid shape): dead-safe, say why
    $.ui.toast(`tg-messenger: ${cfg.reason}`)
    return
  }
  try {
    await resolveProfile($, cfg)
    if (cfg.ready) await resolveDialog($, cfg)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    $.ui.toast(`tg-messenger: ${message}`)
    await update($, messages, all =>
      [...all, { text: message, out: false, system: true }].slice(-100) as TgMessage[],
    )
    return
  }
  // adopt an already-running daemon (one fast probe — a pane reopen or a
  // second pane on the account then boots warm); none answering → spawn one
  // in the background, the cold CLI path covers the warm-up window
  await ensureDaemon($, cfg)
  if (!cfg.daemonPort) void startDaemon($, cfg).catch(() => {})
  if (!cfg.ready) {
    // no dialog configured: the picker IS the boot; `switchDialog` completes
    // it — readiness flips there, the stream and the poll start on the pick
    await update($, view, () => 'dialogs' as const)
    await spinWhile($, 'dialogs', () => loadDialogList($, cfg).catch(error => {
      $.ui.toast(`tg-messenger: ${error instanceof Error ? error.message : String(error)}`)
    }))
    return
  }
  // history is best-effort: a failure (e.g. Telegram unreachable) must not
  // kill the bridge — the live stream retries with backoff and self-heals
  await spinWhile($, 'history', () => loadHistory($, cfg).catch(error => {
    const message = error instanceof Error ? error.message : String(error)
    $.ui.toast(`tg-messenger: ${message}`)
    return update($, messages, all =>
      [...all, { text: message, out: false, system: true }].slice(-100) as TgMessage[],
    )
  }))
  if (cfg.isGroup)
    $.ui.toast('tg-messenger: group dialog — history only (live feed is DM-only in v1)')
  else {
    startStream($, cfg)
    startPolling($, cfg)
  }
}

function openPane($: EngineInterface, cfg: ModConfig) {
  // a dead environment mid-boot rejects the whole chain; a detached rejection
  // crashes the hooks worker, so the boot is guarded like the loops above
  void bootPane($, cfg).catch(() => {})
}

export const register: Register = (on, options) => {
  const cfg = readConfig(options)

  on('session.start', async ($, e, next) => {
    // a reload mid-load kills the ticker but leaves the atom set — a frozen
    // loading marker must not survive into the fresh module
    void update($, loading, () => null)
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

    if (panes.some(p => p.id === PANE)) openPane($, cfg)

    return next(e)
  })

  on('command.run', { command: 'tg' }, async $ => {
    openPane($, cfg)

    return { text: 'tg-messenger: pane opened below the prompt.' }
  })

  // one draw at a time: the hook is async (every atom read is an await point),
  // so a burst of state updates — a stream burst, a send's echo — starts
  // overlapping renders, and their interleaved output splices wrapped lines of
  // DIFFERENT messages together (панель черепком). The gate serializes draws;
  // a queued render re-reads all atoms fresh, only its geometry snapshot may
  // be a few ms stale.
  // promise-gate mutex: every render waits for the previous one and releases
  // the next in a finally. (A busy-wait flag starved the very I/O completions
  // the holder awaited.) $ stays in the hook's own scope — the engine's static
  // checks reject it inside a nested closure.
  let renderGate = Promise.resolve()
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const prev = renderGate
    let release: () => void = () => {}
    renderGate = new Promise<void>(done => {
      release = done
    })
    await prev
    try {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const list = await read($, messages)
    const openPalette = await read($, paletteFor)
    const draftLen = await read($, draft)
    const viewName = await read($, view)
    const dialogs = await read($, dialogList)
    const loadingNow = await read($, loading)
    // pane geometry: diff reads e.props.scroll.bodyRows/bodyColumns
    const props = (e as { props?: { scroll?: { bodyRows?: number }; bodyColumns?: number } }).props
    const cols = (props?.bodyColumns ?? e.viewport?.columns ?? 80) - 2
    const chat = viewName === 'chat'
    const composerRows = chat
      ? Math.min(5, Math.max(1, Math.ceil((draftLen + 1) / Math.max(4, cols - 4))))
      : 0
    // the engine (2.1.287) stopped stretching docked panes, so flexGrow alone
    // left the composer floating mid-pane: size the list area EXPLICITLY
    // from the pane's body height (fixed spend: top padding + header + gaps
    // (+ separator + composer in chat); bottom padding is 0 — verified flush
    // at 200x60 and 110x40). Falls back to flex when the engine reports no
    // body height.
    const fixed = chat ? 5 : 3
    const listRows = props?.scroll?.bodyRows
      ? Math.max(1, props.scroll.bodyRows - fixed - composerRows)
      : undefined
    // The engine's pointer map follows the FULL list layout, not the clipped
    // flex-end view: an overflowing list puts every click rows off (proven on
    // a live pane — header buttons press, body buttons never do). Show only
    // what fits, so the drawn layout IS the pointer layout.
    const perRows = (m: TgMessage): number => {
      const prefix = m.out ? '→ ' : m.system ? '· ' : '← '
      const lines = wrapText(prefix + m.text, Math.max(2, cols - 4)).split('\n').length
      return lines + (m.reactions?.length ? 1 : 0)
    }
    const budget = Math.max(1, (listRows ?? 35) - 2) // headroom: the open palette row
    const shown: TgMessage[] = []
    let used = 0
    for (let i = list.length - 1; i >= 0 && used < budget; i--) {
      const m = list[i] as TgMessage
      const rows = perRows(m)
      if (used + rows > budget) {
        if (i !== list.length - 1) break
        // a single message taller than the whole pane: draw its TAIL — the
        // newest row must always render, an empty list would sit here showing
        // "loading history…" forever (already-loaded history!)
        const tail = wrapText(m.text, Math.max(2, cols - 4)).split('\n').slice(-(budget - 1)).join('\n')
        shown.unshift({ ...m, text: tail })
        used = budget
        break
      }
      shown.unshift(m)
      used += rows
    }

    return (
      <Box flexDirection="column" flexGrow={1} gap={1} padding={1} paddingBottom={0}>
        {/* No <> fragments among the row children: the engine does NOT flatten
            them — a Fragment renders as one node stacking its children
            vertically, which kept the диалоги/назад buttons on their own line
            under the middle text no matter the widths. Every element is a
            direct child of the row; JSX false/undefined children are skipped. */}
        <Box gap={2}>
          <Text bold>tg</Text>
          {chat ? (
            <Text>{cfg.ready ? `— ${[cfg.profile, cfg.target].filter(Boolean).join(' · ')}` : <Text dimColor>— {cfg.reason}</Text>}</Text>
          ) : (
            <Text>— диалоги</Text>
          )}
          {/* reachable whenever the picker is: a valid configured dialog, or
              none at all (picker boot) — hidden only in the dead invalid mode */}
          {chat && (cfg.ready || !cfg.target) && (
            <Button
              onPress={() => {
                void update($, view, () => 'dialogs' as const)
                if (dialogs === null)
                  void spinWhile($, 'dialogs', () => loadDialogList($, cfg).catch(error =>
                    $.ui.toast(`tg-messenger: ${error instanceof Error ? error.message : String(error)}`),
                  ))
              }}
            >
              диалоги
            </Button>
          )}
          {/* назад only when a dialog is open: in the picker boot's dialogs
              view there is nothing to go back to (a dead-looking press) */}
          {!chat && cfg.target && (
            <Button onPress={() => void update($, view, () => 'chat' as const)}>← назад</Button>
          )}
          <Button role="dismiss" onPress={() => $.ui.close({ id: PANE })}>
            close
          </Button>
        </Box>
        {chat ? (
        <>
        <Box
          flexDirection="column"
          flexGrow={1}
          justifyContent="flex-end"
          overflow="hidden"
          height={listRows}
        >
          {shown.length === 0 && !loading && (
            <Text dimColor>{cfg.ready ? 'loading history…' : 'not configured — see the header'}</Text>
          )}
          {loadingNow === 'history' && (
            <Text dimColor>⏳ загружаю историю…</Text>
          )}
          {shown.map((m, i) => {
            const canReact = cfg.ready && !m.out && !m.system && m.id != null
            // the trigger is a plain Button whose LABEL is the literal "[+]"
            // (plain draws the label alone) — dim at rest, inverted under the
            // pointer; 3 cells + the 1-col gap
            const w = cols - (canReact ? 4 : 0)
            const react = (emoticon: string) => {
              const id = m.id as number
              void update($, paletteFor, () => -1)
              // a repeat pick is a no-op: the CLI toggle would turn the
              // reaction off and nothing would come back to redraw it
              if (m.reactions?.includes(emoticon)) return
              void (async () => {
                try {
                  await sendReaction($, cfg, id, emoticon)
                  await update($, messages, all => withReaction(all, id, emoticon))
                } catch (error) {
                  const message = error instanceof Error ? error.message : String(error)
                  $.ui.toast(`tg-messenger: ${message}`)
                }
              })()
            }
            const body = padLines(
              wrapText(
                m.out ? `→ ${m.text}` : m.system ? `· ${m.text}` : `← ${m.text}`,
                w,
              ),
              w,
            )
            return (
              <Box flexDirection="column" key={String(m.id ?? `i${i}`)}>
                {/* scope key MUST be a plain string: a numeric key names no
                    scope and the pointer goes inert over everything inside,
                    the [+] trigger included (engine d.ts, Box key docs) */}
                <Box gap={1}>
                  {/* the engine's Text does not re-wrap to the pane width —
                      long lines are painted clipped at the edge (tree holds the
                      text, screen loses the tail), so wrap BEFORE drawing.
                      canReact rows share the line with the [ 🙂 ] button —
                      reserve its columns; the rest use the full width. */}
                  <Box flexGrow={1} flexShrink={1}>
                    <Text dimColor={!m.out} wrap="wrap">
                      {body}
                    </Text>
                  </Box>
                  {canReact && (
                    // NOT plain: a plain button draws its label alone and the
                    // terminal's pointer hit-test never fires it (proven on a
                    // live pane — SGR click); the drawn form is `[ + ]`
                    <Button
                      key={`react-${m.id}`}
                      dimColor
                      onPress={() =>
                        void update($, paletteFor, open => (open === m.id ? -1 : (m.id as number)))
                      }
                    >
                      +
                    </Button>
                  )}
                </Box>
                {m.reactions?.length ? (
                  <Text dimColor>{padLines(`  ${m.reactions.join(' ')}`, w)}</Text>
                ) : null}
                {canReact && openPalette === m.id && (
                  <Box gap={1}>
                    {REACTION_PRESETS.map(emoji => (
                      <Button key={emoji} onPress={() => react(emoji)}>
                        {emoji}
                      </Button>
                    ))}
                  </Box>
                )}
              </Box>
            )
          })}
        </Box>
        <Box flexDirection="column" gap={0}>
          <Text dimColor>{'─'.repeat(Math.max(1, cols))}</Text>
          <Box height={composerRows} overflow="hidden">
            <Input
            key="composer"
            placeholder={cfg.ready ? 'сообщение, @/path/to/file [caption]' : 'сообщение (уйдёт в никуда)'}
            submitLabel="send"
            value={(await read($, pendingSend)) || undefined}
            onInput={value => {
              // #256: the first keystroke takes ownership — the restore is done
              void update($, pendingSend, () => '')
              void update($, draft, () => value.length)
            }}
            onSubmit={value => {
              const text = value.trim()
              void update($, draft, () => 0)

              if (!text) return

              if (!cfg.ready) {
                // degraded, not broken: keep the local append, just say it went nowhere
                $.ui.toast(`tg-messenger: not sent — ${cfg.reason}`)
                void update($, messages, all => [...all, { text, out: true }].slice(-100) as TgMessage[])
                return
              }

              // #250: @PATH [caption] routes to the media upload; plain text to send
              const media = parseMediaCommand(text)

              void (async () => {
                try {
                  let id: number | undefined
                  if (media) {
                    id = await sendMedia($, cfg, media.path, media.caption)
                  } else {
                    id = await sendText($, cfg, text)
                  }
                  // #256: the restored text was resubmitted unchanged (no
                  // onInput fired) — the restore is consumed, don't re-draw it
                  void update($, pendingSend, () => '')
                  const shown = media ? `@${media.path}` : text
                  await update($, messages, all =>
                    [...all, { id, text: shown, out: true }].slice(-100) as TgMessage[],
                  )
                } catch (error) {
                  const message = error instanceof Error ? error.message : String(error)
                  $.ui.toast(`tg-messenger: ${message}`)
                  void update($, messages, all =>
                    [...all, { text: message, out: false, system: true }].slice(-100) as TgMessage[],
                  )
                  // #256: keep the composed text for fix/retry — drawn as the
                  // Input's value until the first keystroke (onInput clears it)
                  void update($, pendingSend, () => text)
                  void update($, draft, () => text.length)
                }
              })()
            }}
            />
          </Box>
        </Box>
        </>
        ) : (
          <Box flexDirection="column" flexGrow={1} overflow="hidden" height={listRows}>
            {dialogs === null || dialogs.length === 0 ? (
              <Text dimColor>
                {loadingNow === 'dialogs'
                  ? '⏳ загружаю диалоги…'
                  : dialogs === null ? 'loading dialogs…' : 'no dialogs'}
              </Text>
            ) : (
              dialogs.map(d => (
                <Button key={d.id} onPress={() => void switchDialog($, cfg, d.id)}>
                  {`${d.title}${d.unread ? ` (${d.unread})` : ''}`}
                </Button>
              ))
            )}
          </Box>
        )}
      </Box>
    )
    } finally {
      release()
    }
  })
}
