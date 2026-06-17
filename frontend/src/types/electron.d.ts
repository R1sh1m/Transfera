export interface ElectronAPI {
  // System
  getPlatform: () => Promise<string>
  getVersion: () => Promise<string>

  // Dialogs
  showOpenDialog: (options: {
    title?: string
    defaultPath?: string
    properties?: string[]
  }) => Promise<{ canceled: boolean; filePaths: string[] }>

  showSaveDialog: (options: {
    title?: string
    defaultPath?: string
    filters?: { name: string; extensions: string[] }[]
  }) => Promise<{ canceled: boolean; filePath?: string }>

  showMessageBox: (options: {
    type?: string
    title?: string
    message: string
    detail?: string
    buttons?: string[]
  }) => Promise<{ response: number; checkboxChecked: boolean }>

  // Window
  minimizeWindow: () => Promise<void>
  maximizeWindow: () => Promise<void>
  closeWindow: () => Promise<void>
  isMaximized: () => Promise<boolean>

  // Backend status
  getBackendStatus: () => Promise<{ running: boolean; port: number }>
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI
  }
}

export {}
