// ---------------------------------------------------------------------------
// Transfera v2 — Tauri shell (replaces frontend/electron/main.ts)
// Owns the WebView2 window, the Python sidecar lifecycle, tray, drive
// watcher, and the audited elevated-command + shell-open surface that
// frontend/src/lib/desktop.ts calls into.
//
// Backend contract (unchanged from Electron):
//   FastAPI on 127.0.0.1:47821, /api/health probe, POST /api/shutdown for
//   graceful exit, `backend:starting|ready|down` window events, device
//   events on `device:new-removable-drive`. TRANSFERA_EXTERNAL_BACKEND=1 or
//   an already-healthy port means "adopt, don't spawn" (dev parity).
// ---------------------------------------------------------------------------

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashSet;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, RunEvent, WindowEvent,
};

const BACKEND_PORT: u16 = 47821;
const BACKEND_STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const GRACEFUL_SHUTDOWN_WAIT: Duration = Duration::from_secs(4);

/// IPC allowlist — renderer input must never reach process spawn unvalidated.
/// Mirrors ALLOWED_ELEVATED_BINARIES in the old Electron main process.
const ALLOWED_ELEVATED_BINARIES: [&str; 4] = ["winget.exe", "wsl.exe", "usbipd.exe", "sc.exe"];

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------

struct BackendState {
    /// True when we adopted an externally-managed backend (dev server /
    /// run.py) instead of spawning our own sidecar.
    external: Mutex<bool>,
    /// Local secret token read from data_dir/local_secret.json at startup.
    /// Sent as the X-Local-Token header to POST /api/shutdown so the
    /// graceful shutdown endpoint does not return 403 (GAP-3 fix).
    local_token: Mutex<Option<String>>,
}

/// Shared flag passed to start_drive_watcher so the background thread
/// can exit cleanly when the app quits (GAP-1 fix).
struct DriveShutdown(Arc<AtomicBool>);

/// Guard flag: when false, window close requests are intercepted so the
/// frontend can prompt if a transfer is actively in progress (ROUGH-3).
struct CloseGuardState(Arc<AtomicBool>);

// ---------------------------------------------------------------------------
// Health probing (plain TCP + minimal HTTP so no extra HTTP crate is needed)
// ---------------------------------------------------------------------------

fn port_open() -> bool {
    TcpStream::connect(format!("127.0.0.1:{BACKEND_PORT}"))
        .map(|_| true)
        .unwrap_or(false)
}

fn http_get(path: &str) -> Option<(u16, String)> {
    let mut stream = TcpStream::connect(format!("127.0.0.1:{BACKEND_PORT}")).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .ok()?;
    write!(
        stream,
        "GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    )
    .ok()?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf).into_owned();
    let status = text
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|c| c.parse::<u16>().ok())?;
    Some((status, text))
}

fn backend_healthy() -> bool {
    matches!(http_get("/api/health"), Some((200, _)))
}

// ---------------------------------------------------------------------------
// Sidecar lifecycle
// ---------------------------------------------------------------------------

fn sidecar_data_dir(app: &AppHandle) -> PathBuf {
    app.path()
        .app_data_dir()
        .map(|p| p.join("data"))
        .unwrap_or_else(|_| PathBuf::from("backend-data"))
}

/// Absolute path of the frozen engine inside the bundled resources.
///
/// The PyInstaller one-dir bundle (transfera-engine.exe + _internal/
/// runtime) ships as a Tauri *resource* — externalBin only supports single
/// files, so the exe must stay next to its _internal/ folder. The path is
/// resolved with the same config-relative syntax as tauri.conf.json's
/// `bundle.resources` entry: on Windows resource_dir() is the exe's own
/// folder and resources live under a `resources/` subdir there, so the
/// `resources/` prefix is load-bearing (resolving bare
/// `transfera-engine/...` misses and the engine never spawns).
fn sidecar_exe_path(app: &AppHandle) -> Option<PathBuf> {
    use tauri::path::BaseDirectory;
    let rel = if cfg!(target_os = "windows") {
        "resources/transfera-engine/transfera-engine.exe"
    } else {
        "resources/transfera-engine/transfera-engine"
    };
    app.path().resolve(rel, BaseDirectory::Resource).ok()
}

