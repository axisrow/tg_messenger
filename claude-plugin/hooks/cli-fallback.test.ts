import { expect, test } from 'claude-code/testing'

import { OPENED, OPEN_PLACEMENT, until } from './pane-harness'

// #293: the cold CLI fallback built argv with the peer and the text as BARE
// positionals — Click parsed a negative (group) id and a leading-dash text as
// options (`No such option '-1'`), and `send 777 --help` printed usage with
// exit 0, which the pane took for a sent message: a silent message loss.
// Every positional must ride AFTER `--`, and a send whose stdout carries no
// `sent.` confirmation line is a failure, never a success.

const DUMP = ['← [1] холодный бут', ''].join('\n')

test('cold read passes the negative group id after --', { options: { profile: 'p', dialog: '-1001234567' } }, async ($, on) => {
  const spawns: string[] = []
  on('process.spawn', async function* ($, e) {
    spawns.push(e.argv.join(' '))
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      // no daemon answers: the pane must take the cold CLI path
      yield { stream: 'stdout', text: ' ' }
      return { value: { code: 7, signal: null } }
    }
    if (/ serve /.test(argv)) await new Promise(() => {}) // the daemon is long-lived
    if (/ read /.test(argv)) {
      yield { stream: 'stdout', text: '← [1] группа холодным путём\n' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      // every wait hangs: the daemon probe loop settles SUSPENDED on a spawn
      await new Promise(() => {})
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
  await until(async () => (await mounted.find({ type: 'Text', text: /группа холодным путём/ })) !== undefined, 'group history drawn')
  await until(async () => (await mounted.find({ type: 'Text', text: /— p · -1001234567/ })) !== undefined, 'header shows the group target')

  const read = spawns.find(s => / read /.test(s)) ?? ''
  // the whole point (#293): `-100…` must sit AFTER the `--` separator, never
  // bare where Click reads it as an option (`No such option '-1'`)
  expect(read.endsWith('-- -1001234567'), `peer rides after --: ${read}`).toBe(true)
})

test('composer --help is sent as text; a send with no confirmation fails loudly', { options: { profile: 'p', dialog: '777', pollMs: 300 } }, async ($, on) => {
  const spawns: string[] = []
  on('process.spawn', async function* ($, e) {
    spawns.push(e.argv.join(' '))
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      yield { stream: 'stdout', text: ' ' }
      return { value: { code: 7, signal: null } }
    }
    if (/ serve /.test(argv)) await new Promise(() => {})
    if (/ listen /.test(argv)) await new Promise(() => {})
    if (/ read /.test(argv)) {
      yield { stream: 'stdout', text: DUMP }
      return { value: { code: 0, signal: null } }
    }
    if (/ send /.test(argv)) {
      // exactly what the real CLI did pre-fix: `send 777 --help` prints usage
      // to stdout and exits 0 — no `Error:` on stderr, no `sent.` line
      yield { stream: 'stdout', text: 'Usage: tg-messenger send [OPTIONS] DIALOG_ID [TEXT]...\n' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      await new Promise(() => {})
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
  await until(async () => (await mounted.find({ type: 'Text', text: /холодный бут/ })) !== undefined, 'history drawn')

  await mounted.input({ key: 'composer', text: '--help' })

  const send = spawns.find(s => / send /.test(s)) ?? ''
  expect(send.endsWith('send -- 777 --help'), `peer and text ride after --: ${send}`).toBe(true)
  // nothing was sent — the pane must SAY so instead of drawing the message as sent
  await until(async () => (await mounted.find({ type: 'Text', text: /no confirmation/ })) !== undefined, 'send failure drawn')
  expect(await mounted.find({ type: 'Text', text: /→ --help/ }), 'no optimistic sent line').toBeUndefined()
})

test('composer text starting with - rides after -- and is sent', { options: { profile: 'p', dialog: '777', pollMs: 300 } }, async ($, on) => {
  const spawns: string[] = []
  on('process.spawn', async function* ($, e) {
    spawns.push(e.argv.join(' '))
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      yield { stream: 'stdout', text: ' ' }
      return { value: { code: 7, signal: null } }
    }
    if (/ serve /.test(argv)) await new Promise(() => {})
    if (/ listen /.test(argv)) await new Promise(() => {})
    if (/ read /.test(argv)) {
      yield { stream: 'stdout', text: DUMP }
      return { value: { code: 0, signal: null } }
    }
    if (/ send /.test(argv)) {
      yield { stream: 'stdout', text: 'sent. [id=5]\n' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      await new Promise(() => {})
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
  await until(async () => (await mounted.find({ type: 'Text', text: /холодный бут/ })) !== undefined, 'history drawn')

  await mounted.input({ key: 'composer', text: '-x' })

  const send = spawns.find(s => / send /.test(s)) ?? ''
  expect(send.endsWith('send -- 777 -x'), `text rides after --: ${send}`).toBe(true)
  await until(async () => (await mounted.find({ type: 'Text', text: /→ -x/ })) !== undefined, 'sent line drawn')
})
