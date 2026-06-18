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
})