fn spawn_sidecar(app: &AppHandle) -> Result<std::process::Child, String> {
    let data_dir = sidecar_data_dir(app);
    std::fs::create_dir_all(&data_dir).map_err(|e| e.to_string())?;

    let exe = sidecar_exe_path(app).ok_or_else(|| "sidecar path unresolved".to_string())?;
    if !exe.is_file() {
        return Err(format!("sidecar missing at {}", exe.display()));
    }
    // Helper binaries (wpd_helper.exe, exiftool.exe) sit directly in the
    // resource dir, one level above the one-dir bundle folder.
    let resource_dir = exe
        .parent()
        .and_then(|p| p.parent())
        .map(PathBuf::from)
        .unwrap_or_else(|| data_dir.clone());
    // The frozen entry point (backend/main.py __main__) boots uvicorn
    // itself from backend.config defaults, so no CLI args are passed.
    // Detached stdio: the windowed bootloader shows its own fatal dialog
    // on startup failure, which is the diagnosable path for bad bundles.
    std::process::Command::new(&exe)
        .env("TRANSFERA_DATA_DIR", &data_dir)
        .env("TRANSFERA_RESOURCE_DIR", &resource_dir)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("sidecar spawn failed ({}): {e}", exe.display()))
}

fn post_shutdown_signal(token: Option<&str>) -> bool {
    let mut stream = match TcpStream::connect(format!("127.0.0.1:{BACKEND_PORT}")) {
        Ok(s) => s,
        Err(_) => return false,
    };
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok();
    stream.set_write_timeout(Some(Duration::from_secs(5))).ok();
    let body = "{}";
    // Include the local secret token so require_local_token() passes (GAP-3 fix).
    let token_header = token
        .map(|t| format!("X-Local-Token: {}\r\n", t))
        .unwrap_or_default();
    let req = format!(
        "POST /api/shutdown HTTP/1.0\r\nHost: 127.0.0.1\r\n{}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        token_header,
        body.len(),
        body
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    if stream.read_to_end(&mut buf).is_err() {
        return false;
    }
    String::from_utf8_lossy(&buf).contains("200")
}

/// Full shutdown: graceful POST, wait, then force-kill the port owner tree.
fn shutdown_backend(app: &AppHandle) {
    let state = app.state::<BackendState>();
    let external = state.external.lock().map(|g| *g).unwrap_or(true);
    if external {
        return;
    }
    let token: Option<String> = state.local_token.lock().ok().and_then(|g| g.clone());
    if post_shutdown_signal(token.as_deref()) {
        let start = Instant::now();
        while start.elapsed() < GRACEFUL_SHUTDOWN_WAIT {
            if !port_open() {
                return;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }
    // Last resort: kill whatever still owns the backend port. Windows-only;
    // on macOS/Linux the sidecar is a child process reaped on app exit, so
    // the graceful POST above is sufficient.
    #[cfg(target_os = "windows")]
    if port_open() {
        let _ = std::process::Command::new("powershell")
            .args([
                "-NoProfile",
                "-Command",
                &format!(
                    "Get-NetTCPConnection -LocalPort {BACKEND_PORT} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | ForEach-Object {{ taskkill /pid $_ /T /F }}"
                ),
            ])
            .output();
    }
}

fn ensure_backend(app: &AppHandle) {
    let window = app.get_webview_window("main");
    if std::env::var("TRANSFERA_EXTERNAL_BACKEND").as_deref() == Ok("1") {
        set_external(app, true);
        return;
    }
    if backend_healthy() {
        // Adopt (run.py dev server or a previous instance's backend).
        set_external(app, true);
        if let Some(w) = window {
            let _ = w.emit("backend:ready", ());
        }
        return;
    }
    set_external(app, false);
    if let Some(w) = &window {
        let _ = w.emit("backend:starting", ());
    }
    let mut child = match spawn_sidecar(app) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[lifecycle] backend start failed: {e}");
            if let Some(w) = window {
                let _ = w.emit("backend:down", ());
            }
            return;
        }
    };
    // Wait for health, but surface an early sidecar death immediately: the
    // frozen engine exits on its own on a bad bundle (missing _internal/,
    // blocked DLL) and making the UI sit out the full 60s timeout first
    // looks like a hang on the splash screen.
    let start = Instant::now();
    let mut ready = false;
    while start.elapsed() < BACKEND_STARTUP_TIMEOUT {
        if backend_healthy() {
            ready = true;
            break;
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                eprintln!("[lifecycle] sidecar exited during startup: {status}");
                break;
            }
            Ok(None) => {}
            Err(e) => {
                eprintln!("[lifecycle] sidecar wait failed: {e}");
                break;
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    // Detached by design: a healthy engine outlives this scope and is
    // reaped on shutdown via POST /api/shutdown + taskkill fallback below.
    drop(child);
    if ready {
        // Token file is written by the backend on first boot; read it now
        // that we know the process is healthy (GAP-3 fix).
        load_local_token(app);
    }
    if let Some(w) = window {
        let _ = w.emit(
            if ready {
                "backend:ready"
            } else {
                "backend:down"
            },
            (),
        );
    }
}

fn set_external(app: &AppHandle, value: bool) {
    if let Ok(mut g) = app.state::<BackendState>().external.lock() {
        *g = value;
    }
}

/// Read local_secret.json from the sidecar data dir and cache the token.
/// Called once after spawn_sidecar succeeds so every graceful-shutdown
/// attempt can present the token (GAP-3 fix).
fn load_local_token(app: &AppHandle) {
    let secret_path = sidecar_data_dir(app).join("local_secret.json");
    let token = std::fs::read_to_string(&secret_path).ok().and_then(|s| {
        let v: serde_json::Value = serde_json::from_str(&s).ok()?;
        v["token"].as_str().map(String::from)
    });
    if let Ok(mut g) = app.state::<BackendState>().local_token.lock() {
        *g = token;
    }
}

// ---------------------------------------------------------------------------
// Audited commands (called from frontend/src/lib/desktop.ts)
// ---------------------------------------------------------------------------

fn normalize_allowed_executable(raw: &str) -> Option<&'static str> {
    if raw.is_empty() || raw.len() > 260 {
        return None;
    }
    let base = raw.rsplit(['/', '\\']).next().unwrap_or(raw).to_lowercase();
    let with_exe = if base.ends_with(".exe") {
        base
    } else {
        format!("{base}.exe")
    };
    ALLOWED_ELEVATED_BINARIES
        .iter()
        .find(|allowed| **allowed == with_exe)
        .copied()
}

fn is_safe_arg(arg: &str) -> bool {
    !arg.is_empty()
        && arg.len() <= 2000
        && !arg.contains('\0')
        && !arg.contains('\n')
        && !arg.contains('\r')
}

#[derive(Serialize)]
struct ElevatedResult {
    success: bool,
    #[serde(rename = "exitCode")]
    exit_code: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[tauri::command]
fn run_elevated(executable: String, args: Vec<String>) -> ElevatedResult {
    // The allowlist is Windows driver tooling (winget/wsl/usbipd/sc).
    // Fail closed with a clear message instead of spawning a nonexistent
    // `powershell` on macOS/Linux.
    #[cfg(not(target_os = "windows"))]
    {
        let _ = (&executable, &args);
        return ElevatedResult {
            success: false,
            exit_code: None,
            error: Some("Elevated install is only supported on Windows".into()),
        };
    }
    let Some(exe) = normalize_allowed_executable(&executable) else {
        return ElevatedResult {
            success: false,
            exit_code: None,
            error: Some("Executable not allowed".into()),
        };
    };
    if args.len() > 64 || args.iter().any(|a| !is_safe_arg(a)) {
        return ElevatedResult {
            success: false,
            exit_code: None,
            error: Some("Unsafe argument blocked".into()),
        };
    }
    let args_ps = args
        .iter()
        .map(|a| format!("'{}'", a.replace('\'', "''")))
        .collect::<Vec<_>>()
        .join(",");
    let ps = format!(
        "$p = Start-Process -FilePath '{exe}' -ArgumentList @({args_ps}) -Verb RunAs -Wait -PassThru; $p.ExitCode"
    );
    // NOTE: synchronous by design — Tauri runs sync commands on its blocking
    // pool, and driver installs legitimately take minutes. A future step can
    // stream progress events if the UI needs finer feedback.
    match std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-NonInteractive",
            "-Command",
            &ps,
        ])
        .output()
    {
        Ok(out) => {
            let code = String::from_utf8_lossy(&out.stdout)
                .trim()
                .parse::<i32>()
                .unwrap_or(-1);
            ElevatedResult {
                success: code == 0,
                exit_code: Some(code),
                error: if code == 0 {
                    None
                } else {
                    Some(String::from_utf8_lossy(&out.stderr).trim().to_string())
                },
            }
        }
        Err(e) => ElevatedResult {
            success: false,
            exit_code: None,
            error: Some(e.to_string()),
        },
    }
}

fn is_existing_path(p: &str) -> bool {
    !p.is_empty() && p.len() <= 32767 && std::path::Path::new(p).exists()
}

#[tauri::command]
fn show_item_in_folder(full_path: String) -> Result<(), String> {
    if !is_existing_path(&full_path) {
        return Err("Path does not exist".into());
    }
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg("/select,")
            .arg(&full_path)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    // macOS Finder reveals with `open -R`; Linux has no reveal API, so
    // fall back to opening the containing folder.
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg("-R")
            .arg(&full_path)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let parent = std::path::Path::new(&full_path)
            .parent()
            .map(|p| p.as_os_str().to_owned())
            .unwrap_or_default();
        std::process::Command::new("xdg-open")
            .arg(&parent)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
}

#[tauri::command]
fn open_path(full_path: String) -> Result<(), String> {
    if !is_existing_path(&full_path) {
        return Err("Path does not exist".into());
    }
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg(&full_path)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&full_path)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        std::process::Command::new("xdg-open")
            .arg(&full_path)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
}

