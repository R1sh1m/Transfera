#!/usr/bin/env python3
"""
Transfera v2 — Development Stack Orchestrator
Single-command startup for the full backend + frontend development environment.

Enforces Python 3.12 for stable pre-compiled wheel availability (pillow-heif, blake3).

Usage:
    python run.py              # Start everything (backend + Tauri dev shell)
    python run.py --backend    # Backend only (serves compiled frontend)
    python run.py --frontend   # Tauri dev shell only (adopts an already-running backend)
    python run.py --tauri      # Start backend + Tauri dev shell (recommended full-feature local test)
    python run.py --reload     # Auto-reload backend on Python changes (dev only, combines with others)
    python run.py --no-build   # Skip frontend dist rebuild (Tauri dev uses Vite HMR)
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
    *(
        [Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python" / "Python312" / "python.exe"]
        if IS_WINDOWS and "LOCALAPPDATA" in os.environ
        else []
    ),
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

    RESET = "\033[0m" if sys.stdout.isatty() else ""
    BOLD = "\033[1m" if sys.stdout.isatty() else ""
    DIM = "\033[2m" if sys.stdout.isatty() else ""
    RED = "\033[31m" if sys.stdout.isatty() else ""
    GREEN = "\033[32m" if sys.stdout.isatty() else ""
    YELLOW = "\033[33m" if sys.stdout.isatty() else ""
    BLUE = "\033[34m" if sys.stdout.isatty() else ""
    MAGENTA = "\033[35m" if sys.stdout.isatty() else ""
    CYAN = "\033[36m" if sys.stdout.isatty() else ""


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
                [str(venv_python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
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
    """Pre-flight check to ensure Node.js v20+ is on PATH (fatal if missing or too old)."""
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=IS_WINDOWS,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )

        version_str = result.stdout.strip()  # e.g. "v20.11.0"
        cleaned = version_str.lstrip("v")
        major_s = cleaned.split(".")[0] if cleaned else ""
        if not major_s.isdigit():
            _err(f"Could not parse Node.js version from {version_str!r} -- expected e.g. v20.x.x")
            sys.exit(1)
        if int(major_s) < 20:
            _err(f"Node.js {version_str} detected -- Transfera requires Node.js v20+.")
            _err("  Download URL: https://nodejs.org/ (choose the LTS release)")
            _err("  Please restart your terminal after installing")
            sys.exit(1)
        _ok(f"Node.js {version_str} detected")
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
_PACKAGE_JSON = FRONTEND_DIR / "package.json"
_PACKAGE_LOCK = FRONTEND_DIR / "package-lock.json"
_NPM_HASH_FILE = NODE_MODULES / ".transfera-install-hash"


def _npm_files_hash() -> str:
    """SHA-256 over package.json + package-lock.json (lock file optional)."""
    import hashlib

    h = hashlib.sha256()
    for f in (_PACKAGE_JSON, _PACKAGE_LOCK):
        if f.is_file():
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def _npm_up_to_date() -> bool:
    """True when node_modules carries a hash matching the current manifests."""
    try:
        return _NPM_HASH_FILE.is_file() and _NPM_HASH_FILE.read_text(encoding="utf-8").strip() == _npm_files_hash()
    except OSError:
        return False


def _write_npm_hash() -> None:
    """Record the current manifest hash inside node_modules (best-effort)."""
    try:
        _NPM_HASH_FILE.write_text(_npm_files_hash(), encoding="utf-8")
    except OSError:
        pass


def _ensure_frontend_deps() -> bool:
    _phase("STEP 2: Frontend Dependencies")

    if NODE_MODULES.is_dir() and _npm_up_to_date():
        _ok(f"node_modules found at {NODE_MODULES.relative_to(ROOT_DIR)} (up to date)")
        return True

    if NODE_MODULES.is_dir():
        _info("package.json or package-lock.json changed -- reinstalling frontend dependencies")
    else:
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
        _write_npm_hash()
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
    return any(src.stat().st_mtime > exe_mtime for src in _native_src_files())


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
                _err(f"  ↳ Cannot overwrite {WPD_HELPER_EXE.name} -- another process may still have it locked.")
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
# STEP 2.7: Stage Tauri dev resources (frontend/src-tauri/resources/)
# ---------------------------------------------------------------------------
_TAURI_DIR = FRONTEND_DIR / "src-tauri"
_TAURI_RESOURCES_DIR = _TAURI_DIR / "resources"
_EXIFTOOL_PKG_DIR = BACKEND_DIR / "bin" / "exiftool"
_EXIFTOOL_DATA_DIR = BACKEND_DIR / "data" / "bin" / "exiftool"


def _resolve_exiftool_src() -> tuple[Path, Path]:
    """Return (exe_src, files_src) preferring release download dir, falling back to dev data dir.

    Release builds download into backend/bin/exiftool/ (PACKAGED_EXIFTOOL_DIR);
    dev runs keep the writable copy at backend/data/bin/exiftool/ (EXIFTOOL_DIR).
    """
    for base in (_EXIFTOOL_PKG_DIR, _EXIFTOOL_DATA_DIR):
        exe = base / "exiftool.exe"
        files = base / "exiftool_files"
        if exe.is_file():
            return exe, files
    return _EXIFTOOL_PKG_DIR / "exiftool.exe", _EXIFTOOL_PKG_DIR / "exiftool_files"


def _copy_if_newer(src: Path, dst: Path, label: str) -> bool:
    """Copy src -> dst when dst is missing or src is newer. Returns True if dst is current."""
    import shutil

    try:
        if dst.is_file() and src.is_file():
            if src.stat().st_mtime <= dst.stat().st_mtime:
                _info(f"{label} up to date ({dst.relative_to(ROOT_DIR)})")
                return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        _ok(f"{label} staged ({dst.relative_to(ROOT_DIR)})")
        return True
    except OSError as exc:
        _err(f"Failed to stage {label}: {exc}")
        return False


def _stage_tauri_dev_resources() -> bool:
    """Sync dev-required binaries into frontend/src-tauri/resources/.

    Always runs when the Tauri dev shell will start (orthogonal to
    --skip-deps/--no-build). Stages wpd_helper.exe + exiftool, ensures
    dev-unneeded large-asset dirs exist, and writes a 0-byte dev-only
    placeholder for VC_redist.x64.exe. Release workflow
    (.github/workflows/release.yml) stages the real files -- placeholders
    are dev-only and must never ship.
    """
    import shutil

    _info("Staging Tauri dev resources...")
    try:
        _TAURI_RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _err(f"Cannot create resources dir {_TAURI_RESOURCES_DIR}: {exc}")
        return False

    ok = True

    # 1. wpd_helper.exe (required -- built by _build_native into backend/bin/)
    wpd_dst = _TAURI_RESOURCES_DIR / "wpd_helper.exe"
    if not WPD_HELPER_EXE.is_file():
        _err(f"Missing {WPD_HELPER_EXE.relative_to(ROOT_DIR)} -- cannot stage Tauri resource.")
        _err("  Fix: run 'python run.py' without --skip-deps/--no-build so _build_native() runs,")
        _err("  or build native/wpd_helper/build.bat manually, then retry.")
        ok = False
    elif not _copy_if_newer(WPD_HELPER_EXE, wpd_dst, "wpd_helper.exe"):
        ok = False

    # 2. exiftool.exe + exiftool_files/ (required -- do not silently placeholder).
    # Source: backend/bin/exiftool/ in release, backend/data/bin/exiftool/ in dev.
    exif_src, _exiftool_files_src = _resolve_exiftool_src()
    exif_dst = _TAURI_RESOURCES_DIR / "exiftool.exe"
    if not exif_src.is_file():
        _err(
            f"Missing exiftool.exe (checked {_EXIFTOOL_PKG_DIR.relative_to(ROOT_DIR)} and {_EXIFTOOL_DATA_DIR.relative_to(ROOT_DIR)}) -- cannot stage Tauri resource."
        )
        _err("  Fix: restore backend/data/bin/exiftool/ (exiftool.exe + exiftool_files/) then retry.")
        ok = False
    elif not _copy_if_newer(exif_src, exif_dst, "exiftool.exe"):
        ok = False

    files_dst = _TAURI_RESOURCES_DIR / "exiftool_files"
    try:
        files_dst.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _err(f"Cannot create {files_dst}: {exc}")
        ok = False
    if not _exiftool_files_src.is_dir():
        _err(f"Missing {_exiftool_files_src.relative_to(ROOT_DIR)}/ -- cannot stage Tauri resource.")
        _err("  Fix: restore backend/data/bin/exiftool/exiftool_files/ then retry.")
        ok = False
    else:
        try:
            for src_file in sorted(_exiftool_files_src.rglob("*")):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(_exiftool_files_src)
                dst_file = files_dst / rel
                if dst_file.is_file() and src_file.stat().st_mtime <= dst_file.stat().st_mtime:
                    continue
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_file), str(dst_file))
            _ok(f"exiftool_files staged ({files_dst.relative_to(ROOT_DIR)})")
        except OSError as exc:
            _err(f"Failed to stage exiftool_files: {exc}")
            ok = False

    # 3. Dev-unneeded large assets: ensure dirs exist, do not require real files in dev.
    for dirname in ("transfera-engine", "models", "exiftool_files"):
        try:
            (_TAURI_RESOURCES_DIR / dirname).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _err(f"Cannot create resources/{dirname}/: {exc}")
            ok = False

    # 4. VC_redist.x64.exe: 0-byte dev-only placeholder (dev shell never runs
    #    the installer hook; Unix builds carry a 0-byte placeholder per
    #    scripts/build-sidecar.ps1 convention; release stages the real file).
    vc_dst = _TAURI_RESOURCES_DIR / "VC_redist.x64.exe"
    if not vc_dst.is_file():
        try:
            vc_dst.write_bytes(b"")
            _warn(
                "VC_redist.x64.exe missing -- wrote 0-byte dev-only placeholder (release workflow stages the real file)."
            )
        except OSError as exc:
            _err(f"Failed to write dev placeholder {vc_dst}: {exc}")
            ok = False

    if ok:
        _ok("Tauri dev resources staged")
    return ok


def _verify_tauri_resources() -> bool:
    """Fail-fast preflight: every bundle.resources entry must exist under src-tauri/.

    Reads frontend/src-tauri/tauri.conf.json bundle.resources, resolves each
    entry relative to frontend/src-tauri/, and verifies existence (dirs: must
    exist; files: must exist -- 0-byte placeholders allowed in dev for
    VC_redist.x64.exe / transfera-engine.exe with a warning). Returns True
    when all entries resolve, False otherwise. Must run BEFORE the Rust build
    so a missing resource fails fast instead of after a 9-min compile.
    """
    conf = _TAURI_DIR / "tauri.conf.json"
    try:
        raw = conf.read_text(encoding="utf-8")
    except OSError as exc:
        _err(f"Cannot read {conf.relative_to(ROOT_DIR)}: {exc}")
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _err(f"Cannot parse {conf.relative_to(ROOT_DIR)}: {exc}")
        return False

    resources = (data.get("bundle") or {}).get("resources") or []
    if not isinstance(resources, list) or not resources:
        _err(f"No bundle.resources list found in {conf.relative_to(ROOT_DIR)} -- cannot verify Tauri resources")
        return False

    missing: list[str] = []
    for entry in resources:
        if not isinstance(entry, str) or not entry.strip():
            continue
        target = (_TAURI_DIR / entry.strip()).resolve()
        if target.is_dir():
            continue
        if target.is_file():
            try:
                if target.stat().st_size == 0 and target.name in ("VC_redist.x64.exe", "transfera-engine.exe"):
                    _warn(f"Dev-only 0-byte placeholder in use: {entry} (release workflow stages the real file).")
            except OSError:
                pass
            continue
        missing.append(entry)

    if missing:
        _err("Missing Tauri bundle resources (fail-fast, before Rust build):")
        for entry in missing:
            _err(f"  - {entry}  (expected at {(_TAURI_DIR / entry).relative_to(ROOT_DIR)})")
        _err("  Fix: ensure _stage_tauri_dev_resources() ran, then retry.")
        return False

    _ok(f"Tauri resources verified ({len(resources)} bundle.resources entries)")
    return True


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


def _is_port_in_use(port: int) -> bool:
    """Return True if something on 127.0.0.1:port accepts a TCP connection."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _check_ports(start_backend: bool, start_frontend: bool) -> None:
    """Fail fast with an actionable message if a needed port is already occupied."""
    occupied: list[tuple[int, str]] = []
    if start_backend and _is_port_in_use(BACKEND_PORT):
        occupied.append((BACKEND_PORT, "backend (FastAPI)"))
    if start_frontend and _is_port_in_use(VITE_PORT):
        occupied.append((VITE_PORT, "frontend (Vite dev server)"))
    if not occupied:
        return
    for port, owner in occupied:
        _err(f"Port {port} is already in use -- cannot start {owner}.")
    _err("A previous run may not have shut down cleanly, or another app holds the port.")
    _err("Find the owner with:  netstat -ano | findstr :<port>")
    _err("Then stop it with:    taskkill /F /PID <pid>")
    _err("Or stop the other Transfera / dev server using that port and retry.")
    sys.exit(1)


