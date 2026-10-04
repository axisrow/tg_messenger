import { expect, test } from 'claude-code/testing'

import { OPENED, OPEN_PLACEMENT, until } from './pane-harness'

// The warm layer: the pane spawns one `serve` daemon and reads history over
// localhost HTTP — a cold CLI child costs ~6 s (python boot + full MTProto
// handshake on a lossy route), a warm read is one RPC on a live connection.
// When no daemon answers, the pane must fall back to the cold CLI path.

let curls = 0
let reads = 0

const API_MESSAGES = JSON.stringify([{ id: 5, out: false, text: 'тепло из демона' }])

test('pane adopts the daemon and redraws history over HTTP', { options: { profile: 'p', dialog: '999', pollMs: 300 } }, async ($, on) => {
  curls = 0
  reads = 0
  on('process.spawn', async function* ($, e) {
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      curls++
      // messages fetch (poll) vs the identity probe (/api/health answers the
      // profile name — the adoption contract)
      if (/\/api\/dialogs\/999\/messages/.test(argv)) {
        yield { stream: 'stdout', text: API_MESSAGES }
      } else {
        yield { stream: 'stdout', text: '{"profile":"p"}' }
      }
      return { value: { code: 0, signal: null } }
    }
    if (/ serve /.test(argv)) await new Promise(() => {}) // the daemon is long-lived
    if (/ listen /.test(argv)) await new Promise(() => {}) // the stream is long-lived
    if (/ read /.test(argv)) {
      reads++
      yield { stream: 'stdout', text: '← [1] холодный бут' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      // answer two waits (probe backoff + first warm poll gap), then hang so the
      // realm settles SUSPENDED on an engine spawn
      if (curls >= 3) await new Promise(() => {})
      await new Promise(done => {
        ;(globalThis as unknown as { setTimeout: (fn: (value: unknown) => void, ms: number) => void }).setTimeout(done, 50)
      })
      yield { stream: 'stdout', text: '.' }
      return { value: { code: 0, signal: null } }
    }
    yield { stream: 'stdout', text: ' ' }
    return { value: { code: 0, signal: null } }
  })
  on('ui.open', async () => OPENED)

  const mounted = await $.ui.mount({
    plugin: 'tg-messenger',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'tg',
    props: OPEN_PLACEMENT,
  })
  await $.command.run({
    command: 'tg',
    args: '',
    origin: { kind: 'plugin', name: 'tg-messenger' },
    presentation: { isFullscreen: false, columns: 80 },
  })
  // the kit's daemon probe answers instantly, so even the boot read rides
  // HTTP; on a real boot the first read is cold (the daemon is still
  // connecting) and every later one goes warm — the stand proves that part
  await until(async () => (await mounted.find({ type: 'Text', text: /тепло из демона/ })) !== undefined, 'warm history drawn')
  expect(reads, 'no cold CLI child ever ran').toBe(0)
  expect(curls >= 2, 'probe plus history rode curl').toBe(true)
})

test('pane falls back to the cold CLI when no daemon answers', { options: { profile: 'p', dialog: '999', pollMs: 300 } }, async ($, on) => {
  curls = 0
  reads = 0
  on('process.spawn', async function* ($, e) {
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      curls++
      yield { stream: 'stdout', text: ' ' } // connection refused / empty body
      return { value: { code: 7, signal: null } }
    }
    if (/ serve /.test(argv)) await new Promise(() => {})
    if (/ listen /.test(argv)) await new Promise(() => {})
    if (/ read /.test(argv)) {
      reads++
      yield { stream: 'stdout', text: reads === 1 ? '← [1] холодный бут' : '← [2] догруз без демона' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      if (reads >= 2) await new Promise(() => {})
      await new Promise(done => {
        ;(globalThis as unknown as { setTimeout: (fn: (value: unknown) => void, ms: number) => void }).setTimeout(done, 50)
      })
      yield { stream: 'stdout', text: '.' }
      return { value: { code: 0, signal: null } }
    }
    yield { stream: 'stdout', text: ' ' }
    return { value: { code: 0, signal: null } }
  })
  on('ui.open', async () => OPENED)

  const mounted = await $.ui.mount({
    plugin: 'tg-messenger',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'tg',
    props: OPEN_PLACEMENT,
  })
  await $.command.run({
    command: 'tg',
    args: '',
    origin: { kind: 'plugin', name: 'tg-messenger' },
    presentation: { isFullscreen: false, columns: 80 },
  })
  await until(async () => (await mounted.find({ type: 'Text', text: /догруз без демона/ })) !== undefined, 'cold poll still draws')
  expect(reads >= 2, 'polls kept using the cold CLI').toBe(true)
  expect(curls >= 2, 'the daemon was probed first').toBe(true)
})