#[tauri::command]
fn set_progress(
    window: tauri::Window,
    value: Option<f64>,
    status: Option<String>,
) -> Result<(), String> {
    use tauri::window::{ProgressBarState, ProgressBarStatus};
    let bar_status = match status.as_deref() {
        Some("paused") => ProgressBarStatus::Paused,
        Some("error") => ProgressBarStatus::Error,
        _ => ProgressBarStatus::Normal,
    };
    let state = match value {
        Some(v) => {
            let pct = (v.clamp(0.0, 1.0) * 100.0).round() as u64;
            ProgressBarState {
                status: Some(bar_status),
                progress: Some(pct),
            }
        }
        None => ProgressBarState {
            status: Some(ProgressBarStatus::None),
            progress: Some(0),
        },
    };
    window.set_progress_bar(state).map_err(|e| e.to_string())
}

#[tauri::command]
fn force_exit(app: AppHandle, state: tauri::State<CloseGuardState>) {
    state.0.store(true, Ordering::SeqCst);
    app.exit(0);
}

#[derive(Serialize)]
struct VirtualizationStatus {
    available: bool,
    details: String,
}

#[tauri::command]
fn check_virtualization() -> VirtualizationStatus {
    // Answers the Windows-only "is VT-x/AMD-V on for WSL2?" setup question.
    #[cfg(not(target_os = "windows"))]
    {
        return VirtualizationStatus {
            available: false,
            details: "Virtualization check applies to Windows (WSL2 setup) only".into(),
        };
    }
    let out = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-NonInteractive",
            "-Command",
            "Get-ComputerInfo | Select-Object -ExpandProperty HyperVRequirementVirtualizationFirmwareEnabled",
        ])
        .output();
    match out {
        Ok(o) => match String::from_utf8_lossy(&o.stdout).trim().to_lowercase().as_str() {
            "true" => VirtualizationStatus {
                available: true,
                details: "Hardware virtualization is enabled".into(),
            },
            "false" => VirtualizationStatus {
                available: false,
                details: "Hardware virtualization is disabled in BIOS. Enable VT-x/AMD-V in your firmware settings to use Tier 2 (WSL2).".into(),
            },
            other => VirtualizationStatus {
                available: false,
                details: format!("Unexpected output: {}", other),
            },
        },
        Err(e) => VirtualizationStatus {
            available: false,
            details: e.to_string(),
        },
    }
}

