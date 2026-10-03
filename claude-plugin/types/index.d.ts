export type TgDialog = { id: string; title: string; unread: number }

export type TgMessage = {
  text: string
  out: boolean
  system?: boolean
  id?: number
  reactions?: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'tg-messenger': {
      messages: TgMessage[]
      draft: number
      pendingSend: string
      paletteFor: number
      view: 'chat' | 'dialogs'
      dialogList: TgDialog[] | null
      loading: 'history' | 'dialogs' | null
      spinner: number
    }
  }
}
