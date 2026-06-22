import http from 'http'
import { app, BrowserWindow, ipcMain, dialog, shell, Notification } from 'electron'
import { spawn, execFile, type ChildProcess } from 'child_process'
import path from 'path'
import fs from 'fs'
import net from 'net'

// ---------------------------------------------------------------------------
// App identity — must be set before app.whenReady() so Windows groups the
// taskbar entry under the correct AppUserModelID.
// ---------------------------------------------------------------------------
app.setAppUserModelId('com.transfera.app')

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const BACKEND_PORT = 47821
const VITE_DEV_SERVER = 'http://127.0.0.1:5173'
const isDev = !app.isPackaged
const GRACEFUL_SHUTDOWN_WAIT = 4000

let mainWindow: BrowserWindow | null = null
let backendProcess: ChildProcess | null = null
let isQuitting = false
let externalBackend = false

// ---------------------------------------------------------------------------
// Backend process management
// ---------------------------------------------------------------------------
function getBackendCommand(): { cmd: string; args: string[] } {
  if (isDev) {
    const projectRoot = path.resolve(__dirname, '..', '..', '..')
    const venvPython = path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
    return {
      cmd: venvPython,
      args: ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
    }
  }
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

// -- Graceful shutdown -----------------------------------------------------------------

/** POST to /api/shutdown and let the backend clean up its own state. */
async function tryGracefulShutdown(): Promise<boolean> {
  try {
    const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/shutdown`, {
      method: 'POST',
      signal: AbortSignal.timeout(5000),
    })
    return res.ok
  } catch {
    return false
  }
}

/** Windows process-tree termination via taskkill. */
async function taskkillProcessTree(pid: number): Promise<void> {
  await new Promise<void>((resolve) => {
    execFile('taskkill', ['/pid', String(pid), '/T', '/F'], { timeout: 5000 }, () => resolve())
  })
}

/** Wait for a child process to exit, with a timeout. */
function waitForProcessExit(proc: ChildProcess, timeout: number): Promise<void> {
  return new Promise((resolve) => {
    if (proc.killed) { resolve(); return }
    const timer = setTimeout(resolve, timeout)
    const done = () => { clearTimeout(timer); resolve() }
    proc.on('exit', done)
    proc.on('error', done)
  })
}

/**
 * Full backend shutdown sequence:
 *   1. POST /api/shutdown (graceful, lets uvicorn drain in-flight work)
 *   2. Wait up to GRACEFUL_SHUTDOWN_WAIT ms for the process to exit on its own
 *   3. If still alive, force-kill the entire process tree (taskkill /T /F on Windows)
 *   4. Last-resort kill
 */
async function shutdownBackend(): Promise<void> {
  if (externalBackend) {
    console.log('[lifecycle] Backend was started externally — leaving it running.')
    backendProcess = null
    return
  }

  if (!backendProcess || backendProcess.killed) {
    backendProcess = null
    return
  }

  const proc = backendProcess
  console.log('[lifecycle] Shutting down backend (PID %s)...', proc.pid)

  const gracefulOk = await tryGracefulShutdown()
  if (gracefulOk) {
    console.log('[lifecycle] Graceful shutdown signal sent, waiting for exit...')
    await waitForProcessExit(proc, GRACEFUL_SHUTDOWN_WAIT)
  }

  if (!proc.killed) {
    console.log('[lifecycle] Force-killing backend process tree...')
    if (process.platform === 'win32' && proc.pid) {
      await taskkillProcessTree(proc.pid)
      await waitForProcessExit(proc, 3000)
    }
    if (!proc.killed) {
      proc.kill()
    }
  }

  backendProcess = null
  console.log('[lifecycle] Backend shutdown complete.')
}

// -- Orphan cleanup --------------------------------------------------------------------

/**
 * Before starting the backend, check whether port BACKEND_PORT is already in
 * use by an orphaned process from a previous crash (or a stale `run.py` run).
 * On Windows, use Get-NetTCPConnection to find the owning PID, then
 * taskkill it with process-tree semantics.  Without this, a crash that left
 * a Python/uvicorn process running would cause EADDRINUSE on the next launch.
 */
async function cleanupOrphanedBackend(): Promise<void> {
  const portAvailable = await isPortAvailable(BACKEND_PORT)
  if (portAvailable) return

  console.log(`[lifecycle] Port ${BACKEND_PORT} is already in use — attempting to clean up orphaned process...`)

  if (process.platform === 'win32') {
    try {
      await new Promise<void>((resolve, reject) => {
        execFile(
          'powershell',
          [
            '-NoProfile', '-NonInteractive', '-Command',
            `Get-NetTCPConnection -LocalPort ${BACKEND_PORT} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | ForEach-Object { taskkill /pid $_ /T /F }`,
          ],
          { timeout: 10000 },
          (err) => (err ? reject(err) : resolve()),
        )
      })
    } catch {
      // orphan may already be gone — that's fine
    }
    // Give taskkill a moment
    await new Promise((r) => setTimeout(r, 1500))
  }

  const nowAvailable = await isPortAvailable(BACKEND_PORT)
  if (nowAvailable) {
    console.log('[lifecycle] Orphaned backend cleaned up successfully.')
  } else {
    console.warn(`[lifecycle] Port ${BACKEND_PORT} is still occupied — will attempt to start anyway.`)
  }
}

// -- Startup ---------------------------------------------------------------------------

/** Lightweight probe — does the backend respond on /api/health? */
function probeBackend(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(
      `http://127.0.0.1:${BACKEND_PORT}/api/health`,
      { timeout: 1000 },
      (res) => {
        resolve(res.statusCode === 200)
        res.resume()
      },
    )
    req.on('error', () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  })
}