// ---------------------------------------------------------------------------
// Removable-drive watcher (native Win32 API, ROUGH-2 fix replacing deprecated wmic)
// ---------------------------------------------------------------------------

/// Platform removable-drive poll: returns (id, volume_name) pairs, where id
/// is a drive letter on Windows and a mount path elsewhere.
#[cfg(target_os = "windows")]
fn poll_removable_drives() -> Vec<(String, Option<String>)> {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetLogicalDrives() -> u32;
        fn GetDriveTypeW(lpRootPathName: *const u16) -> u32;
        fn GetVolumeInformationW(
            lpRootPathName: *const u16,
            lpVolumeNameBuffer: *mut u16,
            nVolumeNameSize: u32,
            lpVolumeSerialNumber: *mut u32,
            lpMaximumComponentLength: *mut u32,
            lpFileSystemFlags: *mut u32,
            lpFileSystemNameBuffer: *mut u16,
            nFileSystemNameSize: u32,
        ) -> i32;
    }

    const DRIVE_REMOVABLE: u32 = 2;
    let mut drives = Vec::new();

    let mask = unsafe { GetLogicalDrives() };
    for i in 0..26 {
        if (mask & (1 << i)) != 0 {
            let letter = (b'A' + i) as char;
            let root: [u16; 4] = [letter as u16, b':' as u16, b'\\' as u16, 0];
            let drive_type = unsafe { GetDriveTypeW(root.as_ptr()) };
            if drive_type == DRIVE_REMOVABLE {
                let id = format!("{}:", letter);
                let mut vol_buf = [0u16; 260];
                let res = unsafe {
                    GetVolumeInformationW(
                        root.as_ptr(),
                        vol_buf.as_mut_ptr(),
                        vol_buf.len() as u32,
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        0,
                    )
                };
                let volume_name = if res != 0 {
                    let len = vol_buf
                        .iter()
                        .position(|&c| c == 0)
                        .unwrap_or(vol_buf.len());
                    let name = String::from_utf16_lossy(&vol_buf[..len]).trim().to_string();
                    if name.is_empty() {
                        None
                    } else {
                        Some(name)
                    }
                } else {
                    None
                };
                drives.push((id, volume_name));
            }
        }
    }
    drives
}

