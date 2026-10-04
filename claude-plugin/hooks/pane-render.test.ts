import { expect, test } from 'claude-code/testing'

import { OPENED, OPEN_PLACEMENT, until } from './pane-harness'

// Synthetic history in the exact shape the CLI prints it: arrows, a multiline
// message hang-indented by 8 spaces ('← [103] ' for a 3-digit id), an emoji.
// No private text — same structure as the real dialog that rendered torn.
const DUMP = [
  '← [101] Неа',
  '→ [102] а где ты работаешь?',
  '← [103] Вот это пока самые главные новости 😅',
  '        А ну ещё гантель купил и начал качаться дома ) еле притащил ее',
  '→ [104] Выглядит тяжелой',
  '← [105] До 20кг',
  '',
].join('\n')

test('pane renders every history message and every multiline tail', { options: { profile: 'p', dialog: '999' } }, async ($, on) => {
  // stand where the engine's real spawn would be: `read` yields the dump,
  // `listen` hangs for its life like a real bridge, everything else is empty
  on('process.spawn', async function* ($, e) {
    const argv = e.argv.join(' ')
    if (/ read /.test(argv)) {
      yield { stream: 'stdout', text: DUMP }
      return { value: { code: 0, signal: null } }
    }
    if (/ listen /.test(argv)) await new Promise(() => {})
    if (/^sleep /.test(argv)) {
      // every wait hangs: the poll/probe loops must settle SUSPENDED on a spawn
      await new Promise(() => {})
      yield { stream: 'stdout', text: '.' }
      return { value: { code: 0, signal: null } }
    }
    // non-empty: the kit rejects empty-text yields, and the boot's daemon
    // probe (curl) lands here — a blank body reads as "no daemon", cold path
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

  // the pane boots from the /tg command (mount alone draws the static frame)
  await $.command.run({
    command: 'tg',
    args: '',
    origin: { kind: 'plugin', name: 'tg-messenger' },
    presentation: { isFullscreen: false, columns: 80 },
  })

  // the RED suspects from the real pane: the multiline tail and whole
  // messages went missing on screen while the parsed state held all of them
  const GROUND_TRUTH = [/А ну ещё гантель/, /Выглядит тяжелой/, /До 20кг/, /Неа/]
  for (const rx of GROUND_TRUTH)
    await until(
      async () => (await mounted.find({ type: 'Text', text: rx })) !== undefined,
      String(rx),
    )
  expect(GROUND_TRUTH.length, 'all ground-truth texts drawn').toBe(4)
})
