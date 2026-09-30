export type TgMessage = { text: string; out: boolean; system?: boolean }

declare module 'claude-code' {
  interface PluginState {
    'tg-messenger': { messages: TgMessage[]; draft: number; pendingSend: string }
  }
}