/// macOS: external disks mount under /Volumes; Linux: /media, /run/media, /mnt.
#[cfg(not(target_os = "windows"))]
fn poll_removable_drives() -> Vec<(String, Option<String>)> {
    let mut roots: Vec<std::path::PathBuf> = Vec::new();
    #[cfg(target_os = "macos")]
    roots.push(std::path::PathBuf::from("/Volumes"));
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        if let Ok(user) = std::env::var("USER").or_else(|_| std::env::var("LOGNAME")) {
            roots.push(std::path::PathBuf::from(format!("/media/{user}")));
            roots.push(std::path::PathBuf::from(format!("/run/media/{user}")));
        }
        roots.push(std::path::PathBuf::from("/mnt"));
    }
    let mut drives = Vec::new();
    for root in roots {
        let entries = std::fs::read_dir(&root);
        if let Ok(it) = entries {
            for entry in it.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    let name = entry.file_name().to_string_lossy().into_owned();
                    drives.push((path.to_string_lossy().into_owned(), Some(name)));
                }
            }
        }
    }
    drives
}

fn start_drive_watcher(app: AppHandle, shutdown: Arc<AtomicBool>) {
    std::thread::spawn(move || {
        let mut known: HashSet<String> = HashSet::new();
        // GAP-1 fix: check shutdown flag so this thread exits cleanly on quit
        // instead of looping forever and delaying process exit.
        while !shutdown.load(Ordering::Relaxed) {
            let mut current = HashSet::new();
            for (id, volume) in poll_removable_drives() {
                current.insert(id.clone());
                if !known.contains(&id) {
                    let _ = app.emit(
                        "device:new-removable-drive",
                        serde_json::json!({
                            "driveLetter": id,
                            "volumeName": volume,
                        }),
                    );
                }
            }
            known = current;
            std::thread::sleep(Duration::from_secs(5));
        }
    });
}

