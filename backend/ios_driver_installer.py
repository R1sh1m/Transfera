"""
Transfera v2 — Apple Mobile Device Support Auto-Installer

Handles detection of winget availability, verification of the
Apple.AppleMobileDeviceSupport package, and provides the information
needed by the Electron shell to trigger an elevated install.

The actual elevated execution is handled by the Electron main process
via ShellExecuteW with the "runas" verb — this module only does the
non-privileged checks and returns the command to run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_WINGET_ALREADY_INSTALLED = -2147009517  # 0x8A150011 — "already installed" is a success

APPLE_DRIVER_PACKAGE_ID = "Apple.AppleMobileDeviceSupport"


@dataclass
class InstallerStatus:
    winget_available: bool
    winget_version: str | None
    driver_status: str  # from check_driver_status()


@dataclass
class PackageVerification:
    success: bool
    package_id: str | None
    package_name: str | None
    version: str | None
    error: str | None


def _run_command(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run a command with CREATE_NO_WINDOW on Windows."""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creation_flags,
    )


def check_winget_available() -> tuple[bool, str | None]:
    """
    Check whether winget is available on the system.

    Returns (available, version_string).
    """
    try:
        result = _run_command(["winget", "--version"], timeout=10)
        if result.returncode == 0:
            version = result.stdout.strip()
            logger.info("winget available: %s", version)
            return True, version
        logger.warning("winget --version returned exit code %d", result.returncode)
        return False, None
    except FileNotFoundError:
        logger.info("winget not found on PATH")
        return False, None
    except subprocess.TimeoutExpired:
        logger.warning("winget --version timed out")
        return False, None
    except Exception as exc:
        logger.warning("Failed to check winget: %s", exc)
        return False, None


async def check_winget_available_async() -> tuple[bool, str | None]:
    """Async variant that does not block the event loop. See check_winget_available()."""
    try:
        result = await asyncio.to_thread(_run_command, ["winget", "--version"], 10)
        if result.returncode == 0:
            version = result.stdout.strip()
            logger.info("winget available: %s", version)
            return True, version
        logger.warning("winget --version returned exit code %d", result.returncode)
        return False, None
    except FileNotFoundError:
        logger.info("winget not found on PATH")
        return False, None
    except subprocess.TimeoutExpired:
        logger.warning("winget --version timed out")
        return False, None
    except Exception as exc:
        logger.warning("Failed to check winget: %s", exc)
        return False, None


def _find_winget() -> str:
    """Locate winget.exe on PATH or via LOCALAPPDATA fallback."""
    w = shutil.which("winget")
    if w:
        return w
    fallback = os.path.expandvars(
        "%LOCALAPPDATA%\\Microsoft\\WindowsApps\\winget.exe"
    )
    if os.path.isfile(fallback):
        return fallback
    raise FileNotFoundError("winget not found on PATH or in WindowsApps")


def build_install_args(version: str | None = None) -> list[str]:
    """Build winget install argument list.

    ``--version`` and ``--exact`` are only included when *version* is a
    non-empty string so that ``None`` / empty / whitespace-only values
    never end up as positional arguments in a downstream PowerShell call.
    """
    args: list[str] = [
        "install",
        "-e",
        "--id", APPLE_DRIVER_PACKAGE_ID,
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--silent",
    ]
    if version:
        args.extend(["--exact", "--version", version])
    return args


def install_driver(version: str | None = None, timeout: int = 180) -> dict:
    """Install Apple Mobile Device Support via winget (direct subprocess).

    Calls winget directly — **no** PowerShell ``Start-Process`` wrapper.
    Every line of stdout/stderr is streamed to the Python logger so
    progress is visible in the backend logs.

    The winget exit code ``0x8A150011`` (already installed) is treated
    as success, not failure.

    Returns a dict with keys ``success``, ``exit_code``, ``error``.
    """
    try:
        winget_path = _find_winget()
    except FileNotFoundError as exc:
        logger.error("winget not found: %s", exc)
        return {"success": False, "exit_code": None, "error": str(exc)}

    args = build_install_args(version)
    full_cmd = [winget_path] + args
    logger.info("Running winget install: %s", " ".join(full_cmd))

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creation_flags,
        )

        for line in iter(proc.stdout.readline, ""):
            logger.info("[winget] %s", line.rstrip())

        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error("winget install timed out after %ds", timeout)
        return {
            "success": False,
            "exit_code": None,
            "error": f"Installation timed out after {timeout}s",
        }
    except Exception as exc:
        logger.error("winget install failed: %s", exc)
        return {"success": False, "exit_code": None, "error": str(exc)}

    if proc.returncode == 0 or proc.returncode == _WINGET_ALREADY_INSTALLED:
        logger.info("winget install succeeded (exit code %d)", proc.returncode)
        return {"success": True, "exit_code": proc.returncode, "error": None}

    logger.error("winget install failed (exit code %d)", proc.returncode)
    return {
        "success": False,
        "exit_code": proc.returncode,
        "error": f"winget install failed (exit code {proc.returncode})",
    }


