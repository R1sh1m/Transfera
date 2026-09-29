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
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, RunEvent, WindowEvent,
};
use tauri_plugin_shell::ShellExt;

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
}

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

fn wait_for_backend(timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if backend_healthy() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    backend_healthy()
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

fn spawn_sidecar(app: &AppHandle) -> Result<(), String> {
    let data_dir = sidecar_data_dir(app);
    std::fs::create_dir_all(&data_dir).map_err(|e| e.to_string())?;

    let sidecar = app
        .shell()
        .sidecar("transfera-engine")
        .map_err(|e| format!("sidecar resolve failed: {e}"))?;
    let (_rx, _child) = sidecar
        .args([
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            &BACKEND_PORT.to_string(),
        ])
        .env("TRANSFERA_DATA_DIR", data_dir)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .spawn()
        .map_err(|e| format!("sidecar spawn failed: {e}"))?;
    // The child is intentionally detached: it outlives this scope and is
    // reaped on shutdown via POST /api/shutdown + taskkill fallback below.
    // (The unused receiver would only matter for stdout streaming.)
    Ok(())
}

fn post_shutdown_signal() -> bool {
    let mut stream = match TcpStream::connect(format!("127.0.0.1:{BACKEND_PORT}")) {
        Ok(s) => s,
        Err(_) => return false,
    };
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok();
    stream.set_write_timeout(Some(Duration::from_secs(5))).ok();
    let body = "{}";
    let req = format!(
        "POST /api/shutdown HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
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
    let external = app
        .state::<BackendState>()
        .external
        .lock()
        .map(|g| *g)
        .unwrap_or(true);
    if external {
        return;
    }
    if post_shutdown_signal() {
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
    match spawn_sidecar(app) {
        Ok(()) => {
            if wait_for_backend(BACKEND_STARTUP_TIMEOUT) {
                if let Some(w) = window {
                    let _ = w.emit("backend:ready", ());
                }
            } else if let Some(w) = window {
                let _ = w.emit("backend:down", ());
            }
        }
        Err(e) => {
            eprintln!("[lifecycle] backend start failed: {e}");
            if let Some(w) = window {
                let _ = w.emit("backend:down", ());
            }
        }
    }
}

fn set_external(app: &AppHandle, value: bool) {
    if let Ok(mut g) = app.state::<BackendState>().external.lock() {
        *g = value;
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
fn set_progress(window: tauri::Window, value: Option<f64>) -> Result<(), String> {
    use tauri::window::{ProgressBarState, ProgressBarStatus};
    let state = match value {
        Some(v) => {
            let pct = (v.clamp(0.0, 1.0) * 100.0).round() as u64;
            ProgressBarState {
                status: Some(ProgressBarStatus::Normal),
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
// Removable-drive watcher (wmic poll, same semantics as Electron main)
// ---------------------------------------------------------------------------

/// Platform removable-drive poll: returns (id, volume_name) pairs, where id
/// is a drive letter on Windows and a mount path elsewhere.
#[cfg(target_os = "windows")]
fn poll_removable_drives() -> Vec<(String, Option<String>)> {
    let out = std::process::Command::new("wmic")
        .args([
            "logicaldisk",
            "where",
            "drivetype=2",
            "get",
            "caption,volumename",
            "/format:csv",
        ])
        .output();
    let mut drives = Vec::new();
    if let Ok(o) = out {
        let text = String::from_utf8_lossy(&o.stdout).into_owned();
        for (i, line) in text.trim().lines().enumerate() {
            if i == 0 {
                continue;
            }
            let parts: Vec<&str> = line.split(',').collect();
            if parts.len() >= 2 {
                let caption = parts[1].trim().to_string();
                let volume = parts.get(2).map(|s| s.trim().to_string());
                if !caption.is_empty() {
                    drives.push((caption, volume));
                }
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

fn start_drive_watcher(app: AppHandle) {
    std::thread::spawn(move || {
        let mut known: HashSet<String> = HashSet::new();
        loop {
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
            "quit" => app.exit(0),
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
            check_virtualization
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            if let Err(e) = build_tray(&handle) {
                eprintln!("[lifecycle] tray init failed: {e}");
            }
            start_drive_watcher(handle.clone());
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
            if let WindowEvent::CloseRequested { .. } = event {
                // Quit semantics (same as Electron): closing the last window
                // shuts down the backend instead of hiding to tray.
                window.app_handle().exit(0);
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build Transfera Tauri app")
        .run(|app, event| {
            if let RunEvent::ExitRequested { api, .. } = event {
                api.prevent_exit();
                let handle = app.clone();
                // Graceful, synchronous-ish: shutdown is fast (local POST +
                // bounded wait), so block this handler briefly rather than
                // risking process teardown mid-cleanup.
                std::thread::spawn(move || {
                    shutdown_backend(&handle);
                    handle.exit(0);
                });
            }
        });
}
