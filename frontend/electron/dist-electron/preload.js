"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
// ---------------------------------------------------------------------------
// Secure IPC bridge
// Exposes only whitelisted methods to the renderer process via window.electronAPI.
// All calls are proxied through ipcRenderer.invoke which is safe with
// contextIsolation enabled and nodeIntegration disabled.
// ---------------------------------------------------------------------------
electron_1.contextBridge.exposeInMainWorld('electronAPI', {
    // System
    getPlatform: () => electron_1.ipcRenderer.invoke('system:platform'),
    getVersion: () => electron_1.ipcRenderer.invoke('system:version'),
    // Dialogs
    showOpenDialog: (options) => electron_1.ipcRenderer.invoke('dialog:open', options),
    showSaveDialog: (options) => electron_1.ipcRenderer.invoke('dialog:save', options),
    showMessageBox: (options) => electron_1.ipcRenderer.invoke('dialog:message', options),
    // Window controls
    minimizeWindow: () => electron_1.ipcRenderer.invoke('window:minimize'),
    maximizeWindow: () => electron_1.ipcRenderer.invoke('window:maximize'),
    closeWindow: () => electron_1.ipcRenderer.invoke('window:close'),
    isMaximized: () => electron_1.ipcRenderer.invoke('window:isMaximized'),
    // Backend status
    getBackendStatus: () => electron_1.ipcRenderer.invoke('backend:status'),
});
//# sourceMappingURL=preload.js.map