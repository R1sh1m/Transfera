"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const http_1 = __importDefault(require("http"));
const electron_1 = require("electron");
const child_process_1 = require("child_process");
const path_1 = __importDefault(require("path"));
const fs_1 = __importDefault(require("fs"));
const net_1 = __importDefault(require("net"));
// ---------------------------------------------------------------------------
// App identity — must be set before app.whenReady() so Windows groups the
// taskbar entry under the correct AppUserModelID.
// ---------------------------------------------------------------------------
electron_1.app.setAppUserModelId('com.transfera.app');
// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const BACKEND_PORT = 47821;
const VITE_DEV_SERVER = 'http://127.0.0.1:5173';
const isDev = !electron_1.app.isPackaged;
const GRACEFUL_SHUTDOWN_WAIT = 4000;
let mainWindow = null;
let backendProcess = null;
let isQuitting = false;
let externalBackend = false;
// ---------------------------------------------------------------------------
// Backend process management
// ---------------------------------------------------------------------------
function getBackendCommand() {
    if (isDev) {
        const projectRoot = path_1.default.resolve(__dirname, '..', '..', '..');
        const venvPython = path_1.default.join(projectRoot, '.venv', 'Scripts', 'python.exe');
        return {
            cmd: venvPython,
            args: ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
        };
    }
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
// -- Graceful shutdown -----------------------------------------------------------------
/** POST to /api/shutdown and let the backend clean up its own state. */
async function tryGracefulShutdown() {
    try {
        const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/shutdown`, {
            method: 'POST',
            signal: AbortSignal.timeout(5000),
        });
        return res.ok;
    }
    catch {
        return false;
    }
}
/** Windows process-tree termination via taskkill. */
async function taskkillProcessTree(pid) {
    await new Promise((resolve) => {
        (0, child_process_1.execFile)('taskkill', ['/pid', String(pid), '/T', '/F'], { timeout: 5000 }, () => resolve());
    });
}
/** Wait for a child process to exit, with a timeout. */
function waitForProcessExit(proc, timeout) {
    return new Promise((resolve) => {
        if (proc.killed) {
            resolve();
            return;
        }
        const timer = setTimeout(resolve, timeout);
        const done = () => { clearTimeout(timer); resolve(); };
        proc.on('exit', done);
        proc.on('error', done);
    });
}
/**
 * Full backend shutdown sequence:
 *   1. POST /api/shutdown (graceful, lets uvicorn drain in-flight work)
 *   2. Wait up to GRACEFUL_SHUTDOWN_WAIT ms for the process to exit on its own
 *   3. If still alive, force-kill the entire process tree (taskkill /T /F on Windows)
 *   4. Last-resort kill
 */
async function shutdownBackend() {
    if (externalBackend) {
        console.log('[lifecycle] Backend was started externally — leaving it running.');
        backendProcess = null;
        return;
    }
    if (!backendProcess || backendProcess.killed) {
        backendProcess = null;
        return;
    }
    const proc = backendProcess;
    console.log('[lifecycle] Shutting down backend (PID %s)...', proc.pid);
    const gracefulOk = await tryGracefulShutdown();
    if (gracefulOk) {
        console.log('[lifecycle] Graceful shutdown signal sent, waiting for exit...');
        await waitForProcessExit(proc, GRACEFUL_SHUTDOWN_WAIT);
    }
    if (!proc.killed) {
        console.log('[lifecycle] Force-killing backend process tree...');
        if (process.platform === 'win32' && proc.pid) {
            await taskkillProcessTree(proc.pid);
            await waitForProcessExit(proc, 3000);
        }
        if (!proc.killed) {
            proc.kill();
        }
    }
    backendProcess = null;
    console.log('[lifecycle] Backend shutdown complete.');
}
// -- Orphan cleanup --------------------------------------------------------------------
/**
 * Before starting the backend, check whether port BACKEND_PORT is already in
 * use by an orphaned process from a previous crash (or a stale `run.py` run).
 * On Windows, use Get-NetTCPConnection to find the owning PID, then
 * taskkill it with process-tree semantics.  Without this, a crash that left
 * a Python/uvicorn process running would cause EADDRINUSE on the next launch.
 */
async function cleanupOrphanedBackend() {
    const portAvailable = await isPortAvailable(BACKEND_PORT);
    if (portAvailable)
        return;
    console.log(`[lifecycle] Port ${BACKEND_PORT} is already in use — attempting to clean up orphaned process...`);
    if (process.platform === 'win32') {
        try {
            await new Promise((resolve, reject) => {
                (0, child_process_1.execFile)('powershell', [
                    '-NoProfile', '-NonInteractive', '-Command',
                    `Get-NetTCPConnection -LocalPort ${BACKEND_PORT} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | ForEach-Object { taskkill /pid $_ /T /F }`,
                ], { timeout: 10000 }, (err) => (err ? reject(err) : resolve()));
            });
        }
        catch {
            // orphan may already be gone — that's fine
        }
        // Give taskkill a moment
        await new Promise((r) => setTimeout(r, 1500));
    }
    const nowAvailable = await isPortAvailable(BACKEND_PORT);
    if (nowAvailable) {
        console.log('[lifecycle] Orphaned backend cleaned up successfully.');
    }
    else {
        console.warn(`[lifecycle] Port ${BACKEND_PORT} is still occupied — will attempt to start anyway.`);
    }
}
// -- Startup ---------------------------------------------------------------------------
/** Lightweight probe — does the backend respond on /api/health? */
function probeBackend() {
    return new Promise((resolve) => {
        const req = http_1.default.get(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { timeout: 1000 }, (res) => {
            resolve(res.statusCode === 200);
            res.resume();
        });
        req.on('error', () => resolve(false));
        req.on('timeout', () => { req.destroy(); resolve(false); });
    });
}
async function startBackend() {
    await cleanupOrphanedBackend();
    const { cmd, args } = getBackendCommand();
    console.log(`[lifecycle] Starting backend: ${cmd} ${args.join(' ')}`);
    const spawnTime = Date.now();
    backendProcess = (0, child_process_1.spawn)(cmd, args, {
        cwd: isDev ? path_1.default.resolve(__dirname, '..', '..', '..') : process.resourcesPath,
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
        console.error('[lifecycle] Failed to start backend:', err);
    });
    backendProcess.on('exit', (code, signal) => {
        const isEarlyExit = Date.now() - spawnTime < 5000;
        if (code === 1 && isEarlyExit) {
            // Port already in use — run.py external backend is running, this is expected
            console.log('[lifecycle] Backend subprocess could not bind (port in use) — using external backend process');
            backendProcess = null;
            externalBackend = true; // treat as external from this point forward
            return;
        }
        console.log(`[lifecycle] Backend exited with code ${code}, signal ${signal}`);
        backendProcess = null;
        if (!isQuitting && mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send('backend:down');
        }
    });
    const ready = await waitForBackend();
    if (ready) {
        console.log('[lifecycle] Backend is ready.');
    }
    else {
        console.error('[lifecycle] Backend failed to start within timeout.');
        mainWindow?.webContents.send('backend:down');
    }
}
// ---------------------------------------------------------------------------
// Icon resolution — platform-aware, works in both dev and packaged builds.
// ---------------------------------------------------------------------------
function resolveIconPath() {
    const ext = process.platform === 'win32' ? 'icon.ico' : 'icon.png';
    const candidates = [];
    if (electron_1.app.isPackaged) {
        candidates.push(path_1.default.join(process.resourcesPath, 'build', ext));
        candidates.push(path_1.default.join(process.resourcesPath, ext));
    }
    else {
        candidates.push(path_1.default.join(__dirname, '..', 'build', ext));
        candidates.push(path_1.default.join(__dirname, '..', '..', 'build', ext));
    }
    for (const candidate of candidates) {
        if (fs_1.default.existsSync(candidate))
            return candidate;
    }
    console.warn('[icon] Could not resolve icon, tried:', candidates);
    return path_1.default.join(__dirname, '..', 'build', ext);
}
// ---------------------------------------------------------------------------
// Window creation
// ---------------------------------------------------------------------------
function createWindow() {
    const iconPath = resolveIconPath();
    console.log(`[icon] Using icon: ${iconPath}`);
    mainWindow = new electron_1.BrowserWindow({
        width: 1200,
        height: 800,
        minWidth: 800,
        minHeight: 600,
        title: 'Transfera',
        icon: iconPath,
        frame: false,
        backgroundColor: '#0f0f0f',
        webPreferences: {
            preload: path_1.default.join(__dirname, 'preload.cjs'),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: false,
        },
        show: false,
    });
    if (isDev) {
        mainWindow.loadURL(VITE_DEV_SERVER);
    }
    else {
        mainWindow.loadFile(path_1.default.join(__dirname, '..', 'dist', 'index.html'));
    }
    mainWindow.once('ready-to-show', () => {
        mainWindow?.show();
    });
    mainWindow.webContents.on('before-input-event', (_event, input) => {
        if (input.key === 'F12' && input.type === 'keyDown') {
            mainWindow?.webContents.toggleDevTools();
        }
    });
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
    electron_1.ipcMain.handle('dialog:open-directory', async (_event, defaultPath) => {
        console.log('[IPC] dialog:open-directory invoked', defaultPath ?? '');
        if (!mainWindow)
            return null;
        const result = await electron_1.dialog.showOpenDialog(mainWindow, {
            properties: ['openDirectory'],
            defaultPath,
        });
        return result.canceled ? null : result.filePaths[0];
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
    // Close window — triggers the normal Electron quit lifecycle
    // (window-all-closed → app.quit() → before-quit → backend cleanup → exit).
    electron_1.ipcMain.handle('window:close', () => mainWindow?.close());
    electron_1.ipcMain.handle('window:isMaximized', () => mainWindow?.isMaximized() ?? false);
    electron_1.ipcMain.handle('shell:showItemInFolder', (_event, fullPath) => {
        electron_1.shell.showItemInFolder(fullPath);
    });
    electron_1.ipcMain.handle('shell:openPath', (_event, fullPath) => {
        electron_1.shell.openPath(fullPath);
    });
    electron_1.ipcMain.handle('system:platform', () => process.platform);
    electron_1.ipcMain.handle('system:version', () => electron_1.app.getVersion());
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
    electron_1.ipcMain.handle('notification:show', (_event, opts) => {
        if (!electron_1.Notification.isSupported()) {
            console.warn('[notification] OS notifications not supported');
            return false;
        }
        const iconPath = resolveIconPath();
        const notification = new electron_1.Notification({
            title: opts.title,
            body: opts.body,
            icon: iconPath,
            silent: false,
        });
        notification.on('click', () => {
            if (mainWindow) {
                if (mainWindow.isMinimized())
                    mainWindow.restore();
                mainWindow.focus();
            }
            mainWindow?.webContents.send('notification:click', opts.sessionId);
        });
        notification.show();
        return true;
    });
    electron_1.ipcMain.handle('window:isFocused', () => mainWindow?.isFocused() ?? false);
    // -- Elevated driver installation ---------------------------------------------------
    electron_1.ipcMain.handle('driver:installElevated', async (_event, opts) => {
        const argsString = opts.args.map((a) => `'${a.replace(/'/g, "''")}'`).join(',');
        const psCommand = [
            `$p = Start-Process -FilePath '${opts.executable}'`,
            `-ArgumentList @(${argsString})`,
            '-Verb RunAs -Wait -PassThru',
            '$p.ExitCode',
        ].join(' ');
        return new Promise((resolve) => {
            (0, child_process_1.execFile)('powershell', ['-NoProfile', '-NonInteractive', '-Command', psCommand], { timeout: 300_000 }, (error, stdout, stderr) => {
                if (error) {
                    if (error.killed) {
                        resolve({ success: false, exitCode: null, error: 'Installation timed out' });
                    }
                    else {
                        const exitCode = error.code;
                        if (exitCode === '1602' || exitCode === '1223') {
                            resolve({ success: false, exitCode: null, error: 'Installation cancelled' });
                        }
                        else {
                            resolve({
                                success: false,
                                exitCode: exitCode != null ? Number(exitCode) : null,
                                error: stderr?.trim() || error.message,
                            });
                        }
                    }
                    return;
                }
                const exitCode = parseInt(stdout?.trim() || '0', 10);
                resolve({ success: exitCode === 0, exitCode });
            });
        });
    });
    electron_1.ipcMain.handle('driver:openStorePage', async () => {
        const storeUri = 'ms-windows-store://pdp/?productid=9NMPJ99VJBWV';
        const storeWebUrl = 'https://apps.microsoft.com/detail/apple-devices/9NMPJ99VJBWV';
        try {
            await electron_1.shell.openExternal(storeUri);
            return { opened: true };
        }
        catch {
            try {
                await electron_1.shell.openExternal(storeWebUrl);
                return { opened: true };
            }
            catch {
                return { opened: false };
            }
        }
    });
    // -- Tier 2 (WSL2 + usbipd-win) IPC handlers ----------------------------------------
    electron_1.ipcMain.handle('tier2:runElevated', async (_event, opts) => {
        const argsString = opts.args.map((a) => `'${a.replace(/'/g, "''")}'`).join(',');
        const psCommand = [
            `$p = Start-Process -FilePath '${opts.executable}'`,
            `-ArgumentList @(${argsString})`,
            '-Verb RunAs -Wait -PassThru',
            '$p.ExitCode',
        ].join(' ');
        return new Promise((resolve) => {
            (0, child_process_1.execFile)('powershell', ['-NoProfile', '-NonInteractive', '-Command', psCommand], { timeout: 300_000 }, (error, stdout, stderr) => {
                if (error) {
                    const exitCode = error.code;
                    if (exitCode === '1602' || exitCode === '1223') {
                        resolve({ success: false, exitCode: null, error: 'User cancelled elevation prompt' });
                    }
                    else if (error.killed) {
                        resolve({ success: false, exitCode: null, error: 'Command timed out' });
                    }
                    else {
                        resolve({
                            success: false,
                            exitCode: exitCode != null ? Number(exitCode) : null,
                            error: stderr?.trim() || error.message,
                        });
                    }
                    return;
                }
                const exitCode = parseInt(stdout?.trim() || '0', 10);
                resolve({ success: exitCode === 0, exitCode });
            });
        });
    });
    electron_1.ipcMain.handle('tier2:runCommand', async (_event, opts) => {
        const timeout = opts.timeoutMs ?? 120_000;
        if (opts.elevated) {
            const argsString = opts.args.map((a) => `'${a.replace(/'/g, "''")}'`).join(',');
            const psCommand = [
                `$p = Start-Process -FilePath '${opts.executable}'`,
                `-ArgumentList @(${argsString})`,
                '-Verb RunAs -Wait -PassThru',
                `$p.ExitCode`,
            ].join(' ');
            return new Promise((resolve) => {
                (0, child_process_1.execFile)('powershell', ['-NoProfile', '-NonInteractive', '-Command', psCommand], { timeout }, (error, stdout, stderr) => {
                    if (error) {
                        const exitCode = error.code;
                        if (exitCode === '1602' || exitCode === '1223') {
                            resolve({ success: false, stdout: '', stderr: 'User cancelled elevation', exitCode: null });
                        }
                        else {
                            resolve({
                                success: false,
                                stdout: stdout?.trim() || '',
                                stderr: stderr?.trim() || error.message,
                                exitCode: exitCode != null ? Number(exitCode) : null,
                            });
                        }
                        return;
                    }
                    const exitCode = parseInt(stdout?.trim() || '0', 10);
                    resolve({ success: exitCode === 0, stdout: stdout?.trim() || '', stderr: stderr?.trim() || '', exitCode });
                });
            });
        }
        return new Promise((resolve) => {
            (0, child_process_1.execFile)(opts.executable, opts.args, { timeout }, (error, stdout, stderr) => {
                if (error) {
                    const exitCode = error.code;
                    resolve({
                        success: false,
                        stdout: stdout?.trim() || '',
                        stderr: stderr?.trim() || error.message,
                        exitCode: typeof exitCode === 'number' ? exitCode : null,
                    });
                    return;
                }
                const exitCode = parseInt(stdout?.trim() || '0', 10);
                resolve({ success: exitCode === 0, stdout: stdout?.trim() || '', stderr: stderr?.trim() || '', exitCode });
            });
        });
    });
    electron_1.ipcMain.handle('tier2:checkVirtualization', async () => {
        return new Promise((resolve) => {
            (0, child_process_1.execFile)('powershell', ['-NoProfile', '-NonInteractive', '-Command', 'Get-ComputerInfo | Select-Object -ExpandProperty HyperVRequirementVirtualizationFirmwareEnabled'], { timeout: 30_000 }, (error, stdout, stderr) => {
                if (error) {
                    resolve({ available: false, details: stderr?.trim() || error.message });
                    return;
                }
                const val = stdout?.trim().toLowerCase();
                if (val === 'true') {
                    resolve({ available: true, details: 'Hardware virtualization is enabled' });
                }
                else if (val === 'false') {
                    resolve({ available: false, details: 'Hardware virtualization is disabled in BIOS. Enable VT-x/AMD-V in your firmware settings to use Tier 2 (WSL2).' });
                }
                else {
                    resolve({ available: false, details: `Unexpected output: ${stdout?.trim() || '(empty)'}` });
                }
            });
        });
    });
    // Relaunch the app — shut down backend first so the new instance starts clean.
    electron_1.ipcMain.handle('tier2:restart', async () => {
        isQuitting = true;
        await shutdownBackend();
        electron_1.app.relaunch({ args: [] });
        electron_1.app.exit(0);
    });
}
// ---------------------------------------------------------------------------
// App Lifecycle
// ---------------------------------------------------------------------------
process.on('uncaughtException', (error) => {
    console.error('[lifecycle] Uncaught exception:', error);
    if (!isQuitting) {
        isQuitting = true;
        shutdownBackend().finally(() => electron_1.app.exit(1));
    }
});
process.on('unhandledRejection', (reason) => {
    console.error('[lifecycle] Unhandled rejection:', reason);
});
electron_1.app.whenReady().then(async () => {
    registerIPC();
    // Probe the backend port before creating the window so we know whether
    // to manage our own subprocess or rely on an externally-launched one
    // (e.g. the one started by run.py during development).
    const alreadyRunning = await probeBackend();
    createWindow();
    if (alreadyRunning) {
        externalBackend = true;
        console.log('[lifecycle] Detected external backend — skipping spawn');
    }
    else {
        startBackend().catch((err) => {
            console.error('[lifecycle] Backend startup error:', err);
            if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('backend:down');
            }
        });
    }
    electron_1.app.on('activate', () => {
        if (electron_1.BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});
// Window-all-closed: on non-macOS, tell the app to quit (which triggers
// before-quit → backend shutdown → exit).
electron_1.app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        electron_1.app.quit();
    }
});
// Before-quit: prevent the default quit so we can shut down the backend
// asynchronously first.  After cleanup, call app.exit(0) which skips the
// quit events and terminates immediately.
electron_1.app.on('before-quit', (event) => {
    if (isQuitting)
        return;
    event.preventDefault();
    isQuitting = true;
    shutdownBackend().finally(() => {
        electron_1.app.exit(0);
    });
});
// Will-quit: last-resort safety net.  If the app reaches this point with
// the backend still running (e.g. before-quit wasn't invoked on macOS,
// or the async shutdown hangs), force-kill the process tree immediately.
electron_1.app.on('will-quit', () => {
    if (externalBackend)
        return;
    if (backendProcess && !backendProcess.killed) {
        console.log('[lifecycle] will-quit: force-killing backend as last resort');
        const pid = backendProcess.pid;
        backendProcess = null;
        if (pid) {
            if (process.platform === 'win32') {
                (0, child_process_1.execFile)('taskkill', ['/pid', String(pid), '/T', '/F'], { timeout: 3000 }, () => { });
            }
            else {
                try {
                    process.kill(pid, 'SIGKILL');
                }
                catch { /* already dead */ }
            }
        }
    }
});
//# sourceMappingURL=main.js.map