// ---------------------------------------------------------------------------
// Tray
// ---------------------------------------------------------------------------

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let icon = app
        .default_window_icon()
        .cloned()
        .expect("window icon must be bundled");
    let open = MenuItem::with_id(app, "open", "Open Transfera", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    TrayIconBuilder::with_id("main")
        .icon(icon)
        .tooltip("Transfera")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                }
            }
            "quit" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                    let _ = w.emit("app:request-close", ());
                } else {
                    app.exit(0);
                }
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                }
            }
        })
        .build(app)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

fn main() {
    tauri::Builder::default()
        .manage(BackendState {
            external: Mutex::new(false),
            local_token: Mutex::new(None),
        })
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![
            run_elevated,
            show_item_in_folder,
            open_path,
            set_progress,
            check_virtualization,
            force_exit
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            if let Err(e) = build_tray(&handle) {
                eprintln!("[lifecycle] tray init failed: {e}");
            }
            // Shared shutdown flag: set by the exit handler to stop the
            // drive-watcher thread cleanly (GAP-1 fix).
            let drive_shutdown = Arc::new(AtomicBool::new(false));
            app.manage(DriveShutdown(drive_shutdown.clone()));
            start_drive_watcher(handle.clone(), drive_shutdown);
            // Close guard state: allows frontend to intercept close requests (ROUGH-3).
            let allow_close = Arc::new(AtomicBool::new(false));
            app.manage(CloseGuardState(allow_close.clone()));
            // Backend boot must not block window creation — the frontend
            // shows its starting state from the `backend:starting` event.
            std::thread::spawn(move || ensure_backend(&handle));
            // Reveal the window once created (frontend paints its own
            // loading state from the `backend:starting` event), matching
            // Electron's ready-to-show behaviour closely enough that no
            // white flash is visible against the transparent window.
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let allow = window
                    .app_handle()
                    .try_state::<CloseGuardState>()
                    .map(|s| s.0.load(Ordering::SeqCst))
                    .unwrap_or(true);
                if allow {
                    window.app_handle().exit(0);
                } else {
                    api.prevent_close();
                    let _ = window.emit("app:request-close", ());
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build Transfera Tauri app")
        .run({
            // GAP-2 fix: guard against a double ExitRequested when tray Quit
            // calls app.exit(0), the shutdown thread runs, then calls
            // handle.exit(0) again — which fires a second ExitRequested.
            let shutdown_started = Arc::new(AtomicBool::new(false));
            move |app, event| {
                if let RunEvent::ExitRequested { api, .. } = event {
                    if shutdown_started.swap(true, Ordering::SeqCst) {
                        // Second ExitRequested: backend already shutting down,
                        // do NOT prevent_exit again or spawn another thread.
                        return;
                    }
                    // Signal the drive-watcher thread to exit (GAP-1 fix).
                    if let Some(ds) = app.try_state::<DriveShutdown>() {
                        ds.0.store(true, Ordering::Relaxed);
                    }
                    api.prevent_exit();
                    let handle = app.clone();
                    std::thread::spawn(move || {
                        shutdown_backend(&handle);
                        handle.exit(0);
                    });
                }
            }
        });
}
