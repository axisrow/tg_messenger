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
 * mod never picks an account silently. Without a full config the pane
 * degrades to the local-only append with a one-line toast.
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

/**
 * Reads recent history of the dialog and replaces the pane content.
 * Status noise (`Loading history…`) rides stderr — only Click's `Error:`
 * line means failure.
 */
async function loadHistory($: EngineInterface, cfg: ModConfig): Promise<void> {
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
 * `→ [DIALOG] [MSG] text` (own messages from any device, `--out`). Preamble
 * and unknown lines are ignored.
 */
const STREAM_LINE = /^([←→]) \[(\d+)\] \[(\d+)\] (.*)$/

async function handleStreamLine($: EngineInterface, cfg: ModConfig, line: string): Promise<void> {
  const m = STREAM_LINE.exec(line)
  if (!m || m[2] !== cfg.resolvedId) return
  const mid = Number(m[3])
  if (m[1] === '→' && sentIds.has(mid)) return // this mod's own send, echoed
  await update(
    $,
    messages,
    all => [...all, { id: mid, text: m[4], out: m[1] === '→' }].slice(-100) as TgMessage[],
  )
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
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl)
          buffer = buffer.slice(nl + 1)
          await handleStreamLine($, cfg, line)
        }
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
    await $.clock.sleep(backoff)
    backoff = Math.min(backoff * 2, 30000)
  }
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
 * crawl on a huge account takes minutes and floods. */
async function loadDialogList($: EngineInterface, cfg: ModConfig): Promise<void> {
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
  void update($, view, () => 'chat' as const)
  void update($, paletteFor, () => -1)
  if (cfg.isGroup)
    $.ui.toast('tg-messenger: group dialog — history only (live feed is DM-only in v1)')
  try {
    await loadHistory($, cfg)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    $.ui.toast(`tg-messenger: ${message}`)
  }
  startStream($, cfg)
}

// --- outgoing ------------------------------------------------------------------

