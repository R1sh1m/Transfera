import { contextBridge, ipcRenderer } from 'electron'

// ---------------------------------------------------------------------------
// Secure IPC bridge
// Exposes only whitelisted methods to the renderer process via window.electronAPI.
// All calls are proxied through ipcRenderer.invoke which is safe with
// contextIsolation enabled and nodeIntegration disabled.
// ---------------------------------------------------------------------------
contextBridge.exposeInMainWorld('electronAPI', {
  // System
  getPlatform: () => ipcRenderer.invoke('system:platform'),
  getVersion: () => ipcRenderer.invoke('system:version'),

  // Dialogs
  showOpenDialog: (options: {
    title?: string
    defaultPath?: string
    properties?: string[]
  }) => ipcRenderer.invoke('dialog:open', options),

  showSaveDialog: (options: {
    title?: string
    defaultPath?: string
    filters?: { name: string; extensions: string[] }[]
  }) => ipcRenderer.invoke('dialog:save', options),

  showMessageBox: (options: {
    type?: string
    title?: string
    message: string
    detail?: string
    buttons?: string[]
  }) => ipcRenderer.invoke('dialog:message', options),

  // Directory picker — returns selected folder path or null
  openDirectory: (defaultPath?: string) =>
    ipcRenderer.invoke('dialog:open-directory', defaultPath),

  // Window controls
  minimizeWindow: () => ipcRenderer.invoke('window:minimize'),
  maximizeWindow: () => ipcRenderer.invoke('window:maximize'),
  closeWindow: () => ipcRenderer.invoke('window:close'),
  isMaximized: () => ipcRenderer.invoke('window:isMaximized'),

  // Backend status
  getBackendStatus: () => ipcRenderer.invoke('backend:status'),

  // Shell
  showItemInFolder: (fullPath: string) =>
    ipcRenderer.invoke('shell:showItemInFolder', fullPath),
  openPath: (fullPath: string) =>
    ipcRenderer.invoke('shell:openPath', fullPath),

  // Backend lifecycle events
  onBackendDown: (callback: () => void) => {
    ipcRenderer.on('backend:down', callback)
    return () => ipcRenderer.removeListener('backend:down', callback)
  },

  // Native OS notification — returns true if shown, false if unsupported
  showNotification: (opts: { title: string; body: string; sessionId: number }) =>
    ipcRenderer.invoke('notification:show', opts),

  // Notification click handler — fires when user clicks a notification toast
  onNotificationClick: (callback: (sessionId: number) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, sessionId: number) => callback(sessionId)
    ipcRenderer.on('notification:click', handler)
    return () => ipcRenderer.removeListener('notification:click', handler)
  },

  // Window focus state
  isWindowFocused: () => ipcRenderer.invoke('window:isFocused'),

  // Elevated driver installation — runs winget with UAC elevation
  installDriverElevated: (opts: { executable: string; args: string[] }) =>
    ipcRenderer.invoke('driver:installElevated', opts),

  // Open Microsoft Store page for Apple Mobile Device Support (winget fallback)
  openDriverStorePage: () => ipcRenderer.invoke('driver:openStorePage'),

  // --- Tier 2 (WSL2 + usbipd-win) -----------------------------------------
  // Run an elevated command via UAC prompt
  runElevated: (opts: { executable: string; args: string[]; description: string }) =>
    ipcRenderer.invoke('tier2:runElevated', opts),

  // Run an arbitrary command (usbipd, wsl.exe, etc.)
  runCommand: (opts: {
    executable: string
    args: string[]
    elevated?: boolean
    timeoutMs?: number
  }) => ipcRenderer.invoke('tier2:runCommand', opts),

  // Check hardware virtualization status
  checkVirtualization: () => ipcRenderer.invoke('tier2:checkVirtualization'),

  // Restart the app (relaunch + exit)
  restartApp: () => ipcRenderer.invoke('tier2:restart'),
})
