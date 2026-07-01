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

  // Directory picker — returns selected folder path or null
  openDirectory: (defaultPath?: string) => Promise<string | null>

  // Backend status
  getBackendStatus: () => Promise<{ running: boolean; starting: boolean; port: number }>

  // Shell
  showItemInFolder: (fullPath: string) => Promise<void>
  openPath: (fullPath: string) => Promise<string>

  // Backend lifecycle events
  onBackendDown: (callback: () => void) => () => void
  onBackendStarting: (callback: () => void) => () => void
  onBackendReady: (callback: () => void) => () => void

  // Native OS notification
  showNotification: (opts: { title: string; body: string; sessionId: number }) => Promise<boolean>
  onNotificationClick: (callback: (sessionId: number) => void) => () => void

  // Window focus state
  isWindowFocused: () => Promise<boolean>

  // Elevated driver installation — triggers UAC prompt
  installDriverElevated: (opts: {
    executable: string
    args: string[]
  }) => Promise<{ success: boolean; exitCode: number | null; error?: string }>

  // Open Microsoft Store page for Apple Mobile Device Support (winget fallback)
  openDriverStorePage: () => Promise<{ opened: boolean }>

  // --- Tier 2 (WSL2 + usbipd-win) -----------------------------------------
  runElevated: (opts: {
    executable: string
    args: string[]
    description: string
  }) => Promise<{ success: boolean; exitCode: number | null; error?: string }>

  runCommand: (opts: {
    executable: string
    args: string[]
    elevated?: boolean
    timeoutMs?: number
  }) => Promise<{ success: boolean; stdout: string; stderr: string; exitCode: number | null }>

  checkVirtualization: () => Promise<{ available: boolean; details: string }>

  restartApp: () => Promise<void>

  // Open URL in default browser
  openExternal: (url: string) => Promise<void>

  // Tray progress — updates Windows taskbar progress overlay
  setTrayProgress: (value: number | null) => Promise<void>

  // Removable drive detection — fires when a new USB drive is connected
  onNewRemovableDrive: (callback: (data: { driveLetter: string; volumeName: string | null }) => void) => () => void

  // Setup / Python installation
  checkPythonInstalled: () => Promise<{ installed: boolean }>
  installPython: () => Promise<void>
  onInstallProgress: (callback: (data: { step: string; percent: number; error?: string }) => void) => () => void
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI
  }
}

export {}