def get_installer_status() -> InstallerStatus:
    """Get the full installer status (winget + driver)."""
    from backend.ios_device import check_driver_status

    winget_available, winget_version = check_winget_available()
    driver_status = check_driver_status()

    return InstallerStatus(
        winget_available=winget_available,
        winget_version=winget_version,
        driver_status=driver_status,
    )


def verify_package() -> PackageVerification:
    """
    Verify the Apple.AppleMobileDeviceSupport package exists in winget.

    Runs `winget show --id Apple.AppleMobileDeviceSupport -e` to confirm
    the ID is currently valid and see what version is available.
    """
    try:
        result = _run_command(
            ["winget", "show", "--id", APPLE_DRIVER_PACKAGE_ID, "-e"],
            timeout=30,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or f"winget show exited with code {result.returncode}"
            logger.warning("Package verification failed: %s", error_msg)
            return PackageVerification(
                success=False,
                package_id=None,
                package_name=None,
                version=None,
                error=error_msg,
            )

        # Parse the output to extract package details
        output = result.stdout
        package_name = None
        version = None

        for line in output.splitlines():
            lower = line.lower().strip()
            if lower.startswith("name:"):
                package_name = line.split(":", 1)[1].strip()
            elif lower.startswith("version:"):
                version = line.split(":", 1)[1].strip()

        logger.info(
            "Package verified: %s (version: %s)",
            package_name or APPLE_DRIVER_PACKAGE_ID,
            version or "unknown",
        )
        return PackageVerification(
            success=True,
            package_id=APPLE_DRIVER_PACKAGE_ID,
            package_name=package_name,
            version=version,
            error=None,
        )

    except FileNotFoundError:
        return PackageVerification(
            success=False,
            package_id=None,
            package_name=None,
            version=None,
            error="winget is not available",
        )
    except subprocess.TimeoutExpired:
        return PackageVerification(
            success=False,
            package_id=None,
            package_name=None,
            version=None,
            error="Package lookup timed out",
        )
    except Exception as exc:
        logger.warning("Package verification error: %s", exc)
        return PackageVerification(
            success=False,
            package_id=None,
            package_name=None,
            version=None,
            error=str(exc),
        )


async def verify_package_async() -> PackageVerification:
    """Async variant that does not block the event loop. See verify_package()."""
    try:
        result = await asyncio.to_thread(
            _run_command,
            ["winget", "show", "--id", APPLE_DRIVER_PACKAGE_ID, "-e"],
            30,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or f"winget show exited with code {result.returncode}"
            logger.warning("Package verification failed: %s", error_msg)
            return PackageVerification(
                success=False,
                package_id=None,
                package_name=None,
                version=None,
                error=error_msg,
            )

        output = result.stdout
        package_name = None
        version = None

        for line in output.splitlines():
            lower = line.lower().strip()
            if lower.startswith("name:"):
                package_name = line.split(":", 1)[1].strip()
            elif lower.startswith("version:"):
                version = line.split(":", 1)[1].strip()

        logger.info(
            "Package verified: %s (version: %s)",
            package_name or APPLE_DRIVER_PACKAGE_ID,
            version or "unknown",
        )
        return PackageVerification(
            success=True,
            package_id=APPLE_DRIVER_PACKAGE_ID,
            package_name=package_name,
            version=version,
            error=None,
        )

    except FileNotFoundError:
        return PackageVerification(
            success=False,
            package_id=None,
            package_name=None,
            version=None,
            error="winget is not available",
        )
    except subprocess.TimeoutExpired:
        return PackageVerification(
            success=False,
            package_id=None,
            package_name=None,
            version=None,
            error="Package lookup timed out",
        )
    except Exception as exc:
        logger.warning("Package verification error: %s", exc)
        return PackageVerification(
            success=False,
            package_id=None,
            package_name=None,
            version=None,
            error=str(exc),
        )


def get_install_command(version: str | None = None) -> dict[str, str | list[str]]:
    """Return the winget install command details.

    ``--version`` / ``--exact`` are only added when *version* is a non-
    empty string, preventing ``$null`` positional arguments in any
    downstream PowerShell invocation.
    """
    return {
        "executable": "winget",
        "args": build_install_args(version),
    }


# ---------------------------------------------------------------------------
# Apple Mobile Device Service lifeline
# ---------------------------------------------------------------------------
APPLE_SERVICE_NAME = "Apple Mobile Device Service"


@dataclass
class AppleServiceStatus:
    service_name: str = APPLE_SERVICE_NAME
    state: str = "unknown"
    message: str = ""
    needs_elevation: bool = False
    elevation_command: list[str] | None = None
    exit_code: int | None = None


def _run_sc_command(
    args: list[str],
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    """Run ``sc`` with *args* and return the result."""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = ["sc"] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creation_flags,
    )


async def ensure_apple_service_running() -> AppleServiceStatus:
    """Check the Apple Mobile Device Service and attempt to start it.

    Queries the Windows service manager via ``sc query`` to determine
    whether the Apple Mobile Device Service is installed, running, or
    stopped.  If the service is installed but stopped, attempts to start
    it via ``sc start``.  If that fails with access denied (exit code 5,
    meaning Python is not running elevated), returns an actionable
    ``elevation_command`` that the frontend can execute with
    ``ShellExecuteW("runas")``.

    Returns
    -------
    AppleServiceStatus
        - ``state``: ``"running"`` / ``"stopped"`` / ``"not_installed"`` /
          ``"elevation_required"`` / ``"error"``
        - ``needs_elevation``: True when ``sc start`` failed with access
          denied
        - ``elevation_command``: ``["sc", "start", <service_name>]`` to
          run elevated
    """
    # 1. Query current state
    try:
        query = await asyncio.to_thread(
            _run_sc_command,
            ["query", APPLE_SERVICE_NAME],
            10,
        )
    except FileNotFoundError:
        return AppleServiceStatus(
            state="error",
            message="sc.exe not found on PATH — this is not a standard Windows system",
        )
    except subprocess.TimeoutExpired:
        return AppleServiceStatus(
            state="error",
            message="sc query timed out after 10s",
        )
    except Exception as exc:
        return AppleServiceStatus(
            state="error",
            message=f"sc query failed: {exc}",
        )

    stdout = query.stdout or ""
    stderr_text = query.stderr or ""

    # 2. Determine state from output
    state_line = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("STATE"):
            state_line = stripped
            break

    if not state_line and query.returncode != 0:
        # Service may not exist
        err_lower = (stderr_text + stdout).lower()
        if "not exist" in err_lower or "not found" in err_lower:
            return AppleServiceStatus(
                state="not_installed",
                message=(
                    "Apple Mobile Device Service is not installed. "
                    "Install iTunes or Apple Devices from the Microsoft Store."
                ),
                exit_code=query.returncode,
            )
        return AppleServiceStatus(
            state="error",
            message=f"sc query returned exit code {query.returncode}: {stderr_text.strip() or stdout.strip()}",
            exit_code=query.returncode,
        )

    # 3. Parse STATE line — format: "STATE               : 4  RUNNING"
    if "RUNNING" in state_line.upper():
        return AppleServiceStatus(
            state="running",
            message="Apple Mobile Device Service is already running",
        )

    if "STOPPED" in state_line.upper():
        # 4. Service is installed but stopped — attempt to start
        try:
            start_result = await asyncio.to_thread(
                _run_sc_command,
                ["start", APPLE_SERVICE_NAME],
                30,
            )
        except subprocess.TimeoutExpired:
            return AppleServiceStatus(
                state="error",
                message="sc start timed out after 30s — the service may be hung",
            )
        except Exception as exc:
            return AppleServiceStatus(
                state="error",
                message=f"sc start failed: {exc}",
            )

        start_stdout = start_result.stdout or ""
        start_stderr = start_result.stderr or ""
        start_combined = (start_stdout + start_stderr).lower()

        if start_result.returncode == 0 or "running" in start_combined:
            logger.info("Apple Mobile Device Service started successfully")
            return AppleServiceStatus(
                state="running",
                message="Apple Mobile Device Service started successfully",
                exit_code=start_result.returncode,
            )

        # Access denied (error 5) — elevation needed
        if start_result.returncode == 5 or "access denied" in start_combined or "5" in start_stderr:
            logger.info("Apple Mobile Device Service needs elevation to start")
            return AppleServiceStatus(
                state="elevation_required",
                message=(
                    "Administrator privileges are required to start the "
                    "Apple Mobile Device Service. The frontend should run "
                    "the elevation command via ShellExecuteW('runas')."
                ),
                needs_elevation=True,
                elevation_command=["sc", "start", APPLE_SERVICE_NAME],
                exit_code=start_result.returncode,
            )

        # Unknown failure
        return AppleServiceStatus(
            state="error",
            message=(
                f"sc start returned exit code {start_result.returncode}: "
                f"{start_stderr.strip() or start_stdout.strip()}"
            ),
            exit_code=start_result.returncode,
        )

    # Unknown state — report raw line
    return AppleServiceStatus(
        state="error",
        message=f"Unexpected service state: {state_line.strip()}",
    )
