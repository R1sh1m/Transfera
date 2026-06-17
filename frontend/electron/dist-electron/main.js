"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
const child_process_1 = require("child_process");
const path_1 = __importDefault(require("path"));
const net_1 = __importDefault(require("net"));
// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const BACKEND_PORT = 47821;
const VITE_DEV_SERVER = 'http://127.0.0.1:5173';
const isDev = !electron_1.app.isPackaged;
let mainWindow = null;
let backendProcess = null;
// ---------------------------------------------------------------------------
// Backend process management
// ---------------------------------------------------------------------------
function getBackendCommand() {
    if (isDev) {
        // Development: run from project root
        const backendDir = path_1.default.resolve(__dirname, '..', '..', 'backend');
        return {
            cmd: 'python',
            args: ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
        };
    }
    // Production: bundled backend
    const backendDir = path_1.default.join(process.resourcesPath, 'backend');
    return {
        cmd: path_1.default.join(backendDir, 'venv', 'Scripts', 'python.exe'),
        args: ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
    };
}
function isPortAvailable(port) {
    return new Promise((resolve) => {
        const server = net_1.default.createServer();
        server.once('error', () => resolve(false));
        server.once('listening', () => {
            server.close(() => resolve(true));
        });
        server.listen(port, '127.0.0.1');
    });
}
async function waitForBackend(timeout = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
        try {
            const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`);
            if (res.ok)
                return true;
        }
        catch {
            // not ready yet
        }
        await new Promise((r) => setTimeout(r, 500));
    }
    return false;
}
async function startBackend() {
    const portAvailable = await isPortAvailable(BACKEND_PORT);
    if (!portAvailable) {
        console.log(`Port ${BACKEND_PORT} already in use, backend may already be running.`);
        return;
    }
    const { cmd, args } = getBackendCommand();
    console.log(`Starting backend: ${cmd} ${args.join(' ')}`);
    backendProcess = (0, child_process_1.spawn)(cmd, args, {
        cwd: isDev ? path_1.default.resolve(__dirname, '..', '..', 'backend') : process.resourcesPath,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: false,
    });
    backendProcess.stdout?.on('data', (data) => {
        console.log(`[backend] ${data.toString().trim()}`);
    });
    backendProcess.stderr?.on('data', (data) => {
        console.error(`[backend] ${data.toString().trim()}`);
    });
    backendProcess.on('error', (err) => {
        console.error('Failed to start backend:', err);
    });
    backendProcess.on('exit', (code, signal) => {
        console.log(`Backend exited with code ${code}, signal ${signal}`);
        backendProcess = null;
    });
    const ready = await waitForBackend();
    if (ready) {
        console.log('Backend is ready.');
    }
    else {
        console.error('Backend failed to start within timeout.');
    }
}
function killBackend() {
    if (backendProcess && !backendProcess.killed) {
        console.log('Stopping backend process...');
        backendProcess.kill('SIGTERM');
        // Force kill after 5 seconds if still alive
        setTimeout(() => {
            if (backendProcess && !backendProcess.killed) {
                console.log('Force killing backend process...');
                backendProcess.kill('SIGKILL');
            }
        }, 5000);
    }
}
// ---------------------------------------------------------------------------
// Window creation
// ---------------------------------------------------------------------------
function createWindow() {
    mainWindow = new electron_1.BrowserWindow({
        width: 1200,
        height: 800,
        minWidth: 800,
        minHeight: 600,
        title: 'MediaVault',
        titleBarStyle: 'hidden',
        backgroundColor: '#ffffff',
        webPreferences: {
            preload: path_1.default.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: false,
        },
        show: false,
    });
    // Load the app
    if (isDev) {
        mainWindow.loadURL(VITE_DEV_SERVER);
    }
    else {
        mainWindow.loadFile(path_1.default.join(__dirname, '..', 'dist', 'index.html'));
    }
    // Show when ready
    mainWindow.once('ready-to-show', () => {
        mainWindow?.show();
    });
    // Open DevTools with F12
    mainWindow.webContents.on('before-input-event', (_event, input) => {
        if (input.key === 'F12' && input.type === 'keyDown') {
            mainWindow?.webContents.toggleDevTools();
        }
    });
    // Open external links in system browser
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        electron_1.shell.openExternal(url);
        return { action: 'deny' };
    });
    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}
// ---------------------------------------------------------------------------
// IPC Handlers
// ---------------------------------------------------------------------------
function registerIPC() {
    // Dialogs
    electron_1.ipcMain.handle('dialog:open', async (_event, options) => {
        if (!mainWindow)
            return { canceled: true, filePaths: [] };
        return electron_1.dialog.showOpenDialog(mainWindow, {
            title: options.title,
            defaultPath: options.defaultPath,
            properties: (options.properties ?? []),
        });
    });
    electron_1.ipcMain.handle('dialog:save', async (_event, options) => {
        if (!mainWindow)
            return { canceled: true };
        return electron_1.dialog.showSaveDialog(mainWindow, {
            title: options.title,
            defaultPath: options.defaultPath,
            filters: options.filters,
        });
    });
    electron_1.ipcMain.handle('dialog:message', async (_event, options) => {
        if (!mainWindow)
            return { response: 0, checkboxChecked: false };
        return electron_1.dialog.showMessageBox(mainWindow, {
            type: options.type,
            title: options.title,
            message: options.message,
            detail: options.detail,
            buttons: options.buttons,
        });
    });
    // Window controls
    electron_1.ipcMain.handle('window:minimize', () => mainWindow?.minimize());
    electron_1.ipcMain.handle('window:maximize', () => {
        if (mainWindow?.isMaximized()) {
            mainWindow.unmaximize();
        }
        else {
            mainWindow?.maximize();
        }
    });
    electron_1.ipcMain.handle('window:close', () => mainWindow?.close());
    electron_1.ipcMain.handle('window:isMaximized', () => mainWindow?.isMaximized() ?? false);
    // System info
    electron_1.ipcMain.handle('system:platform', () => process.platform);
    electron_1.ipcMain.handle('system:version', () => electron_1.app.getVersion());
    // Backend status
    electron_1.ipcMain.handle('backend:status', async () => {
        try {
            const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`);
            if (res.ok) {
                return { running: true, port: BACKEND_PORT };
            }
        }
        catch {
            // not running
        }
        return { running: false, port: BACKEND_PORT };
    });
}
// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------
electron_1.app.whenReady().then(async () => {
    registerIPC();
    await startBackend();
    createWindow();
    electron_1.app.on('activate', () => {
        if (electron_1.BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});
electron_1.app.on('window-all-closed', () => {
    killBackend();
    if (process.platform !== 'darwin') {
        electron_1.app.quit();
    }
});
electron_1.app.on('before-quit', () => {
    killBackend();
});
//# sourceMappingURL=main.js.map