async function startBackend(): Promise<void> {
  await cleanupOrphanedBackend()

  const { cmd, args } = getBackendCommand()
  console.log(`[lifecycle] Starting backend: ${cmd} ${args.join(' ')}`)

  backendProcess = spawn(cmd, args, {
    cwd: isDev ? path.resolve(__dirname, '..', '..', '..') : process.resourcesPath,
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
    console.error('[lifecycle] Failed to start backend:', err)
  })

  backendProcess.on('exit', (code, signal) => {
    if (code === 1) {
      // Could be port-already-in-use (e.g. run.py's backend beat us to it).
      // Probe the health endpoint: if it still responds, an external backend
      // is holding the port — adopt it rather than reporting a crash.
      probeBackend().then((stillUp) => {
        if (stillUp) {
          console.log('[lifecycle] Backend exited (code 1, port occupied by external process) — adopting external backend')
          backendProcess = null
          externalBackend = true
          return
        }
        // Port is gone — backend actually crashed.
        console.log(`[lifecycle] Backend exited with code ${code}, signal ${signal}`)
        backendProcess = null
        if (!isQuitting && mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('backend:down')
        }
      }).catch(() => {
        // Probe itself failed — treat as crash.
        console.log(`[lifecycle] Backend exited with code ${code}, signal ${signal}`)
        backendProcess = null
        if (!isQuitting && mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('backend:down')
        }
      })
      return
    }

    // Non-1 exit codes are always crashes.
    console.log(`[lifecycle] Backend exited with code ${code}, signal ${signal}`)
    backendProcess = null
    if (!isQuitting && mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend:down')
    }
  })

  const ready = await waitForBackend()
  if (ready) {
    console.log('[lifecycle] Backend is ready.')
  } else {
    console.error('[lifecycle] Backend failed to start within timeout.')
    mainWindow?.webContents.send('backend:down')
  }
}

// ---------------------------------------------------------------------------
// Icon resolution — platform-aware, works in both dev and packaged builds.
// ---------------------------------------------------------------------------
function resolveIconPath(): string {
  const ext = process.platform === 'win32' ? 'icon.ico' : 'icon.png'
  const candidates: string[] = []

  if (app.isPackaged) {
    candidates.push(path.join(process.resourcesPath, 'build', ext))
    candidates.push(path.join(process.resourcesPath, ext))
  } else {
    candidates.push(path.join(__dirname, '..', 'build', ext))
    candidates.push(path.join(__dirname, '..', '..', 'build', ext))
  }

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }

  console.warn('[icon] Could not resolve icon, tried:', candidates)
  return path.join(__dirname, '..', 'build', ext)
}