/** Sends text; returns the new message id when the CLI reported one. */
async function sendText($: EngineInterface, cfg: ModConfig, text: string): Promise<number | undefined> {
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

/** Reacts to a message (`react DIALOG MSG_ID EMOTICON`). */
async function sendReaction(
  $: EngineInterface,
  cfg: ModConfig,
  messageId: number,
  emoticon: string,
): Promise<void> {
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
 * Reads the `userConfig` options. `profile` is optional — an empty one is
 * auto-resolved from the saved sessions at boot (`resolveProfile`); `dialog`
 * is a marked numeric id or `@username`. `ready: false` → the mod stays in
 * the dead-safe no-send mode; `reason` is the one-line explanation.
 */
function readConfig(options: Readonly<Record<string, unknown>>): {
  cfg: ModConfig
  ready: boolean
  reason: string
  target: string
} {
  const pick = (name: string): string =>
    typeof options[name] === 'string' ? (options[name] as string).trim() : ''
  const profile = pick('profile')
  // `dialogId` is the pre-#248 spelling — honor it so an old config degrades
  // to a clear toast instead of "dialog not configured"
  const target = pick('dialog') || pick('dialogId')
  const cfg: ModConfig = { profile, target, resolvedId: '', isGroup: false }
  let reason = ''
  if (!target) reason = 'dialog not configured — claude plugin configure tg-messenger'
  else if (!/^-?\d+$/.test(target) && !target.startsWith('@'))
    reason = `invalid dialog "${target}" — expected a numeric id or @username`
  return { cfg, ready: reason === '', reason, target }
}

/**
 * Opens the pane and boots the transport: resolve → history → live stream.
 * A set-but-unusable config gets its toast right here (#248).
 * Top-level declaration: the engine's static checks only let `$` be passed to
 * functions declared at the top of the file.
 */
async function bootPane($: EngineInterface, cfg: ModConfig, ready: boolean, reason: string, target: string) {
  void $.ui.open({ id: PANE, title: 'tg-messenger', closeOnEscape: true, focus: true })
  if (!ready) {
    if (target) $.ui.toast(`tg-messenger: ${reason}`)
    return
  }
  try {
    await resolveProfile($, cfg)
    await resolveDialog($, cfg)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    $.ui.toast(`tg-messenger: ${message}`)
    await update($, messages, all =>
      [...all, { text: message, out: false, system: true }].slice(-100) as TgMessage[],
    )
    return
  }
  // history is best-effort: a failure (e.g. Telegram unreachable) must not
  // kill the bridge — the live stream retries with backoff and self-heals
  try {
    await loadHistory($, cfg)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    $.ui.toast(`tg-messenger: ${message}`)
    await update($, messages, all =>
      [...all, { text: message, out: false, system: true }].slice(-100) as TgMessage[],
    )
  }
  if (cfg.isGroup)
    $.ui.toast('tg-messenger: group dialog — history only (live feed is DM-only in v1)')
  else startStream($, cfg)
}

function openPane($: EngineInterface, cfg: ModConfig, ready: boolean, reason: string, target: string) {
  void bootPane($, cfg, ready, reason, target)
}

export const register: Register = (on, options) => {
  const { cfg, ready, reason, target } = readConfig(options)

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

    if (panes.some(p => p.id === PANE)) openPane($, cfg, ready, reason, target)

    return next(e)
  })

  on('command.run', { command: 'tg' }, async $ => {
    openPane($, cfg, ready, reason, target)

    return { text: 'tg-messenger: pane opened below the prompt.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const list = await read($, messages)
    const openPalette = await read($, paletteFor)
    const draftLen = await read($, draft)
    const viewName = await read($, view)
    const dialogs = await read($, dialogList)
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
    const shown = list.slice(-50)

    return (
      <Box flexDirection="column" flexGrow={1} gap={1} padding={1} paddingBottom={0}>
        <Box gap={2}>
          <Text bold>tg-messenger</Text>
          {chat ? (
            <>
              <Text>{ready ? `— ${[cfg.profile, cfg.target].filter(Boolean).join(' · ')}` : <Text dimColor>— {reason}</Text>}</Text>
              {ready && (
                <Button
                  onPress={() => {
                    void update($, view, () => 'dialogs' as const)
                    if (dialogs === null)
                      void loadDialogList($, cfg).catch(error =>
                        $.ui.toast(`tg-messenger: ${error instanceof Error ? error.message : String(error)}`),
                      )
                  }}
                >
                  диалоги
                </Button>
              )}
            </>
          ) : (
            <>
              <Text>— диалоги</Text>
              <Button onPress={() => void update($, view, () => 'chat' as const)}>← назад</Button>
            </>
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
          {shown.length === 0 && (
            <Text dimColor>{ready ? 'loading history…' : 'not configured — see the header'}</Text>
          )}
          {shown.map((m, i) => {
            const canReact = ready && !m.out && !m.system && m.id != null
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
            return (
              <Box flexDirection="column" key={m.id ?? `i${i}`}>
                <Box gap={1}>
                  <Text dimColor={!m.out} wrap="wrap">
                    {m.out ? `→ ${m.text}` : m.system ? `· ${m.text}` : `← ${m.text}`}
                  </Text>
                  {canReact && (
                    <Button
                      plain
                      onPress={() =>
                        void update($, paletteFor, open => (open === m.id ? -1 : (m.id as number)))
                      }
                    >
                      🙂
                    </Button>
                  )}
                </Box>
                {m.reactions?.length ? <Text dimColor>{`  ${m.reactions.join(' ')}`}</Text> : null}
                {canReact && openPalette === m.id && (
                  <Box gap={1}>
                    {REACTION_PRESETS.map(emoji => (
                      <Button plain key={emoji} onPress={() => react(emoji)}>
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
            placeholder={ready ? 'сообщение, @/path/to/file [caption]' : 'сообщение (уйдёт в никуда)'}
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

              if (!ready) {
                // degraded, not broken: keep the local append, just say it went nowhere
                $.ui.toast(`tg-messenger: not sent — ${reason}`)
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
              <Text dimColor>{dialogs === null ? 'loading dialogs…' : 'no dialogs'}</Text>
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
  })
}
