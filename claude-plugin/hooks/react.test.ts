import { expect, test } from 'claude-code/testing'

import { OPENED, OPEN_PLACEMENT, until } from './pane-harness'

// Synthetic history (same shape as pane-render.test.ts): ids 101..105,
// incoming 101/103/105 carry the [+] trigger. No private text.
const DUMP = [
  '← [101] Неа',
  '→ [102] а где ты работаешь?',
  '← [103] Вот это пока самые главные новости 😅',
  '        А ну ещё гантель купил и начал качаться дома ) еле притащил ее',
  '→ [104] Выглядит тяжелой',
  '← [105] До 20кг',
  '',
].join('\n')

test('pressing a palette preset spawns the react CLI call', { options: { profile: 'p', dialog: '999' } }, async ($, on) => {
  const spawns: string[] = []
  on('process.spawn', async function* ($, e) {
    spawns.push(e.argv.join(' '))
    const argv = e.argv.join(' ')
    if (/ read /.test(argv)) {
      yield { stream: 'stdout', text: DUMP }
      return { value: { code: 0, signal: null } }
    }
    if (/ listen /.test(argv)) await new Promise(() => {})
    yield { stream: 'stdout', text: '' }
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

  // the pane boots from the /tg command (mount alone draws the static frame)
  await $.command.run({
    command: 'tg',
    args: '',
    origin: { kind: 'plugin', name: 'tg-messenger' },
    presentation: { isFullscreen: false, columns: 80 },
  })
  await until($, async () => (await mounted.find({ type: 'Text', text: /До 20кг/ })) !== undefined, 'history')

  // click [+] → the palette opens under that message; the trigger is keyed
  // per message ("react-<id>"), the first incoming row is 101
  await mounted.press({ key: 'react-101' })
  await until($, async () => (await mounted.find({ type: 'Button', text: '👍' })) !== undefined, 'palette')

  // pick 👍 → the pick must reach the transport: a `react` child with the
  // peer, the message id and the emoticon in argv
  await mounted.press({ key: '👍' })
  await until($, () => spawns.some(s => / react /.test(s)), 'react spawn')
  const reactArgs = spawns.find(s => / react /.test(s)) ?? ''
  expect(reactArgs, 'preset press spawns tg-messenger react').toContain(' react ')
  expect(reactArgs).toContain('999')
  expect(reactArgs).toContain('101')
  expect(reactArgs).toContain('👍')
})
