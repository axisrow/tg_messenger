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

// `find` RESOLVES undefined when nothing matches — a resolution alone proves
// nothing: bounded poll until the element IS in the drawn tree, else fail.
// The runtime Engine has a real clock; the kit's `Engine` type just doesn't
// name it, hence the one structural cast here and nowhere else.
export async function until(
  engine: object,
  $el: () => unknown,
  what: string,
): Promise<void> {
  const { clock } = engine as { clock: { sleep: (ms: number) => Promise<void> } }
  for (let i = 0; i < 100; i++) {
    if (await $el()) return
    await clock.sleep(20)
  }
  throw new Error(`never drawn: ${what}`)
}