// ---------------------------------------------------------------------------
// Window creation
// ---------------------------------------------------------------------------
function createWindow(): void {
  const iconPath = resolveIconPath()
  console.log(`[icon] Using icon: ${iconPath}`)

  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: 'Transfera',
    icon: iconPath,
    frame: false,
    backgroundColor: '#0f0f0f',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    show: false,
  })

  if (isDev) {
    mainWindow.loadURL(VITE_DEV_SERVER)
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
  })

  mainWindow.webContents.on('before-input-event', (_event, input) => {
    if (input.key === 'F12' && input.type === 'keyDown') {
      mainWindow?.webContents.toggleDevTools()
    }
  })

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

  ipcMain.handle('dialog:open-directory', async (_event, defaultPath?: string) => {
    console.log('[IPC] dialog:open-directory invoked', defaultPath ?? '')
    if (!mainWindow) return null
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openDirectory'],
      defaultPath,
    })
    return result.canceled ? null : result.filePaths[0]
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

  // Close window — triggers the normal Electron quit lifecycle
  // (window-all-closed → app.quit() → before-quit → backend cleanup → exit).
  ipcMain.handle('window:close', () => mainWindow?.close())

  ipcMain.handle('window:isMaximized', () => mainWindow?.isMaximized() ?? false)

  ipcMain.handle('shell:showItemInFolder', (_event, fullPath: string) => {
    shell.showItemInFolder(fullPath)
  })
  ipcMain.handle('shell:openPath', (_event, fullPath: string) => {
    shell.openPath(fullPath)
  })

  ipcMain.handle('system:platform', () => process.platform)
  ipcMain.handle('system:version', () => app.getVersion())

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

  ipcMain.handle(
    'notification:show',
    (_event, opts: { title: string; body: string; sessionId: number }) => {
      if (!Notification.isSupported()) {
        console.warn('[notification] OS notifications not supported')
        return false
      }

      const iconPath = resolveIconPath()
      const notification = new Notification({
        title: opts.title,
        body: opts.body,
        icon: iconPath,
        silent: false,
      })

      notification.on('click', () => {
        if (mainWindow) {
          if (mainWindow.isMinimized()) mainWindow.restore()
          mainWindow.focus()
        }
        mainWindow?.webContents.send('notification:click', opts.sessionId)
      })

      notification.show()
      return true
    },
  )

  ipcMain.handle('window:isFocused', () => mainWindow?.isFocused() ?? false)

  // -- Elevated driver installation ---------------------------------------------------
  ipcMain.handle(
    'driver:installElevated',
    async (_event, opts: { executable: string; args: string[] }) => {
      const argsString = opts.args.map((a) => `'${a.replace(/'/g, "''")}'`).join(',')
      const psCommand = [
        `$p = Start-Process -FilePath '${opts.executable}'`,
        `-ArgumentList @(${argsString})`,
        '-Verb RunAs -Wait -PassThru',
        '$p.ExitCode',
      ].join(' ')

      return new Promise<{ success: boolean; exitCode: number | null; error?: string }>((resolve) => {
        execFile(
          'powershell',
          ['-NoProfile', '-NonInteractive', '-Command', psCommand],
          { timeout: 300_000 },
          (error, stdout, stderr) => {
            if (error) {
              if (error.killed) {
                resolve({ success: false, exitCode: null, error: 'Installation timed out' })
              } else {
                const exitCode = (error as NodeJS.ErrnoException).code
                if (exitCode === '1602' || exitCode === '1223') {
                  resolve({ success: false, exitCode: null, error: 'Installation cancelled' })
                } else {
                  resolve({
                    success: false,
                    exitCode: exitCode != null ? Number(exitCode) : null,
                    error: stderr?.trim() || error.message,
                  })
                }
              }
              return
            }

            const exitCode = parseInt(stdout?.trim() || '0', 10)
            resolve({ success: exitCode === 0, exitCode })
          },
        )
      })
    },
  )

  ipcMain.handle('driver:openStorePage', async () => {
    const storeUri = 'ms-windows-store://pdp/?productid=9NMPJ99VJBWV'
    const storeWebUrl = 'https://apps.microsoft.com/detail/apple-devices/9NMPJ99VJBWV'
    try {
      await shell.openExternal(storeUri)
      return { opened: true }
    } catch {
      try {
        await shell.openExternal(storeWebUrl)
        return { opened: true }
      } catch {
        return { opened: false }
      }
    }
  })

  // -- Tier 2 (WSL2 + usbipd-win) IPC handlers ----------------------------------------

  ipcMain.handle(
    'tier2:runElevated',
    async (
      _event,
      opts: { executable: string; args: string[]; description: string },
    ): Promise<{ success: boolean; exitCode: number | null; error?: string }> => {
      const argsString = opts.args.map((a) => `'${a.replace(/'/g, "''")}'`).join(',')
      const psCommand = [
        `$p = Start-Process -FilePath '${opts.executable}'`,
        `-ArgumentList @(${argsString})`,
        '-Verb RunAs -Wait -PassThru',
        '$p.ExitCode',
      ].join(' ')

      return new Promise<{ success: boolean; exitCode: number | null; error?: string }>((resolve) => {
        execFile(
          'powershell',
          ['-NoProfile', '-NonInteractive', '-Command', psCommand],
          { timeout: 300_000 },
          (error, stdout, stderr) => {
            if (error) {
              const exitCode = (error as NodeJS.ErrnoException).code
              if (exitCode === '1602' || exitCode === '1223') {
                resolve({ success: false, exitCode: null, error: 'User cancelled elevation prompt' })
              } else if (error.killed) {
                resolve({ success: false, exitCode: null, error: 'Command timed out' })
              } else {
                resolve({
                  success: false,
                  exitCode: exitCode != null ? Number(exitCode) : null,
                  error: stderr?.trim() || error.message,
                })
              }
              return
            }
            const exitCode = parseInt(stdout?.trim() || '0', 10)
            resolve({ success: exitCode === 0, exitCode })
          },
        )
      })
    },
  )

  ipcMain.handle(
    'tier2:runCommand',
    async (
      _event,
      opts: { executable: string; args: string[]; elevated?: boolean; timeoutMs?: number },
    ): Promise<{ success: boolean; stdout: string; stderr: string; exitCode: number | null }> => {
      const timeout = opts.timeoutMs ?? 120_000

      if (opts.elevated) {
        const argsString = opts.args.map((a) => `'${a.replace(/'/g, "''")}'`).join(',')
        const psCommand = [
          `$p = Start-Process -FilePath '${opts.executable}'`,
          `-ArgumentList @(${argsString})`,
          '-Verb RunAs -Wait -PassThru',
          `$p.ExitCode`,
        ].join(' ')

        return new Promise((resolve) => {
          execFile(
            'powershell',
            ['-NoProfile', '-NonInteractive', '-Command', psCommand],
            { timeout },
            (error, stdout, stderr) => {
              if (error) {
                const exitCode = (error as NodeJS.ErrnoException).code
                if (exitCode === '1602' || exitCode === '1223') {
                  resolve({ success: false, stdout: '', stderr: 'User cancelled elevation', exitCode: null })
                } else {
                  resolve({
                    success: false,
                    stdout: stdout?.trim() || '',
                    stderr: stderr?.trim() || error.message,
                    exitCode: exitCode != null ? Number(exitCode) : null,
                  })
                }
                return
              }
              const exitCode = parseInt(stdout?.trim() || '0', 10)
              resolve({ success: exitCode === 0, stdout: stdout?.trim() || '', stderr: stderr?.trim() || '', exitCode })
            },
          )
        })
      }

      return new Promise((resolve) => {
        execFile(
          opts.executable,
          opts.args,
          { timeout },
          (error, stdout, stderr) => {
            if (error) {
              const exitCode = (error as NodeJS.ErrnoException & { code?: string | number }).code
              resolve({
                success: false,
                stdout: stdout?.trim() || '',
                stderr: stderr?.trim() || error.message,
                exitCode: typeof exitCode === 'number' ? exitCode : null,
              })
              return
            }
            const exitCode = parseInt(stdout?.trim() || '0', 10)
            resolve({ success: exitCode === 0, stdout: stdout?.trim() || '', stderr: stderr?.trim() || '', exitCode })
          },
        )
      })
    },
  )

  ipcMain.handle('tier2:checkVirtualization', async () => {
    return new Promise<{ available: boolean; details: string }>((resolve) => {
      execFile(
        'powershell',
        ['-NoProfile', '-NonInteractive', '-Command', 'Get-ComputerInfo | Select-Object -ExpandProperty HyperVRequirementVirtualizationFirmwareEnabled'],
        { timeout: 30_000 },
        (error, stdout, stderr) => {
          if (error) {
            resolve({ available: false, details: stderr?.trim() || error.message })
            return
          }
          const val = stdout?.trim().toLowerCase()
          if (val === 'true') {
            resolve({ available: true, details: 'Hardware virtualization is enabled' })
          } else if (val === 'false') {
            resolve({ available: false, details: 'Hardware virtualization is disabled in BIOS. Enable VT-x/AMD-V in your firmware settings to use Tier 2 (WSL2).' })
          } else {
            resolve({ available: false, details: `Unexpected output: ${stdout?.trim() || '(empty)'}` })
          }
        },
      )
    })
  })

  // Relaunch the app — shut down backend first so the new instance starts clean.
  ipcMain.handle('tier2:restart', async () => {
    isQuitting = true
    await shutdownBackend()
    app.relaunch({ args: [] })
    app.exit(0)
  })
}

