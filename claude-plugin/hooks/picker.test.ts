import { expect, test } from 'claude-code/testing'

import { OPENED, OPEN_PLACEMENT, until } from './pane-harness'

// No `dialog` configured: the pane must boot the transport anyway and open
// the DIALOG PICKER — the pick then completes the setup (`switchDialog`
// flips readiness). It used to degrade to the dead "dialog not configured"
// scratchpad whose «диалоги» button was gated on readiness: unreachable.

const DIALOG_ROWS = '111\tТестовый Диалог (2 unread)\n222\tВторой\n'

test('pane without a configured dialog opens the picker', { options: { profile: 'p', pollMs: 300 } }, async ($, on) => {
  let sleeps = 0
  on('process.spawn', async function* ($, e) {
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      yield { stream: 'stdout', text: ' ' }
      return { value: { code: 7, signal: null } }
    }
    if (/ serve /.test(argv) || / listen /.test(argv)) await new Promise(() => {})
    if (/ dialogs /.test(argv)) {
      yield { stream: 'stdout', text: DIALOG_ROWS }
      return { value: { code: 0, signal: null } }
    }
    if (/ read /.test(argv)) {
      yield { stream: 'stdout', text: '← [1] холодный бут' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      if (++sleeps > 2) await new Promise(() => {}) // then hang: the realm settles SUSPENDED
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
  await until(
    async () => (await mounted.find({ type: 'Button', text: /Тестовый Диалог/ })) !== undefined,
    'picker rows drawn',
  )
})

test('picking a dialog completes the boot into a ready chat', { options: { profile: 'p', pollMs: 300 } }, async ($, on) => {
  let sleeps = 0
  on('process.spawn', async function* ($, e) {
    const argv = e.argv.join(' ')
    if (/^curl /.test(argv)) {
      yield { stream: 'stdout', text: ' ' }
      return { value: { code: 7, signal: null } }
    }
    if (/ serve /.test(argv) || / listen /.test(argv)) await new Promise(() => {})
    if (/ dialogs /.test(argv)) {
      yield { stream: 'stdout', text: DIALOG_ROWS }
      return { value: { code: 0, signal: null } }
    }
    if (/ read /.test(argv)) {
      yield { stream: 'stdout', text: '← [5] привет из истории' }
      return { value: { code: 0, signal: null } }
    }
    if (/^sleep /.test(argv)) {
      if (++sleeps > 2) await new Promise(() => {}) // then hang: the realm settles SUSPENDED
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
  await until(
    async () => (await mounted.find({ type: 'Button', text: /Тестовый Диалог/ })) !== undefined,
    'picker rows drawn',
  )
  await mounted.press({ key: '111' })
  await until(
    async () => (await mounted.find({ type: 'Text', text: /привет из истории/ })) !== undefined,
    'picked dialog history drawn',
  )
  await until(
    async () => (await mounted.find({ type: 'Text', text: /— p · 111/ })) !== undefined,
    'header shows profile · target — the pick flipped readiness',
  )
})