def _launch_backend(reload: bool = False) -> subprocess.Popen | None:
    _info(f"Starting FastAPI backend on port {BACKEND_PORT}...")
    cmd = [
        _python_bin(),
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(BACKEND_PORT),
    ]
    if reload:
        cmd += [
            "--reload",
            "--reload-dir",
            "backend",
            "--reload-exclude",
            "backend/data",
            "--reload-exclude",
            "backend/bin",
            "--reload-exclude",
            "frontend/dist",
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
                f"http://127.0.0.1:{port}/api/health",
                timeout=1.0,
            )
            if resp.status == 200:
                _ok(f"Backend is responding on http://127.0.0.1:{port}")
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)

    _err(
        f"Backend process (PID {proc.pid}) is alive but not responding on http://127.0.0.1:{port}/api/health after 30s"
    )
    _err(
        "Check the [BACKEND] log lines above for what it may be stuck on. "
        "The application will continue running in case startup completes slowly."
    )


def _launch_tauri_dev() -> subprocess.Popen | None:
    """Start the Tauri dev shell (WebView2 + Vite HMR).

    The backend is managed by run.py, so the shell adopts it via its
    /api/health probe (TRANSFERA_EXTERNAL_BACKEND=1) instead of spawning
    the (not yet frozen) sidecar. Requires `cargo` + WebView2 runtime.
    """
    _info("Starting Tauri dev shell (WebView2 + Vite HMR)...")
    cmd = ["npm", "run", "tauri:dev"]
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
        _ok(f"Tauri dev process started (PID {proc.pid})")
        return proc
    except OSError as exc:
        _err(f"Failed to start Tauri dev shell: {exc}")
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
        "Get-CimInstance Win32_Process -Property ProcessId,Name,CommandLine | "
        "Where-Object { "
        f"$_.ProcessId -ne {exclude} -and $_.CommandLine -and "
        '$_.Name -ne "powershell.exe" -and '
        "( "
        '  ( $_.Name -eq "python.exe" -and ( '
        '      $_.CommandLine -like "*uvicorn*backend.main*" -or '
        '      $_.CommandLine -like "*backend.main:app*" -or '
        '      $_.CommandLine -like "*backend.main*" -or '
        '      $_.CommandLine -like "*run.py*" '
        "  ) ) -or "
        f'  ( $_.Name -eq "exiftool.exe" -and $_.CommandLine -like "*{root}*" ) -or '
        '  ( $_.Name -eq "wpd_helper.exe" ) -or '
        f'  ( $_.Name -eq "transfera.exe" -and $_.CommandLine -like "*{root}*" ) '
        ") "
        "} | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            if isinstance(data, dict):
                data = [data]
            # Exclude IDE language servers / editors that may host a python
            # under a matching path (e.g. .antigravity-ide, .vscode, jedi, pylance).
            _IDE_EXCLUDE = (
                ".antigravity-ide",
                ".vscode",
                "antigravity",
                "vscode",
                "jedi",
                "pylance",
                "ms-python",
                "language_server",
                "languageserver",
            )
            filtered: list[dict] = []
            for proc in data:
                cmdline = proc.get("CommandLine") or ""
                if any(marker in cmdline.lower() for marker in _IDE_EXCLUDE):
                    continue
                filtered.append(proc)
            return filtered
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
                cmd = (r.get("CommandLine") or "")[:300]
                _info(f"  PID {r['ProcessId']} ({r.get('Name', '?')}): {cmd}")

        for r in remaining:
            pid = r.get("ProcessId")
            if pid and pid != own_pid:
                result = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
                )
                if result.returncode != 0:
                    _warn(f"taskkill /PID {pid} failed (exit {result.returncode}) -- will retry")

        time.sleep(1)

    remaining = _get_transfera_processes(exclude_pid=own_pid)
    if remaining:
        _warn(f"{len(remaining)} {label} process(es) could not be terminated and will be left running:")
        for r in remaining:
            _warn(f"  PID {r['ProcessId']} ({r.get('Name', '?')})")
            _warn(f"    Manual cleanup: taskkill /F /PID {r['ProcessId']}")
        return False
    return True