// ---------------------------------------------------------------------------
// App Lifecycle
// ---------------------------------------------------------------------------

process.on('uncaughtException', (error) => {
  console.error('[lifecycle] Uncaught exception:', error)
  if (!isQuitting) {
    isQuitting = true
    shutdownBackend().finally(() => app.exit(1))
  }
})

process.on('unhandledRejection', (reason) => {
  console.error('[lifecycle] Unhandled rejection:', reason)
})

app.whenReady().then(async () => {
  registerIPC()

  const envExternalBackend = process.env.TRANSFERA_EXTERNAL_BACKEND === '1'

  createWindow()

  if (envExternalBackend) {
    externalBackend = true
    console.log('[lifecycle] TRANSFERA_EXTERNAL_BACKEND set — skipping backend spawn')
  } else {
    // Probe the backend port before spawning so we know whether
    // to manage our own subprocess or rely on an externally-launched one.
    const alreadyRunning = await probeBackend()

    if (alreadyRunning) {
      externalBackend = true
      console.log('[lifecycle] Detected external backend — skipping spawn')
    } else {
      startBackend().catch((err) => {
        console.error('[lifecycle] Backend startup error:', err)
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('backend:down')
        }
      })
    }
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    }
  })
})

// Window-all-closed: on non-macOS, tell the app to quit (which triggers
// before-quit → backend shutdown → exit).
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

// Before-quit: prevent the default quit so we can shut down the backend
// asynchronously first.  After cleanup, call app.exit(0) which skips the
// quit events and terminates immediately.
app.on('before-quit', (event) => {
  if (isQuitting) return
  event.preventDefault()
  isQuitting = true

  shutdownBackend().finally(() => {
    app.exit(0)
  })
})

// Will-quit: last-resort safety net.  If the app reaches this point with
// the backend still running (e.g. before-quit wasn't invoked on macOS,
// or the async shutdown hangs), force-kill the process tree immediately.
app.on('will-quit', () => {
  if (externalBackend) return

  if (backendProcess && !backendProcess.killed) {
    console.log('[lifecycle] will-quit: force-killing backend as last resort')
    const pid = backendProcess.pid
    backendProcess = null
    if (pid) {
      if (process.platform === 'win32') {
        execFile('taskkill', ['/pid', String(pid), '/T', '/F'], { timeout: 3000 }, () => {})
      } else {
        try { process.kill(pid, 'SIGKILL') } catch { /* already dead */ }
      }
    }
  }
})
