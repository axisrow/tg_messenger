import { expect, test } from 'claude-code/testing'

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
      return { code: 0, signal: null }
    }
    if (/ listen /.test(argv)) await new Promise(() => {})
    yield { stream: 'stdout', text: '' }
    return { code: 0, signal: null }
  })

  const mounted = await $.ui.mount({
    plugin: 'tg-messenger',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'tg',
    props: {},
  })

  // settle: wait (bounded) until EVERY ground-truth text is in the drawn
  // tree — the boot chain is async, a single early find is a race
  const GROUND_TRUTH = [/А ну ещё гантель/, /Выглядит тяжелой/, /До 20кг/, /Неа/]
  let settled = false
  for (let i = 0; i < 100 && !settled; i++) {
    settled = await Promise.all(GROUND_TRUTH.map(rx => mounted.find({ type: 'Text', text: rx })))
      .then(() => true, () => false)
    if (!settled) await new Promise(r => setTimeout(r, 20))
  }
  // the RED suspects from the real pane: the multiline tail and whole
  // messages went missing on screen while the parsed state held all of them
  expect(settled, 'all ground-truth texts drawn').toBe(true)
})
