import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { TgMessage } from '../types'

/**
 * tg-messenger mod — hello-world pane with a composer (issue #244).
 *
 * `/tg` opens a bottom pane (the cc-arcade kind): `$.ui.open` asks for it,
 * a `ui.render` hook on `{component: 'Pane', requestId}` draws it with JSX.
 * The composer's Enter appends to the `messages` state — the text goes
 * nowhere. The Telegram bridge replaces the append with a real send.
 */

const PANE = 'tg'

const messages = atom(
  { plugin: 'tg-messenger', key: 'messages' } as const,
  [{ text: 'hello world — the bridge lands here', out: false }] as TgMessage[],
)

// MOCK: fake incoming messages until the Telegram bridge lands (issue #244)
const FAKE_INCOMING = [
  'привет! как продвигается мод?',
  'ты уже видел новую панель?',
  'скинь скриншот, интересно',
  'а когда настоящий телеграм подключим?',
  'лук чат-приложения 👌',
]

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    try {
      await $.command.register({
        name: 'tg',
        description: 'Telegram bridge: read recent messages and send replies',
      })
    } catch (error) {
      // ponytail: log-and-keep-going so a name collision never kills the session
      $.ui.log(`tg-messenger: /tg not registered: ${String(error)}`)
    }

    // hot reload: a pane left open keeps the previous drawing — re-seat it
    const panes = await $.ui.panes().catch(() => [])

    if (panes.some(p => p.id === PANE)) {
      void $.ui.open({ id: PANE, title: 'tg-messenger', closeOnEscape: true, focus: true })
    }

    // MOCK: an incoming every 8s, cycled; the SSE bridge replaces this timer
    let i = 0
    $.clock.every(8000, () => {
      void update($, messages, all =>
        [...all, { text: FAKE_INCOMING[i++ % FAKE_INCOMING.length]!, out: false }].slice(-100),
      )
    })

    return next(e)
  })

  on('command.run', { command: 'tg' }, async $ => {
    await $.ui.open({ id: PANE, title: 'tg-messenger', closeOnEscape: true, focus: true })

    return { text: 'tg-messenger: pane opened below the prompt.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const list = await read($, messages)
    // body height of the pane (docked panes are full-height): diff reads e.props.scroll.bodyRows
    const props = (e as { props?: { scroll?: { bodyRows?: number }; bodyColumns?: number } }).props
    const rows = props?.scroll?.bodyRows ?? e.viewport?.rows ?? 20
    const cols = (props?.bodyColumns ?? e.viewport?.columns ?? 80) - 2
    const shown = list.slice(-50)

    return (
      <Box flexDirection="column" flexGrow={1} height={rows + 2} gap={1} padding={1} paddingBottom={0}>
        <Box gap={2}>
          <Text bold>tg-messenger — hello world (v2)</Text>
          <Button onPress={() => $.ui.toast('pong')}>ping</Button>
          <Button role="dismiss" onPress={() => $.ui.close({ id: PANE })}>
            close
          </Button>
        </Box>
        <Box flexDirection="column" flexGrow={1} justifyContent="flex-end" overflow="hidden">
          {shown.map(m => (
            <Text dimColor={!m.out} wrap="wrap">
              {m.out ? `→ ${m.text}` : `← ${m.text}`}
            </Text>
          ))}
        </Box>
        <Box flexDirection="column" gap={0}>
          <Text dimColor>{'─'.repeat(Math.max(1, cols))}</Text>
          <Box height={1} overflow="hidden">
            <Input
            key="composer"
            placeholder="сообщение (уйдёт в никуда)"
            submitLabel="send"
            onSubmit={value => {
              const text = value.trim()

              if (text) {
                void update($, messages, all => [...all, { text, out: true }] as TgMessage[])
              }
            }}
            />
          </Box>
        </Box>
      </Box>
    )
  })
}
