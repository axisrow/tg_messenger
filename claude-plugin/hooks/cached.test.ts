import { expect, test } from 'claude-code/testing'

import { OPENED, OPEN_PLACEMENT, until } from './pane-harness'

// #332: the first /tg of every session rides the cold CLI — python boot plus a
// full MTProto handshake (17–21 s measured on a cold read). The pane paints the
// profile's stored window at once (`read --cached`: no session, no connection)
// and the network read replaces it when it lands.

// module scope: the kit may not teleport closures captured by hook callbacks
let cachedReads = 0
let networkGate: Promise<void> = Promise.resolve()
let openNetwork: () => void = () => {}

test('pane paints the stored window before the network read lands', { options: { profile: 'p', dialog: '999', pollMs: 300 } }, async ($, on) => {
  cachedReads = 0
  networkGate = new Promise<void>(done => {
    openNetwork = done
  })
  on('process.spawn', async function* ($, e) {
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      // no daemon answers: the cold path
      yield { stream: 'stdout', text: ' ' }
      return { value: { code: 7, signal: null } }
    }
    if (/ serve /.test(argv) || / listen /.test(argv)) await new Promise(() => {})
    if (/ read --cached /.test(argv)) {
      cachedReads++
      yield { stream: 'stdout', text: '← [1] из кэша\n' }
      return { value: { code: 0, signal: null } }
    }
    if (/ read /.test(argv)) {
      await networkGate // the cold read is still connecting
      yield { stream: 'stdout', text: '← [1] из кэша\n← [2] свежее из сети\n' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      await new Promise(() => {}) // no polls: the boot alone is under test
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
  await until(async () => (await mounted.find({ type: 'Text', text: /из кэша/ })) !== undefined, 'stored window painted')
  expect(await mounted.find({ type: 'Text', text: /свежее из сети/ }), 'network read still in flight').toBeUndefined()

  openNetwork()
  await until(async () => (await mounted.find({ type: 'Text', text: /свежее из сети/ })) !== undefined, 'network history replaced it')
  expect(cachedReads, 'exactly one cached paint').toBe(1)
})
