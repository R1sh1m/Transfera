#!/usr/bin/env python3
"""
Transfera v2 — Development Stack Orchestrator
Single-command startup for the full backend + frontend development environment.

Enforces Python 3.12 for stable pre-compiled wheel availability (pillow-heif, blake3).

Usage:
    python run.py              # Start everything (backend serves compiled frontend)
    python run.py --backend    # Backend only (serves compiled frontend)
    python run.py --frontend   # Vite dev server only (hot-reload)
    python run.py --skip-deps  # Skip dependency checks
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"
VENV_DIR = ROOT_DIR / ".venv"
NODE_MODULES = FRONTEND_DIR / "node_modules"
REQ_FILE = BACKEND_DIR / "requirements.txt"
NATIVE_WPD_DIR = ROOT_DIR / "native" / "wpd_helper"
WPD_HELPER_EXE = BACKEND_DIR / "bin" / "wpd_helper.exe"

BACKEND_PORT = 47821
VITE_PORT = 5173

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Python 3.12 discovery -- stable wheel availability is critical
# ---------------------------------------------------------------------------
# Ordered list of paths to probe for a Python 3.12 installation on Windows.
# The user-local path is resolved at runtime to avoid baking a real username into source.
_PYTHON312_CANDIDATES: list[Path] = [
    *([Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python" / "Python312" / "python.exe"]
      if IS_WINDOWS and "LOCALAPPDATA" in os.environ else []),
    Path(r"C:\Program Files\Python312\python.exe"),
    Path(r"C:\Python312\python.exe"),
]


def _find_python312() -> Path | None:
    """
    Locate a Python 3.12 interpreter on the system.

    Search order:
      1. Hardcoded candidate paths (user-level and system-level installs).
      2. ``py -3.12`` launcher (if available on PATH).
      3. ``python3.12`` on PATH (Linux/macOS fallback).

    Returns the resolved Path to the interpreter, or None if not found.
    """
    # 1. Probe known Windows install locations
    for candidate in _PYTHON312_CANDIDATES:
        if candidate.is_file():
            return candidate.resolve()

    # 2. Try the Windows Python launcher (py.exe)
    try:
        result = subprocess.run(
            ["py", "-3.12", "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        if result.returncode == 0:
            path = Path(result.stdout.strip())
            if path.is_file():
                return path.resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 3. Try python3.12 directly (Unix-style)
    try:
        result = subprocess.run(
            ["python3.12", "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            path = Path(result.stdout.strip())
            if path.is_file():
                return path.resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def _verify_python312(python_path: Path) -> bool:
    """Confirm the given interpreter is actually Python 3.12.x."""
    try:
        result = subprocess.run(
            [str(python_path), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        return result.stdout.strip() == "3.12"
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return False


# ---------------------------------------------------------------------------
# ANSI colors for terminal output
# ---------------------------------------------------------------------------
class _C:
    """ANSI color codes (disabled when not a TTY)."""
    RESET   = "\033[0m"   if sys.stdout.isatty() else ""
    BOLD    = "\033[1m"    if sys.stdout.isatty() else ""
    DIM     = "\033[2m"    if sys.stdout.isatty() else ""
    RED     = "\033[31m"   if sys.stdout.isatty() else ""
    GREEN   = "\033[32m"   if sys.stdout.isatty() else ""
    YELLOW  = "\033[33m"   if sys.stdout.isatty() else ""
    BLUE    = "\033[34m"   if sys.stdout.isatty() else ""
    MAGENTA = "\033[35m"   if sys.stdout.isatty() else ""
    CYAN    = "\033[36m"   if sys.stdout.isatty() else ""

# Process tracking
_backend_proc: subprocess.Popen | None = None
_frontend_proc: subprocess.Popen | None = None
_shutdown_event = threading.Event()

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def _log(color: str, tag: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{_C.DIM}{ts}{_C.RESET} {color}{_C.BOLD}[{tag}]{_C.RESET} {msg}", flush=True)

def _info(msg: str) -> None:
    _log(_C.CYAN, "STARTUP", msg)

def _ok(msg: str) -> None:
    _log(_C.GREEN, "  OK  ", msg)

def _warn(msg: str) -> None:
    _log(_C.YELLOW, "WARN ", msg)

def _err(msg: str) -> None:
    _log(_C.RED, "ERROR", msg)

def _phase(msg: str) -> None:
    print(f"\n{_C.BOLD}{_C.MAGENTA}{'=' * 60}{_C.RESET}")
    _log(_C.MAGENTA, ">>>>>", msg)
    print(f"{_C.BOLD}{_C.MAGENTA}{'=' * 60}{_C.RESET}\n", flush=True)

# ---------------------------------------------------------------------------
# Stream reader thread -- pipes subprocess output into the main console
# ---------------------------------------------------------------------------
def _stream_output(proc: subprocess.Popen, tag: str, color: str) -> None:
    """Read stdout/stderr from a subprocess and print with a tag prefix."""
    assert proc.stdout is not None
    assert proc.stderr is not None

    def _reader(pipe, prefix: str):
        try:
            for raw_line in iter(pipe.readline, b""):
                if _shutdown_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    ts = time.strftime("%H:%M:%S")
                    print(f"{_C.DIM}{ts}{_C.RESET} {color}{prefix}{_C.RESET} {line}", flush=True)
        except (OSError, ValueError):
            pass  # pipe closed during shutdown

    t_out = threading.Thread(target=_reader, args=(proc.stdout, f"[{tag}]"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, f"[{tag}]"), daemon=True)
    t_out.start()
    t_err.start()

# ---------------------------------------------------------------------------
# STEP 1: Backend virtual environment (Python 3.12 enforced)
# ---------------------------------------------------------------------------
_REQ_HASH_FILE = VENV_DIR / ".requirements-hash"


def _req_file_hash() -> str:
    """Return SHA-256 hash of requirements.txt content (or empty string if missing)."""
    if not REQ_FILE.is_file():
        return ""
    import hashlib
    return hashlib.sha256(REQ_FILE.read_bytes()).hexdigest()


def _install_backend_deps(venv_python: Path) -> bool:
    """Install/upgrade backend dependencies from requirements.txt into the venv."""
    if not REQ_FILE.is_file():
        _info("No requirements.txt found -- skipping dependency install")
        return True

    _info("Upgrading pip...")
    try:
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        _ok("pip upgraded")
    except subprocess.CalledProcessError:
        _warn("pip upgrade failed -- continuing with existing version")

    _info("Installing backend dependencies from requirements.txt")
    try:
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-r", str(REQ_FILE)],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        _ok("Backend dependencies installed")
        _REQ_HASH_FILE.write_text(_req_file_hash(), encoding="utf-8")
        return True
    except subprocess.CalledProcessError as exc:
        _err(f"Failed to install dependencies: {exc}")
        return False


def _ensure_backend_venv(python312: Path) -> bool:
    _phase("STEP 1: Backend Environment")

    venv_python = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    if venv_python.is_file():
        try:
            result = subprocess.run(
                [str(venv_python), "-c",
                 "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            version = result.stdout.strip()
            if version != "3.12":
                _warn(f"Existing venv uses Python {version} -- recreating with Python 3.12")
                import shutil
                shutil.rmtree(str(VENV_DIR))
            else:
                _ok(f"Virtual environment found at .venv (Python {version})")
                # Check whether requirements.txt has changed since last install
                if REQ_FILE.is_file() and _REQ_HASH_FILE.is_file():
                    old_hash = _REQ_HASH_FILE.read_text(encoding="utf-8").strip()
                    if old_hash == _req_file_hash():
                        _ok("Dependencies up to date")
                        return True
                    _info("requirements.txt changed -- reinstalling dependencies")
                elif REQ_FILE.is_file():
                    _info("requirements.txt present but never installed -- installing")
                else:
                    return True  # No requirements file, nothing to do
                return _install_backend_deps(venv_python)
        except Exception:
            _warn("Could not verify existing venv -- recreating")
            import shutil
            shutil.rmtree(str(VENV_DIR), ignore_errors=True)

    # Create venv using the discovered Python 3.12
    _info(f"Creating virtual environment with {python312}")
    try:
        subprocess.run(
            [str(python312), "-m", "venv", str(VENV_DIR)],
            check=True,
            cwd=str(BACKEND_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        _ok("Virtual environment created")
    except subprocess.CalledProcessError as exc:
        _err(f"Failed to create virtual environment: {exc}")
        return False
    # Always delete _REQ_HASH_FILE before calling _install_backend_deps()
    try:
        _REQ_HASH_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    return _install_backend_deps(venv_python)


def _check_node() -> None:
    """Pre-flight check to ensure npm is on PATH and Node.js version is appropriate."""
    try:
        result = subprocess.run(
            ["npm", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=IS_WINDOWS,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, result.args, output=result.stdout, stderr=result.stderr)
        
        version_str = result.stdout.strip()
        cleaned = version_str.lstrip("v")
        if cleaned:
            parts = cleaned.split(".")
            if parts and parts[0].isdigit():
                major_version = int(parts[0])
                if major_version < 20:
                    _warn("npm reports Node.js < v20. Transfera requires v20+.")
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        _err("=" * 56)
        _err("")
        _err("  Node.js v20+ is required")
        _err("  Download URL: https://nodejs.org/")
        _err("  Please restart your terminal after installing")
        _err("")
        _err("=" * 56)
        sys.exit(1)


# ---------------------------------------------------------------------------
# STEP 2: Frontend dependencies
# ---------------------------------------------------------------------------
def _ensure_frontend_deps() -> bool:
    _phase("STEP 2: Frontend Dependencies")

    if NODE_MODULES.is_dir():
        _ok(f"node_modules found at {NODE_MODULES.relative_to(ROOT_DIR)}")
        return True

    _warn("node_modules not found -- running npm install")
    try:
        subprocess.run(
            ["npm", "install"],
            check=True,
            cwd=str(FRONTEND_DIR),
            shell=IS_WINDOWS,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        _ok("Frontend dependencies installed")
    except subprocess.CalledProcessError:
        _err("npm install failed. Check the output above for details.")
        _err("Common fixes: run your terminal as Administrator, or delete")
        _err("        frontend/node_modules and retry.")
        return False

    return True

# ---------------------------------------------------------------------------
# STEP 2.5: Build compiled frontend assets (if not present)
# ---------------------------------------------------------------------------
def _build_frontend() -> bool:
    """Build the React frontend into frontend/dist/ if it does not exist."""
    if FRONTEND_DIST.is_dir():
        # Verify dist has actual content (index.html + assets)
        index = FRONTEND_DIST / "index.html"
        assets = FRONTEND_DIST / "assets"
        if index.is_file() and assets.is_dir():
            _ok(f"Frontend dist found at {FRONTEND_DIST.relative_to(ROOT_DIR)}")
            return True
        _warn("Frontend dist directory exists but appears incomplete -- rebuilding")

    _phase("STEP 2.5: Building Frontend")
    _info("Compiling React frontend (npm run build)...")

    try:
        subprocess.run(
            ["npm", "run", "build"],
            check=True,
            cwd=str(FRONTEND_DIR),
            shell=IS_WINDOWS,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        if not (FRONTEND_DIST / "index.html").is_file():
            _err("Build completed but frontend/dist/index.html not found")
            return False
        _ok("Frontend compiled successfully")
        return True
    except subprocess.CalledProcessError as exc:
        _err(f"Failed to build frontend: {exc}")
        return False

# ---------------------------------------------------------------------------
# STEP 2.6: Build native wpd_helper.exe
# ---------------------------------------------------------------------------
def _native_src_files() -> list[Path]:
    """Return all source files that wpd_helper.exe depends on."""
    files: list[Path] = []
    cpp = NATIVE_WPD_DIR / "wpd_helper.cpp"
    if cpp.is_file():
        files.append(cpp)
    bat = NATIVE_WPD_DIR / "build.bat"
    if bat.is_file():
        files.append(bat)
    return files


def _native_is_stale() -> bool:
    """Return True when the existing exe is missing or older than any source file."""
    if not WPD_HELPER_EXE.is_file():
        return True
    exe_mtime = WPD_HELPER_EXE.stat().st_mtime
    for src in _native_src_files():
        if src.stat().st_mtime > exe_mtime:
            return True
    return False


def _build_native() -> bool:
    """Build wpd_helper.exe from native/wpd_helper/ source.

    Always attempts the build (the underlying build.bat also has its own
    staleness check).  This must run BEFORE the backend is started to
    avoid a race where the backend's device-probe subprocess locks the
    exe while the linker tries to overwrite it.
    """
    if not IS_WINDOWS:
        _ok("Skipping native build (not Windows)")
        return True

    if not NATIVE_WPD_DIR.is_dir():
        _warn(f"Native source directory not found: {NATIVE_WPD_DIR}")
        return False

    if not _native_is_stale():
        _ok(f"wpd_helper.exe up to date ({WPD_HELPER_EXE.relative_to(ROOT_DIR)})")
        return True

    _phase("STEP 2.6: Building native wpd_helper")
    build_bat = NATIVE_WPD_DIR / "build.bat"
    if not build_bat.is_file():
        _err(f"Build script not found: {build_bat}")
        return False

    try:
        result = subprocess.run(
            [str(build_bat)],
            cwd=str(NATIVE_WPD_DIR),
            capture_output=True,
            text=True,
            timeout=60,
            shell=IS_WINDOWS,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        # Always show build output so incremental-skip messages are visible.
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                _info(f"  {line}")
        if result.returncode != 0:
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            _err(f"wpd_helper build failed (exit {result.returncode})")
            if "LNK1104" in combined:
                _err(
                    f"  ↳ Cannot overwrite {WPD_HELPER_EXE.name} -- "
                    "another process may still have it locked."
                )
                _err(
                    "    Close any running Transfera backend or device-probe "
                    "process and retry.  If the problem persists, check for "
                    "antivirus or file-sync tools (OneDrive, Dropbox) that "
                    "may briefly lock freshly-written executables."
                )
            else:
                if result.stdout:
                    _err(result.stdout.strip())
                if result.stderr:
                    _err(result.stderr.strip())
            return False
        if not WPD_HELPER_EXE.is_file():
            _err("Build completed but wpd_helper.exe not found")
            return False
        _ok("wpd_helper.exe built successfully")
        return True
    except subprocess.TimeoutExpired:
        _err("wpd_helper build timed out")
        return False
    except OSError as exc:
        _err(f"Failed to run build.bat: {exc}")
        return False


# ---------------------------------------------------------------------------
# STEP 3: Concurrent process launch
# ---------------------------------------------------------------------------
def _python_bin() -> str:
    """Return the Python executable inside the .venv -- never fall back to system Python."""
    venv_py = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    if venv_py.is_file():
        return str(venv_py)
    # This should never happen if _ensure_backend_venv succeeded, but be explicit
    _err("Cannot find .venv Python -- the venv may be corrupted")
    sys.exit(1)


def _launch_backend() -> subprocess.Popen | None:
    _info(f"Starting FastAPI backend on port {BACKEND_PORT}...")
    cmd = [
        _python_bin(),
        "-m", "uvicorn",
        "backend.main:app",
        "--host", "127.0.0.1",
        "--port", str(BACKEND_PORT),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
        )
        _ok(f"Backend process started (PID {proc.pid})")
        return proc
    except OSError as exc:
        _err(f"Failed to start backend: {exc}")
        return None


def _wait_for_backend_readiness(proc: subprocess.Popen, port: int) -> None:
    """Poll /api/health every 0.5s until the backend responds or 30s elapses.

    If the backend process dies during the wait, report the crash and return
    immediately.  On timeout the app continues running (the backend may still
    become ready later).
    """
    _info(f"Waiting for http://127.0.0.1:{port}/api/health ...")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            code = proc.returncode
            _err(f"Backend process exited (code {code}) while waiting for readiness")
            return
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.0,
            )
            if resp.status == 200:
                _ok(f"Backend is responding on http://127.0.0.1:{port}")
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)

    _err(
        f"Backend process (PID {proc.pid}) is alive but not responding on "
        f"http://127.0.0.1:{port}/api/health after 30s"
    )
    _err(
        "Check the [BACKEND] log lines above for what it may be stuck on. "
        "The application will continue running in case startup completes slowly."
    )


def _launch_frontend_dev() -> subprocess.Popen | None:
    _info(f"Starting Electron dev shell (Vite + Electron on port {VITE_PORT})...")
    cmd = ["npm", "run", "electron:dev"]
    env = {**os.environ, "TRANSFERA_EXTERNAL_BACKEND": "1"}
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(FRONTEND_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=IS_WINDOWS,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
        )
        _ok(f"Electron dev process started (PID {proc.pid})")
        return proc
    except OSError as exc:
        _err(f"Failed to start Electron dev shell: {exc}")
        return None


# ---------------------------------------------------------------------------
# Process termination -- verified, tree-aware, with Windows WMI sweep
# ---------------------------------------------------------------------------
def _get_transfera_processes(exclude_pid: int | None = None) -> list[dict]:
    """Query WMI for Transfera-related processes on Windows.

    Returns a list of {ProcessId, Name, CommandLine} dicts, or [] on
    non-Windows or query failure.
    """
    if not IS_WINDOWS:
        return []

    root = str(ROOT_DIR)
    exclude = exclude_pid or 0

    ps = (
        'Get-CimInstance Win32_Process -Property ProcessId,Name,CommandLine | '
        'Where-Object { '
        f'$_.ProcessId -ne {exclude} -and $_.CommandLine -and '
        '$_.Name -ne "powershell.exe" -and '
        f'( $_.CommandLine -like "*{root}*" -or '
        f'$_.CommandLine -like "*uvicorn*backend.main*" ) '
        '} | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json'
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            if isinstance(data, dict):
                data = [data]
            return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    return []


def _sweep_remaining(label: str) -> bool:
    """Find and forcefully kill any remaining Transfera-related processes.

    Returns True if all processes are gone after the sweep,
    False if some could not be terminated.
    """
    own_pid = os.getpid()

    for attempt in range(3):
        remaining = _get_transfera_processes(exclude_pid=own_pid)
        if not remaining:
            return True

        if attempt == 0:
            _warn(f"Found {len(remaining)} remaining {label} process(es)")
            for r in remaining:
                cmd = (r.get("CommandLine") or "")[:100]
                _info(f"  PID {r['ProcessId']} ({r.get('Name', '?')}): {cmd}")

        for r in remaining:
            pid = r.get("ProcessId")
            if pid and pid != own_pid:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
                )

        time.sleep(1)

    remaining = _get_transfera_processes(exclude_pid=own_pid)
    if remaining:
        # Fallback: kill by image name for known stubborn processes
        for img in ("electron.exe", "node.exe", "python.exe"):
            if img == "python.exe":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/FI", f"COMMANDLINE eq *{ROOT_DIR}*", "/IM", "python.exe"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
                    )
                except Exception:
                    pass
            else:
                subprocess.run(
                    ["taskkill", "/F", "/IM", img],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
                )
        time.sleep(1)
        remaining = _get_transfera_processes(exclude_pid=own_pid)

    if remaining:
        _err(f"{len(remaining)} {label} process(es) could not be terminated:")
        for r in remaining:
            _err(f"  PID {r['ProcessId']} ({r.get('Name', '?')})")
        return False
    return True


def _terminate_process_tree(proc: subprocess.Popen | None, label: str) -> None:
    """Kill a tracked process tree and confirm exit before returning."""
    if proc is None or proc.poll() is not None:
        return

    pid = proc.pid
    _info(f"Stopping {label} (PID {pid})...")

    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    else:
        os.killpg(os.getpgid(pid), signal.SIGTERM)

    try:
        proc.wait(timeout=5)
        _info(f"{label} main process exited")
    except subprocess.TimeoutExpired:
        _warn(f"{label} (PID {pid}) did not exit -- terminating individually")
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (ProcessLookupError, OSError):
            pass


def _shutdown(sig=None, _frame=None) -> None:
    """Handle SIGINT / Ctrl+C -- comprehensive process cleanup with WMI sweep."""
    if _shutdown_event.is_set():
        return
    _shutdown_event.set()

    print()
    _phase("TEARDOWN: Shutting down Transfera stack")

    _terminate_process_tree(_backend_proc, "Backend")
    _terminate_process_tree(_frontend_proc, "Frontend")

    if IS_WINDOWS:
        _sweep_remaining("Transfera")

    _ok("All processes stopped -- goodbye!")
    sys.exit(0)


def _check_stray_processes() -> None:
    """Warn about and auto-terminate leftover Transfera processes from prior sessions."""
    if not IS_WINDOWS:
        return

    strays = _get_transfera_processes(exclude_pid=os.getpid())
    if strays:
        _warn(f"Found {len(strays)} leftover process(es) from a previous session:")
        for s in strays:
            cmd = (s.get("CommandLine") or "")[:120]
            _warn(f"  PID {s['ProcessId']} ({s.get('Name', '?')}): {cmd}")
        _info("Auto-terminating before starting new processes...")
        if not _sweep_remaining("stray"):
            _err("Could not clear all stray processes -- continuing anyway")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Transfera development stack orchestrator")
    parser.add_argument("--backend", action="store_true", help="Start backend only")
    parser.add_argument("--frontend", action="store_true", help="Start Vite dev server only")
    parser.add_argument("--skip-deps", action="store_true", help="Skip dependency checks")
    args = parser.parse_args()

    start_backend = not args.frontend
    start_frontend = not args.backend

    # Check for leftover processes from previous sessions before launching
    _check_stray_processes()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _shutdown)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, _shutdown)

    print(f"""
{_C.BOLD}{_C.CYAN}
    +------------------------------------------+
    |          Transfera v2  Dev Stack           |
    |     Backend : http://127.0.0.1:{BACKEND_PORT}      |
    |     Frontend: http://127.0.0.1:{BACKEND_PORT}      |
    |     Electron: Vite + Electron (dev)   |
    +------------------------------------------+
{_C.RESET}""")

    # -----------------------------------------------------------------------
    # Python 3.12 enforcement (must happen before any venv/pip work)
    # -----------------------------------------------------------------------
    python312: Path | None = None

    if start_backend and not args.skip_deps:
        _phase("PYTHON: Locating Python 3.12 interpreter")
        python312 = _find_python312()

        if python312 is None:
            _err("=" * 56)
            _err("")
            _err("  Transfera requires Python 3.12 to fetch stable")
            _err("  pre-compiled wheels. Please ensure Python 3.12 is")
            _err("  selected or installed.")
            _err("")
            _err("  Expected locations checked:")
            for c in _PYTHON312_CANDIDATES:
                _err(f"    - {c}")
            _err("")
            _err("  Download: https://www.python.org/downloads/")
            _err("")
            _err("=" * 56)
            sys.exit(1)

        if not _verify_python312(python312):
            _err(f"Found interpreter at {python312} but it is NOT Python 3.12")
            sys.exit(1)

        _ok(f"Python 3.12 located at {python312}")

    # STEP 1 & 2: Dependency checks
    if not args.skip_deps:
        if start_backend and not _ensure_backend_venv(python312):
            sys.exit(1)
        if start_frontend:
            _check_node()
        if start_frontend and not _ensure_frontend_deps():
            sys.exit(1)
    else:
        _info("Skipping dependency checks (--skip-deps)")

    # STEP 2.5: Build frontend if serving through FastAPI
    if start_backend:
        if not _build_frontend():
            _warn("Frontend build failed -- backend will run in API-only mode")

    # STEP 2.6: Build native wpd_helper
    if start_backend:
        _build_native()

    # STEP 3: Launch processes
    _phase("STEP 3: Launching Services")

    global _backend_proc, _frontend_proc

    if start_backend:
        _backend_proc = _launch_backend()
        if _backend_proc is None:
            _err("Cannot continue without backend -- exiting")
            sys.exit(1)
        _stream_output(_backend_proc, "BACKEND ", _C.BLUE)
        _wait_for_backend_readiness(_backend_proc, BACKEND_PORT)

    if start_frontend:
        _frontend_proc = _launch_frontend_dev()
        if _frontend_proc is None:
            _err("Cannot continue without frontend -- exiting")
            _terminate_process_tree(_backend_proc, "Backend")
            if IS_WINDOWS:
                _sweep_remaining("Transfera")
            sys.exit(1)
        _stream_output(_frontend_proc, "FRONTEND", _C.GREEN)

    # Wait for readiness
    _phase("WAITING: Services starting up...")
    _info("Press Ctrl+C to stop all services\n")

    # Poll for process exit (detects crashes)
    try:
        while True:
            if _backend_proc and _backend_proc.poll() is not None:
                code = _backend_proc.returncode
                _err(f"Backend exited unexpectedly (code {code})")
                _terminate_process_tree(_frontend_proc, "Frontend")
                if IS_WINDOWS:
                    _sweep_remaining("Transfera")
                sys.exit(code or 1)

            if _frontend_proc and _frontend_proc.poll() is not None:
                code = _frontend_proc.returncode
                if code == 0:
                    _shutdown()
                else:
                    _err(f"Frontend exited unexpectedly (code {code})")
                    _terminate_process_tree(_backend_proc, "Backend")
                    if IS_WINDOWS:
                        _sweep_remaining("Transfera")
                    sys.exit(code)

            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
