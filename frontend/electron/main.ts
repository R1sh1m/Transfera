import { app, BrowserWindow, ipcMain, dialog, shell } from 'electron'
import { spawn, type ChildProcess } from 'child_process'
import path from 'path'
import fs from 'fs'
import net from 'net'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const BACKEND_PORT = 47821
const VITE_DEV_SERVER = 'http://127.0.0.1:5173'
const isDev = !app.isPackaged

let mainWindow: BrowserWindow | null = null
let backendProcess: ChildProcess | null = null

// ---------------------------------------------------------------------------
// Backend process management
// ---------------------------------------------------------------------------
function getBackendCommand(): { cmd: string; args: string[] } {
  if (isDev) {
    // Development: run from project root
    const backendDir = path.resolve(__dirname, '..', '..', 'backend')
    return {
      cmd: 'python',
      args: ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
    }
  }
  // Production: bundled backend
  const backendDir = path.join(process.resourcesPath, 'backend')
  return {
    cmd: path.join(backendDir, 'venv', 'Scripts', 'python.exe'),
    args: ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
  }
}

function isPortAvailable(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => {
      server.close(() => resolve(true))
    })
    server.listen(port, '127.0.0.1')
  })
}

async function waitForBackend(timeout = 30000): Promise<boolean> {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try {
      const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`)
      if (res.ok) return true
    } catch {
      // not ready yet
    }
    await new Promise((r) => setTimeout(r, 500))
  }
  return false
}

async function startBackend(): Promise<void> {
  const portAvailable = await isPortAvailable(BACKEND_PORT)
  if (!portAvailable) {
    console.log(`Port ${BACKEND_PORT} already in use, backend may already be running.`)
    return
  }

  const { cmd, args } = getBackendCommand()
  console.log(`Starting backend: ${cmd} ${args.join(' ')}`)

  backendProcess = spawn(cmd, args, {
    cwd: isDev ? path.resolve(__dirname, '..', '..', 'backend') : process.resourcesPath,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: false,
  })

  backendProcess.stdout?.on('data', (data: Buffer) => {
    console.log(`[backend] ${data.toString().trim()}`)
  })

  backendProcess.stderr?.on('data', (data: Buffer) => {
    console.error(`[backend] ${data.toString().trim()}`)
  })

  backendProcess.on('error', (err) => {
    console.error('Failed to start backend:', err)
  })

  backendProcess.on('exit', (code, signal) => {
    console.log(`Backend exited with code ${code}, signal ${signal}`)
    backendProcess = null
  })

  const ready = await waitForBackend()
  if (ready) {
    console.log('Backend is ready.')
  } else {
    console.error('Backend failed to start within timeout.')
  }
}

function killBackend(): void {
  if (backendProcess && !backendProcess.killed) {
    console.log('Stopping backend process...')
    backendProcess.kill('SIGTERM')

    // Force kill after 5 seconds if still alive
    setTimeout(() => {
      if (backendProcess && !backendProcess.killed) {
        console.log('Force killing backend process...')
        backendProcess.kill('SIGKILL')
      }
    }, 5000)
  }
}

// ---------------------------------------------------------------------------
// Window creation
// ---------------------------------------------------------------------------
function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: 'MediaVault',
    icon: path.join(__dirname, '..', 'build', 'icon.ico'),
    titleBarStyle: 'hidden',
    backgroundColor: '#ffffff',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    show: false,
  })

  // Load the app
  if (isDev) {
    mainWindow.loadURL(VITE_DEV_SERVER)
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  // Show when ready
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
  })

  // Open DevTools with F12
  mainWindow.webContents.on('before-input-event', (_event, input) => {
    if (input.key === 'F12' && input.type === 'keyDown') {
      mainWindow?.webContents.toggleDevTools()
    }
  })

  // Open external links in system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

// ---------------------------------------------------------------------------
// IPC Handlers
// ---------------------------------------------------------------------------
function registerIPC(): void {
  // Dialogs
  ipcMain.handle('dialog:open', async (_event, options) => {
    if (!mainWindow) return { canceled: true, filePaths: [] }
    return dialog.showOpenDialog(mainWindow, {
      title: options.title,
      defaultPath: options.defaultPath,
      properties: (options.properties ?? []) as Electron.OpenDialogOptions['properties'],
    })
  })

  ipcMain.handle('dialog:save', async (_event, options) => {
    if (!mainWindow) return { canceled: true }
    return dialog.showSaveDialog(mainWindow, {
      title: options.title,
      defaultPath: options.defaultPath,
      filters: options.filters,
    })
  })

  ipcMain.handle('dialog:message', async (_event, options) => {
    if (!mainWindow) return { response: 0, checkboxChecked: false }
    return dialog.showMessageBox(mainWindow, {
      type: options.type as Electron.MessageBoxOptions['type'],
      title: options.title,
      message: options.message,
      detail: options.detail,
      buttons: options.buttons,
    })
  })

  // Window controls
  ipcMain.handle('window:minimize', () => mainWindow?.minimize())
  ipcMain.handle('window:maximize', () => {
    if (mainWindow?.isMaximized()) {
      mainWindow.unmaximize()
    } else {
      mainWindow?.maximize()
    }
  })
  ipcMain.handle('window:close', () => mainWindow?.close())
  ipcMain.handle('window:isMaximized', () => mainWindow?.isMaximized() ?? false)

  // System info
  ipcMain.handle('system:platform', () => process.platform)
  ipcMain.handle('system:version', () => app.getVersion())

  // Backend status
  ipcMain.handle('backend:status', async () => {
    try {
      const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`)
      if (res.ok) {
        return { running: true, port: BACKEND_PORT }
      }
    } catch {
      // not running
    }
    return { running: false, port: BACKEND_PORT }
  })
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------
app.whenReady().then(async () => {
  registerIPC()
  await startBackend()
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    }
  })
})

app.on('window-all-closed', () => {
  killBackend()
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('before-quit', () => {
  killBackend()
})
