import { expect, test } from 'claude-code/testing'

import { OPENED, OPEN_PLACEMENT, until } from './pane-harness'

// The live `listen` stream is deaf on some networks (updates never reach a
// second Telethon connection — reproduced on a bare CLI). The pane must
// therefore re-read history on an interval and draw what changed.

// module scope: the kit may not teleport closures captured by hook callbacks
let reads = 0

const DUMP1 = ['← [70] старое', '← [71] первое', ''].join('\n')
const DUMP2 = ['← [70] старое', '← [72] догруз от опроса', ''].join('\n')

test('pane re-reads history on the poll interval', { options: { profile: 'p', dialog: '999', pollMs: 300 } }, async ($, on) => {
  reads = 0
  on('process.spawn', async function* ($, e) {
    const argv = e.argv.join(' ')
    if (/ read /.test(argv)) {
      reads++
      yield { stream: 'stdout', text: reads === 1 ? DUMP1 : DUMP2 }
      return { value: { code: 0, signal: null } }
    }
    if (/ listen /.test(argv)) await new Promise(() => {})
    // the hook's waits are spawned children (`sleep SECONDS`); the test realm's
    // own timers do run, so pacing the stub here paces the poll
    if (/^sleep /.test(argv)) {
      // answer two ticks (two polls), then hang: the loop must end the test
      // SUSPENDED on the engine's spawn — like the `listen` child above — or
      // the kit never settles over a perpetually re-arming hook loop
      if (reads >= 2) await new Promise(() => {})
      await new Promise(done => {
        ;(globalThis as unknown as { setTimeout: (fn: (value: unknown) => void, ms: number) => void }).setTimeout(done, 50)
      })
      yield { stream: 'stdout', text: '.' }
      return { value: { code: 0, signal: null } }
    }
    // non-empty: the kit rejects empty-text yields (the boot's daemon probe
    // lands here) — a blank body reads as "no daemon", cold path
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
  // «старое» is in BOTH dumps: the first poll can replace the tree before the
  // boot render of DUMP1 is ever observed, so the DUMP1-only line is a race
  await until(async () => (await mounted.find({ type: 'Text', text: /старое/ })) !== undefined, 'initial history')
  await until(async () => reads >= 2, 'poll re-read')
  await until(async () => (await mounted.find({ type: 'Text', text: /догруз от опроса/ })) !== undefined, 'polled message drawn')
  expect(reads >= 2, 'poll re-read happened').toBe(true)
})
