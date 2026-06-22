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
    // Directory picker — returns selected folder path or null
    openDirectory: (defaultPath) => electron_1.ipcRenderer.invoke('dialog:open-directory', defaultPath),
    // Window controls
    minimizeWindow: () => electron_1.ipcRenderer.invoke('window:minimize'),
    maximizeWindow: () => electron_1.ipcRenderer.invoke('window:maximize'),
    closeWindow: () => electron_1.ipcRenderer.invoke('window:close'),
    isMaximized: () => electron_1.ipcRenderer.invoke('window:isMaximized'),
    // Backend status
    getBackendStatus: () => electron_1.ipcRenderer.invoke('backend:status'),
    // Shell
    showItemInFolder: (fullPath) => electron_1.ipcRenderer.invoke('shell:showItemInFolder', fullPath),
    openPath: (fullPath) => electron_1.ipcRenderer.invoke('shell:openPath', fullPath),
    // Backend lifecycle events
    onBackendDown: (callback) => {
        electron_1.ipcRenderer.on('backend:down', callback);
        return () => electron_1.ipcRenderer.removeListener('backend:down', callback);
    },
    // Native OS notification — returns true if shown, false if unsupported
    showNotification: (opts) => electron_1.ipcRenderer.invoke('notification:show', opts),
    // Notification click handler — fires when user clicks a notification toast
    onNotificationClick: (callback) => {
        const handler = (_event, sessionId) => callback(sessionId);
        electron_1.ipcRenderer.on('notification:click', handler);
        return () => electron_1.ipcRenderer.removeListener('notification:click', handler);
    },
    // Window focus state
    isWindowFocused: () => electron_1.ipcRenderer.invoke('window:isFocused'),
    // Elevated driver installation — runs winget with UAC elevation
    installDriverElevated: (opts) => electron_1.ipcRenderer.invoke('driver:installElevated', opts),
    // Open Microsoft Store page for Apple Mobile Device Support (winget fallback)
    openDriverStorePage: () => electron_1.ipcRenderer.invoke('driver:openStorePage'),
    // --- Tier 2 (WSL2 + usbipd-win) -----------------------------------------
    // Run an elevated command via UAC prompt
    runElevated: (opts) => electron_1.ipcRenderer.invoke('tier2:runElevated', opts),
    // Run an arbitrary command (usbipd, wsl.exe, etc.)
    runCommand: (opts) => electron_1.ipcRenderer.invoke('tier2:runCommand', opts),
    // Check hardware virtualization status
    checkVirtualization: () => electron_1.ipcRenderer.invoke('tier2:checkVirtualization'),
    // Restart the app (relaunch + exit)
    restartApp: () => electron_1.ipcRenderer.invoke('tier2:restart'),
});
//# sourceMappingURL=preload.js.map