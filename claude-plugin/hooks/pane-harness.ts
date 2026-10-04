// The kit draws a mounted Pane but implements no pane chrome: `ui.open` has
// no implementation in the test harness, so the boot chain (`/tg` → open →
// resolve → history) dies at its first line. The test answers `ui.open`
// itself with the result the engine would return ...
export const OPENED = { value: { isPlaced: true as const } }

// ... and mounts the Pane with the placement fact the engine would stamp.
export const OPEN_PLACEMENT = {
  title: 'tg-messenger',
  isFocused: true,
  bodyColumns: 80,
  placement: 'dock' as 'dock' | 'inline',
  scroll: { offset: 0, bodyRows: 24 },
  view: {},
}

// The kit's `$` carries NO clock (verified: the property is absent, not merely
// untyped), so tests pace time with a plain runtime timer, present in every
// JS engine. The tsconfig lib excludes timer types — hence the cast, kept in
// exactly this one place.
export const sleep = (ms: number): Promise<void> =>
  new Promise(done => {
    ;(globalThis as unknown as { setTimeout: (fn: () => void, ms: number) => void }).setTimeout(done, ms)
  })

// `find` RESOLVES undefined when nothing matches — a resolution alone proves
// nothing: bounded poll until the element IS in the drawn tree, else fail.
export async function until($el: () => unknown, what: string): Promise<void> {
  for (let i = 0; i < 100; i++) {
    if (await $el()) return
    await sleep(20)
  }
  throw new Error(`never drawn: ${what}`)
}