def _terminate_process_tree(proc: subprocess.Popen | None, label: str) -> None:
    """Kill a tracked process tree and confirm exit before returning."""
    if proc is None or proc.poll() is not None:
        return

    pid = proc.pid
    _info(f"Stopping {label} (PID {pid})...")

    if IS_WINDOWS:
        result = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        if result.returncode != 0:
            _warn(f"taskkill /PID {pid} ({label}) failed (exit {result.returncode})")
    else:
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        if killpg and getpgid:
            killpg(getpgid(pid), signal.SIGTERM)

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
            cmd = (s.get("CommandLine") or "")[:300]
            _warn(f"  PID {s['ProcessId']} ({s.get('Name', '?')}): {cmd}")
        _info("Auto-terminating before starting new processes...")
        if not _sweep_remaining("stray"):
            _warn("Could not clear all stray processes -- continuing anyway")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Transfera development stack orchestrator")
    parser.add_argument("--backend", action="store_true", help="Start backend only")
    parser.add_argument(
        "--frontend", action="store_true", help="Start Tauri dev shell only (adopts an already-running backend)"
    )
    parser.add_argument(
        "--tauri", action="store_true", help="Start backend + Tauri dev shell (recommended full-feature local test)"
    )
    parser.add_argument("--reload", action="store_true", help="Auto-reload backend on Python changes (dev only)")
    parser.add_argument("--no-build", action="store_true", help="Skip frontend dist rebuild (Tauri dev uses Vite HMR)")
    parser.add_argument("--skip-deps", action="store_true", help="Skip dependency checks")
    args = parser.parse_args()

    if args.backend and args.frontend:
        _err("--backend and --frontend are mutually exclusive -- pass only one, or neither for the full stack")
        sys.exit(2)
    if args.tauri and (args.backend or args.frontend):
        _err("--tauri cannot be combined with --backend/--frontend -- it starts backend + Tauri shell together")
        sys.exit(2)

    start_backend = not args.frontend
    start_frontend = not args.backend
    if args.tauri:
        # Explicit: --tauri means backend + Tauri dev shell (same as default no-flags).
        start_backend = True
        start_frontend = True

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
    |     Shell   : Tauri dev (WebView2 + Vite HMR) |
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
        if start_backend:
            assert python312 is not None
            if not _ensure_backend_venv(python312):
                sys.exit(1)
        if start_frontend:
            _check_node()
        if start_frontend and not _ensure_frontend_deps():
            sys.exit(1)
    else:
        _info("Skipping dependency checks (--skip-deps)")

    # STEP 2.5: Build frontend if serving through FastAPI
    # (skipped with --no-build: Tauri dev uses Vite HMR via devUrl, so dist
    # is unnecessary; backend-only mode still needs dist, hence build by default)
    if start_backend and not args.no_build:
        if not _build_frontend():
            _warn("Frontend build failed -- backend will run in API-only mode")
    elif args.no_build:
        _info("Skipping frontend dist build (--no-build)")

    # STEP 2.6: Build native wpd_helper
    if start_backend:
        _build_native()

    # STEP 2.7: Stage + verify Tauri dev resources (always when frontend starts,
    # orthogonal to --skip-deps/--no-build -- must run before the Rust build).
    if start_frontend:
        if not _stage_tauri_dev_resources():
            _err("Tauri resource staging failed -- fix the missing binaries above and retry.")
            sys.exit(1)
        if not _verify_tauri_resources():
            _err("Tauri resource preflight failed -- fix the missing entries above and retry.")
            sys.exit(1)

    # STEP 3: Launch processes (fail fast if a needed port is already taken)
    _check_ports(start_backend, start_frontend)
    _phase("STEP 3: Launching Services")

    global _backend_proc, _frontend_proc

    if start_backend:
        _backend_proc = _launch_backend(reload=args.reload)
        if _backend_proc is None:
            _err("Cannot continue without backend -- exiting")
            sys.exit(1)
        _stream_output(_backend_proc, "BACKEND ", _C.BLUE)
        _wait_for_backend_readiness(_backend_proc, BACKEND_PORT)

    if start_frontend:
        _frontend_proc = _launch_tauri_dev()
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
