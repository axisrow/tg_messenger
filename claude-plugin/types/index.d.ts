export type TgMessage = {
  text: string
  out: boolean
  system?: boolean
  id?: number
  reactions?: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'tg-messenger': { messages: TgMessage[]; draft: number; pendingSend: string; paletteFor: number }
  }
}
