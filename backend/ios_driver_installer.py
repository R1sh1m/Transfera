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

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

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


def get_install_command() -> dict[str, str | list[str]]:
    """
    Return the winget install command details.

    Does NOT execute the command — that's the Electron shell's job
    (it needs to request elevation via the "runas" verb).
    """
    return {
        "executable": "winget",
        "args": [
            "install",
            "-e",
            "--id", APPLE_DRIVER_PACKAGE_ID,
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
    }
