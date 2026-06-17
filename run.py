#!/usr/bin/env python3
"""
MediaVault v2 -- Development Stack Orchestrator
Single-command startup for the full backend + frontend development environment.

Usage:
    python run.py              # Start everything
    python run.py --backend    # Backend only
    python run.py --frontend   # Frontend only
    python run.py --skip-deps  # Skip dependency checks
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"
VENV_DIR = BACKEND_DIR / "venv"
NODE_MODULES = FRONTEND_DIR / "node_modules"
REQ_FILE = BACKEND_DIR / "requirements.txt"

BACKEND_PORT = 47821
VITE_PORT = 5173

IS_WINDOWS = sys.platform == "win32"

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
# STEP 1: Backend virtual environment
# ---------------------------------------------------------------------------
def _ensure_backend_venv() -> bool:
    _phase("STEP 1: Backend Environment")

    if VENV_DIR.is_dir():
        _ok(f"Virtual environment found at {VENV_DIR.relative_to(ROOT_DIR)}")
        return True

    _warn("Virtual environment not found -- creating one")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
            cwd=str(BACKEND_DIR),
        )
        _ok("Virtual environment created")
    except subprocess.CalledProcessError as exc:
        _err(f"Failed to create virtual environment: {exc}")
        return False

    if REQ_FILE.is_file():
        _info("Installing backend dependencies from requirements.txt")
        pip = VENV_DIR / ("Scripts/pip.exe" if IS_WINDOWS else "bin/pip")
        try:
            subprocess.run(
                [str(pip), "install", "-r", str(REQ_FILE)],
                check=True,
                cwd=str(BACKEND_DIR),
            )
            _ok("Backend dependencies installed")
        except subprocess.CalledProcessError as exc:
            _err(f"Failed to install dependencies: {exc}")
            return False
    else:
        _warn("No requirements.txt found -- skipping dependency install")

    return True

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
        )
        _ok("Frontend dependencies installed")
    except subprocess.CalledProcessError as exc:
        _err(f"Failed to install frontend dependencies: {exc}")
        return False

    return True

# ---------------------------------------------------------------------------
# STEP 3: Concurrent process launch
# ---------------------------------------------------------------------------
def _python_bin() -> str:
    """Return the Python executable inside the backend venv, falling back to system Python."""
    venv_py = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    if venv_py.is_file():
        return str(venv_py)
    # Fallback: use the Python that invoked this script
    return sys.executable


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


def _launch_frontend() -> subprocess.Popen | None:
    _info(f"Starting Vite dev server on port {VITE_PORT}...")
    cmd = ["npm", "run", "dev"]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(FRONTEND_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=IS_WINDOWS,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
        )
        _ok(f"Frontend process started (PID {proc.pid})")
        return proc
    except OSError as exc:
        _err(f"Failed to start frontend: {exc}")
        return None


# ---------------------------------------------------------------------------
# Graceful teardown
# ---------------------------------------------------------------------------
def _kill_tree(proc: subprocess.Popen, label: str) -> None:
    """Terminate a process and its children gracefully, then force-kill if needed."""
    if proc is None or proc.poll() is not None:
        return

    _info(f"Stopping {label} (PID {proc.pid})...")

    try:
        if IS_WINDOWS:
            # taskkill /T kills the entire process tree; /F forces after grace
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Send SIGTERM to the process group
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass  # already dead

    try:
        proc.wait(timeout=5)
        _ok(f"{label} stopped")
    except subprocess.TimeoutExpired:
        _warn(f"{label} did not exit in time -- force killed")
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (ProcessLookupError, OSError):
            pass


def _shutdown(sig=None, _frame=None) -> None:
    """Handle SIGINT / Ctrl+C -- tear down both processes cleanly."""
    if _shutdown_event.is_set():
        return  # already shutting down
    _shutdown_event.set()

    print()  # newline after ^C
    _phase("TEARDOWN: Shutting down MediaVault stack")

    _kill_tree(_backend_proc, "Backend")
    _kill_tree(_frontend_proc, "Frontend")

    _ok("All processes stopped -- goodbye!")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="MediaVault development stack orchestrator")
    parser.add_argument("--backend", action="store_true", help="Start backend only")
    parser.add_argument("--frontend", action="store_true", help="Start frontend only")
    parser.add_argument("--skip-deps", action="store_true", help="Skip dependency checks")
    args = parser.parse_args()

    start_backend = not args.frontend
    start_frontend = not args.backend

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _shutdown)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, _shutdown)

    print(f"""
{_C.BOLD}{_C.CYAN}
    +------------------------------------------+
    |          MediaVault v2  Dev Stack         |
    |     Backend : http://127.0.0.1:{BACKEND_PORT}      |
    |     Frontend: http://127.0.0.1:{VITE_PORT}       |
    +------------------------------------------+
{_C.RESET}""")

    # STEP 1 & 2: Dependency checks
    if not args.skip_deps:
        if start_backend and not _ensure_backend_venv():
            sys.exit(1)
        if start_frontend and not _ensure_frontend_deps():
            sys.exit(1)
    else:
        _info("Skipping dependency checks (--skip-deps)")

    # STEP 3: Launch processes
    _phase("STEP 3: Launching Services")

    global _backend_proc, _frontend_proc

    if start_backend:
        _backend_proc = _launch_backend()
        if _backend_proc is None:
            _err("Cannot continue without backend -- exiting")
            sys.exit(1)
        _stream_output(_backend_proc, "BACKEND ", _C.BLUE)

    if start_frontend:
        _frontend_proc = _launch_frontend()
        if _frontend_proc is None:
            _err("Cannot continue without frontend -- exiting")
            _kill_tree(_backend_proc, "Backend")
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
                _kill_tree(_frontend_proc, "Frontend")
                sys.exit(code or 1)

            if _frontend_proc and _frontend_proc.poll() is not None:
                code = _frontend_proc.returncode
                _err(f"Frontend exited unexpectedly (code {code})")
                _kill_tree(_backend_proc, "Backend")
                sys.exit(code or 1)

            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
