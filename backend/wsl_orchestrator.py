"""
Transfera v2 -- WSL2 + usbipd-win Orchestrator
Windows-side management of the WSL2 USB/IP fallback path for iPhone access.

This module NEVER triggers UAC elevation or system restarts directly.
It returns results that tell the caller what happened, and the API layer
+ frontend handle user notification and confirmation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from backend.config import DATA_DIR

creationflags = 0x08000000 if sys.platform == "win32" else 0

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BRIDGE_PORT = 18920
DISTRO_NAME = "Ubuntu"  # fallback if dynamic lookup fails
APPLE_VID = "05ac"
STATE_DIR = Path.home() / ".transfera"
STATE_FILE = STATE_DIR / "tier2_state.json"
BRIDGE_SCRIPT_NAME = "wsl_bridge.py"
BRIDGE_INSTALL_PATH = "/opt/transfera-bridge"
DISTRO_SAVE_PATH = DATA_DIR / "wsl_distro.txt"

# ---------------------------------------------------------------------------
# Distro name resolution
# ---------------------------------------------------------------------------
def get_transfera_wsl_distro() -> str | None:
    """Return the name of the WSL distro Transfera should use.

    Checks in order:
    1. A saved distro name in ``backend/data/wsl_distro.txt``
       (written during ``tier2/setup``).
    2. The default WSL distro (``wsl --list --quiet``, first line).
    3. Any distro whose name contains **Ubuntu** (case-insensitive).
    4. Any registered distro.
    Returns ``None`` if no WSL distro is installed at all.
    """
    # 1. Saved name from a previous setup run
    if DISTRO_SAVE_PATH.exists():
        try:
            # wsl --list outputs UTF-16LE on Windows; the save file is also
            # written as UTF-16LE so one reader works for both sources.
            name = DISTRO_SAVE_PATH.read_text(encoding="utf-16-le").strip().strip("\x00")
            if name:
                logger.debug("Distro from saved file: %s", name)
                return name
        except Exception:
            pass

    # 2. List all distros
    try:
        result = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True,
            timeout=10,
            creationflags=creationflags,
        )
        # wsl --list --quiet outputs plain distro names in UTF-16LE
        output = result.stdout.decode("utf-16-le", errors="replace")
        distros = [
            d.strip().strip("\x00")
            for d in output.splitlines()
            if d.strip().strip("\x00")
        ]
    except Exception:
        distros = []

    if not distros:
        # Fallback: try --verbose which shows more info
        try:
            result = subprocess.run(
                ["wsl", "--list", "--verbose"],
                capture_output=True,
                timeout=10,
                creationflags=creationflags,
            )
            output = result.stdout.decode("utf-16-le", errors="replace")
            distros = []
            for line in output.splitlines():
                clean = line.strip().strip("\x00").lstrip("*").strip()
                if not clean:
                    continue
                if clean.startswith("NAME") or "VERSION" in clean.upper():
                    continue
                parts = clean.split()
                if len(parts) >= 3:
                    distros.append(parts[0])
        except Exception:
            return None

    if not distros:
        return None

    # 3. Ubuntu variant preferred
    for d in distros:
        if "ubuntu" in d.lower():
            logger.debug("Distro from Ubuntu match: %s", d)
            return d

    # 4. First available
    logger.debug("Distro from first available: %s", distros[0])
    return distros[0]


def save_transfera_wsl_distro(name: str) -> None:
    """Persist the distro name so ``get_transfera_wsl_distro()`` finds it."""
    DISTRO_SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write as UTF-16LE so the same reader handles saved file and wsl output
    DISTRO_SAVE_PATH.write_text(name + "\n", encoding="utf-16-le")
    logger.info("Saved WSL distro name: %s", name)


# Portable shell lock-polling preamble for apt/dpkg locks
# Compatible with apt 1.x, 2.x, and WSL distros without systemd (no --lock-timeout).
APT_LOCK_POLL_SCRIPT = """\
wait_for_lock() {
  local lockfile=$1 sentinel=$2 i=0
  if flock --nonblock "$lockfile" -c true 2>/dev/null; then
    return 0
  fi
  echo "Waiting for apt lock..."
  while ! flock --nonblock "$lockfile" -c true 2>/dev/null; do
    if [ $i -ge 24 ]; then
      echo "$sentinel" >&2
      return 1
    fi
    sleep 5
    i=$((i+1))
  done
}
wait_for_lock /var/lib/apt/lists/lock APT_LOCK_TIMEOUT || exit 1
wait_for_lock /var/lib/dpkg/lock-frontend DPKG_LOCK_TIMEOUT || exit 1
"""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class StepID(str, Enum):
    CHECK_FEASIBILITY = "check_feasibility"
    ENABLE_WSL = "enable_wsl"
    INSTALL_DISTRO = "install_distro"
    INSTALL_USBIPD = "install_usbipd"
    PROVISION_LINUX = "provision_linux"
    START_BRIDGE = "start_bridge"
    BIND_DEVICE = "bind_device"
    ATTACH_DEVICE = "attach_device"
    CONFIRM_DEVICE = "confirm_device"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class WSLStatus:
    wsl_installed: bool = False
    distro_name: str | None = None
    distro_ready: bool = False
    restart_required: bool = False
    virtualization_available: bool = False
    kernel_version: str | None = None
    error: str | None = None


@dataclass
class USBIPDStatus:
    installed: bool = False
    version: str | None = None
    package_id: str | None = None
    error: str | None = None


@dataclass
class BridgeStatus:
    running: bool = False
    port: int = BRIDGE_PORT
    reachable: bool = False
    devices: list[dict] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    last_error: str | None = None
    restart_count: int = 0


@dataclass
class USBDeviceInfo:
    busid: str = ""
    vid_pid: str = ""
    device_name: str = ""
    state: str = ""

    @property
    def is_apple(self) -> bool:
        return self.vid_pid.lower().startswith(APPLE_VID)

    @property
    def is_bound(self) -> bool:
        return "Shared" in self.state

    @property
    def is_attached(self) -> bool:
        return "Attached" in self.state


@dataclass
class Tier2DeviceStatus:
    busid: str = ""
    bound: bool = False
    attached: bool = False
    confirmed_in_wsl: bool = False
    error: str | None = None


@dataclass
class Tier2StepResult:
    step_id: str
    completed: bool
    restart_required: bool = False
    error: str | None = None
    error_code: str | None = None
    next_step: str | None = None
    details: dict = field(default_factory=dict)


@dataclass
class Tier2StepPreview:
    step_id: str
    title: str
    description: str
    requires_restart: bool = False
    requires_elevation: bool = False
    elevation_description: str | None = None
    restart_description: str | None = None
    can_cancel: bool = True


@dataclass
class Tier2SetupPreview:
    steps: list[Tier2StepPreview] = field(default_factory=list)
    total_steps: int = 0
    requires_restart: bool = False
    requires_elevation: bool = False


@dataclass
class Tier2PersistedState:
    steps_completed: list[str] = field(default_factory=list)
    pending_step: str | None = None
    pending_bind_busid: str | None = None
    pending_bind_device_name: str | None = None
    saved_at: str = ""
    schema_version: int = 1

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.saved_at = datetime.now(UTC).isoformat()
        STATE_FILE.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls) -> Tier2PersistedState | None:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                return cls(**data)
            except Exception:
                return None
        return None

    def delete(self) -> None:
        if STATE_FILE.exists():
            STATE_FILE.unlink()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _looks_like_utf16le(data: bytes) -> bool:
    """Heuristic check: is this byte sequence UTF-16LE-encoded text?

    UTF-16LE ASCII text has an alternating pattern where every character
    (position 0, 2, 4...) is a printable ASCII byte and every interleaved
    byte (position 1, 3, 5...) is null (``\\x00``).  We sample the first
    512 bytes and check this property statistically.
    """
    if len(data) < 4:
        return False
    sample = data[: min(len(data) & ~1, 512)]
    even = sample[0::2]
    odd = sample[1::2]
    if not odd or not even:
        return False
    null_odds = sum(1 for b in odd if b == 0)
    printable_evens = sum(1 for b in even if 0x09 <= b <= 0x7E)
    return (
        null_odds / len(odd) > 0.8
        and printable_evens / len(even) > 0.7
    )


def _decode_wsl_output(data: bytes) -> str:
    """Decode ``wsl.exe`` subprocess output, auto-detecting encoding.

    ``wsl.exe`` on Windows 10/11 writes UTF-16LE to its stdout pipe when
    the output is not a console (i.e. when captured).  Decoding that as
    UTF-8 produces Python strings with embedded ``\\x00`` bytes between
    every character, which causes ``ValueError: embedded null character``
    the moment the string hits any C-level operation (Pydantic
    serialization, logging, ``os.putenv()``, etc.).

    This function detects UTF-16LE by BOM or by the alternating-null ASCII
    pattern and decodes accordingly, falling back to UTF-8 as the default.
    ``errors="replace"`` is always applied as a safety net so any future
    encoding mismatch produces a readable (if imperfect) string, not a
    hard crash.
    """
    if not data:
        return ""
    try:
        # Definitive signal: UTF-16LE BOM
        if data[:2] == b"\xff\xfe":
            logger.debug(
                "wsl output: detected UTF-16LE BOM, decoding as utf-16-le "
                "(first 64 raw bytes: %s)",
                data[:64].hex(),
            )
            return data.decode("utf-16-le", errors="replace")
        # Strong signal: alternating-null pattern
        if _looks_like_utf16le(data):
            logger.debug(
                "wsl output: detected UTF-16LE alternating-null pattern, "
                "decoding as utf-16-le (first 64 raw bytes: %s)",
                data[:64].hex(),
            )
            return data.decode("utf-16-le", errors="replace")
    except Exception:
        logger.warning(
            "wsl output: UTF-16LE decode failed, falling back to UTF-8 "
            "(first 64 raw bytes: %s)",
            data[:64].hex(),
        )
    # Default: UTF-8 (Python's default encoding)
    logger.debug(
        "wsl output: decoding as UTF-8 (first 64 raw bytes: %s)",
        data[:64].hex(),
    )
    return data.decode("utf-8", errors="replace")


def _apt_lock_contended(err: str) -> bool:
    err_lower = err.lower()
    return (
        "could not get lock" in err_lower
        or "apt_lock_timeout" in err_lower
        or "dpkg_lock_timeout" in err_lower
    )


async def _run_cmd(
    *args: str,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, _decode_wsl_output(stdout), _decode_wsl_output(stderr)


async def _run_cmd_ok(*args: str, timeout: float = 60.0) -> str:
    rc, out, err = await _run_cmd(*args, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"Command failed (rc={rc}): {err.strip() or out.strip()}")
    return out


def _is_valid_distro_line(line: str) -> str | None:
    """Parse a single line from ``wsl --list --verbose`` output.

    Returns the distro name if the line describes a registered WSL
    distribution, or ``None`` if the line is a header, informational
    message, or empty.

    Skips:
    - Empty lines
    - Header rows (NAME / VERSION columns)
    - Informational messages (start with ``(`` or ``-`` or contain
      English text like "no installed distributions")
    """
    clean = line.strip().strip("\x00").lstrip("*").strip()
    if not clean:
        return None
    if clean.startswith("NAME") or "VERSION" in clean.upper():
        return None
    if clean.startswith("(") or clean.startswith("-"):
        return None
    parts = clean.split()
    if len(parts) < 3:
        return None
    name = parts[0]
    if not name.isascii() or not name[0].isalpha():
        return None
    return name


# ---------------------------------------------------------------------------
# WSLOrchestrator
# ---------------------------------------------------------------------------
class WSLOrchestrator:

    def __init__(self) -> None:
        self._cached_distro_name: str | None = None
        self._distro_cache_time: float = 0.0
        self._bridge_restart_count: int = 0
        self._bridge_last_stderr: str | None = None
        self._bridge_watchdog_task: asyncio.Task | None = None
        self._bridge_intentional_stop: bool = False

    async def _find_distro(self) -> str | None:
        """Return installed WSL distro name, or None if none found. Cached 30s."""
        now = asyncio.get_event_loop().time()
        if self._cached_distro_name is not None and now - self._distro_cache_time < 30:
            return self._cached_distro_name
        # Delegate to the standalone resolver (saved file → wsl --list → Ubuntu)
        name = get_transfera_wsl_distro()
        self._cached_distro_name = name
        self._distro_cache_time = now
        return name

    async def check_feasibility(self) -> WSLStatus:
        status = WSLStatus()
        try:
            rc, out, _ = await _run_cmd("cmd", "/c", "ver")
            build_match = re.search(r"\[(\d+)\.(\d+)\.(\d+)\]", out)
            if build_match:
                build = int(build_match.group(3))
                if build < 17763:
                    status.error = f"Windows build {build} is too old for WSL2 (need 17763+)"
                    return status
        except Exception:
            pass

        status.virtualization_available = await self._check_virtualization()

        try:
            rc, out, _ = await _run_cmd("wsl", "--list", "--verbose", timeout=10)
            if rc == 0 and out.strip():
                status.wsl_installed = True
                # First: use the dynamic resolver (saved → default → Ubuntu → first)
                resolved = get_transfera_wsl_distro()
                if resolved:
                    status.distro_name = resolved
                    status.distro_ready = ("2" in out) or True
                else:
                    # Fallback: scan --verbose output for any distro
                    for line in out.splitlines():
                        name = _is_valid_distro_line(line)
                        if name:
                            status.distro_name = name
                            status.distro_ready = ("2" in line) or True
                            break
        except FileNotFoundError:
            status.wsl_installed = False
        except Exception as exc:
            logger.debug("WSL list check failed: %s", exc)
            status.wsl_installed = False

        if status.wsl_installed and not status.distro_ready:
            try:
                rc, out, _ = await _run_cmd("wsl", "--status", timeout=10)
                if "restart" in out.lower() or "reboot" in out.lower():
                    status.restart_required = True
            except Exception:
                pass

        if status.distro_ready:
            distro_for_uname = status.distro_name or get_transfera_wsl_distro() or DISTRO_NAME
            try:
                rc, out, _ = await _run_cmd(
                    "wsl", "-d", distro_for_uname,
                    "--", "uname", "-r", timeout=10,
                )
                if rc == 0:
                    status.kernel_version = out.strip()
            except Exception:
                pass

        return status

    async def _check_virtualization(self) -> bool:
        try:
            rc, out, _ = await _run_cmd(
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "(Get-CimInstance -ClassName Win32_ComputerSystem).HypervisorPresent -or (Get-CimInstance -ClassName Win32_Processor).VirtualizationFirmwareEnabled",
                timeout=10,
            )
            return rc == 0 and "true" in out.lower()
        except Exception:
            return False

    async def install_wsl(self) -> Tier2StepResult:
        try:
            rc, out, err = await _run_cmd(
                "wsl", "--install", "-d", DISTRO_NAME, timeout=300,
            )
            combined = out + err
            if "restart" in combined.lower() or "Changes will not be effective" in combined.lower():
                return Tier2StepResult(
                    step_id=StepID.ENABLE_WSL, completed=False,
                    restart_required=True, details={"output": combined},
                )
            if rc == 0 or "installed" in combined.lower() or "ubuntu" in combined.lower():
                return Tier2StepResult(
                    step_id=StepID.ENABLE_WSL, completed=True,
                    details={"output": combined},
                )
            return Tier2StepResult(
                step_id=StepID.ENABLE_WSL, completed=False,
                error=f"WSL install returned: {combined.strip()}",
                details={"output": combined},
            )
        except TimeoutError:
            return Tier2StepResult(
                step_id=StepID.ENABLE_WSL, completed=False,
                error="WSL installation timed out after 5 minutes",
            )
        except Exception as exc:
            return Tier2StepResult(step_id=StepID.ENABLE_WSL, completed=False, error=str(exc))

    async def verify_wsl_after_restart(self) -> Tier2StepResult:
        status = await self.check_feasibility()
        if status.wsl_installed and status.distro_ready:
            return Tier2StepResult(step_id=StepID.ENABLE_WSL, completed=True, details={"distro": status.distro_name})
        if status.restart_required:
            return Tier2StepResult(step_id=StepID.ENABLE_WSL, completed=False, restart_required=True, error="Restart has not completed yet")
        return Tier2StepResult(step_id=StepID.ENABLE_WSL, completed=False, error=status.error or "WSL is not ready after restart")

    async def install_distro(self) -> Tier2StepResult:
        status = await self.check_feasibility()
        if status.distro_ready:
            return Tier2StepResult(step_id=StepID.INSTALL_DISTRO, completed=True, details={"distro": status.distro_name})
        try:
            rc, out, err = await _run_cmd("wsl", "--install", "-d", DISTRO_NAME, "--no-launch", timeout=300)
            combined = out + err
            if rc == 0 or "installed" in combined.lower():
                return Tier2StepResult(step_id=StepID.INSTALL_DISTRO, completed=True, details={"output": combined})
            return Tier2StepResult(step_id=StepID.INSTALL_DISTRO, completed=False, error=combined.strip() or "Distro installation failed")
        except Exception as exc:
            return Tier2StepResult(step_id=StepID.INSTALL_DISTRO, completed=False, error=str(exc))

    async def get_usbipd_install_command(self) -> dict:
        return {
            "executable": "powershell",
            "args": ["winget", "install", "--id", "dorssel.usbipd-win", "-e", "--accept-package-agreements", "--accept-source-agreements"],
            "description": "Install usbipd-win -- an open-source USB device sharing tool referenced in Microsoft's official WSL documentation. Windows will ask for admin permission (UAC).",
        }

    async def verify_usbipd_installed(self) -> USBIPDStatus:
        status = USBIPDStatus()
        try:
            rc, out, _ = await _run_cmd("winget", "show", "--id", "dorssel.usbipd-win", "-e", timeout=30)
            if rc == 0 and "Version" in out:
                status.installed = True
                status.package_id = "dorssel.usbipd-win"
                for line in out.splitlines():
                    if line.strip().startswith("Version"):
                        parts = line.split(":")
                        if len(parts) >= 2:
                            status.version = parts[1].strip()
                            break
        except Exception:
            pass
        if not status.installed and shutil.which("usbipd"):
            status.installed = True
            try:
                rc, out, _ = await _run_cmd("usbipd", "--version", timeout=5)
                if rc == 0:
                    status.version = out.strip()
            except Exception:
                pass
        return status

    async def provision_linux(self, distro: str | None = None) -> Tier2StepResult:
        d = get_transfera_wsl_distro() if distro is None else distro
        if d is None:
            return Tier2StepResult(
                step_id=StepID.PROVISION_LINUX,
                completed=False,
                error=(
                    "No WSL distribution found. "
                    "Please install Ubuntu from the Microsoft Store."
                ),
                error_code="NO_WSL_DISTRO",
                details={"steps_completed": []},
            )
        installed = await self._find_distro()
        if installed is None:
            attempt = await self.install_distro()
            if not attempt.completed:
                hint = (
                    "No Linux distribution is registered in WSL. "
                    "Transfera attempted to install one automatically, "
                    "but that failed. Open a PowerShell terminal and run:\n"
                    "  wsl --install -d Ubuntu\n"
                    "then retry this setup step."
                )
                return Tier2StepResult(
                    step_id=StepID.PROVISION_LINUX,
                    completed=False,
                    error=attempt.error or hint,
                    details={"steps_completed": []},
                )
            installed = await self._find_distro()
            if installed is None:
                return Tier2StepResult(
                    step_id=StepID.PROVISION_LINUX,
                    completed=False,
                    error="No Linux distribution found, and automatic installation did not register one.",
                    details={"steps_completed": []},
                )
        if distro is None:
            d = installed
        steps_completed: list[str] = []
        # -------------------------------------------------------------------
        # apt-get update with lock-wait strategy
        # -------------------------------------------------------------------
        update_desc = "Update package lists"
        update_ok = False
        update_lock_contended = False

        # Portable lock-polling loop (no --lock-timeout, works on apt 1.x/2.x)
        try:
            rc, out, err = await _run_cmd(
                "wsl", "-d", d, "-u", "root", "--",
                "bash", "-c",
                APT_LOCK_POLL_SCRIPT + "DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::Retries=1 update -y",
                timeout=200,
            )
            if rc == 0:
                update_ok = True
            elif _apt_lock_contended(err or ""):
                update_lock_contended = True
        except TimeoutError:
            return Tier2StepResult(
                step_id=StepID.PROVISION_LINUX,
                completed=False,
                error="Updating package lists timed out. Please check your internet connection inside WSL.",
                details={"steps_completed": steps_completed},
            )
        except Exception as exc:
            return Tier2StepResult(
                step_id=StepID.PROVISION_LINUX,
                completed=False,
                error=f"Failed to update package lists: {exc}",
                details={"steps_completed": steps_completed},
            )

        if not update_ok:
            raw = err.strip() or out.strip()
            if "no distribution with the supplied name" in raw.lower() or "wsl_e_distro_not_found" in raw.lower():
                hint = (
                    f"The Linux distribution '{d}' is no longer registered. "
                    f"Open a PowerShell terminal and run:\n"
                    f"  wsl --install -d Ubuntu\n"
                    f"then retry this setup step."
                )
                return Tier2StepResult(step_id=StepID.PROVISION_LINUX, completed=False, error=hint, details={"steps_completed": steps_completed})
            return Tier2StepResult(
                step_id=StepID.PROVISION_LINUX,
                completed=False,
                error=f"Failed to {update_desc}: {raw}",
                error_code="APT_LOCK_TIMEOUT" if update_lock_contended else None,
                details={"steps_completed": steps_completed},
            )
        steps_completed.append(update_desc)

        # -------------------------------------------------------------------
        # apt-get install with lock-wait strategy
        # -------------------------------------------------------------------
        install_desc = "Install USB/IP tools, Python, usbmuxd"
        install_pkgs = [
            "linux-tools-common", "hwdata", "usbutils",
            "python3", "python3-pip", "python3-venv",
            "usbmuxd", "curl",
        ]
        try:
            pkgs_str = " ".join(install_pkgs)
            rc, out, err = await _run_cmd(
                "wsl", "-d", d, "-u", "root", "--",
                "bash", "-c",
                APT_LOCK_POLL_SCRIPT + f"DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::Retries=1 install -y {pkgs_str}",
                timeout=180,
            )
            if rc != 0 and "already" not in out.lower():
                raw = err.strip() or out.strip()
                error_code = "DPKG_LOCK_TIMEOUT" if _apt_lock_contended(err or "") else None
                return Tier2StepResult(
                    step_id=StepID.PROVISION_LINUX,
                    completed=False,
                    error=f"Failed to {install_desc}: {raw}",
                    error_code=error_code,
                    details={"steps_completed": steps_completed},
                )
            steps_completed.append(install_desc)
        except Exception as exc:
            return Tier2StepResult(
                step_id=StepID.PROVISION_LINUX,
                completed=False,
                error=f"Failed to {install_desc}: {exc}",
                details={"steps_completed": steps_completed},
            )

        try:
            rc, out, _ = await _run_cmd("wsl", "-d", d, "-u", "root", "--", "pip3", "install", "--break-system-packages", "pymobiledevice3", timeout=120)
            if rc != 0:
                await _run_cmd("wsl", "-d", d, "-u", "root", "--", "pip3", "install", "pymobiledevice3", timeout=120)
            steps_completed.append("pymobiledevice3")
        except Exception as exc:
            return Tier2StepResult(step_id=StepID.PROVISION_LINUX, completed=False, error=f"Failed to install pymobiledevice3: {exc}", details={"steps_completed": steps_completed})

        try:
            await _run_cmd("wsl", "-d", d, "-u", "root", "--", "bash", "-c", "update-alternatives --install /usr/bin/usbip usbip $(ls /usr/lib/linux-tools/*-generic/usbip 2>/dev/null | head -1) 20 || true", timeout=30)
            steps_completed.append("usbip alternatives")
        except Exception:
            pass

        try:
            await _run_cmd("wsl", "-d", d, "-u", "root", "--", "mkdir", "-p", BRIDGE_INSTALL_PATH, timeout=10)
            backend_dir = Path(__file__).resolve().parent
            bridge_src = backend_dir / BRIDGE_SCRIPT_NAME
            if bridge_src.exists():
                wsl_backend = await _run_cmd_ok("wsl", "-d", d, "--", "wslpath", "-u", str(backend_dir), timeout=10)
                wsl_src = f"{wsl_backend.strip()}/{BRIDGE_SCRIPT_NAME}"
                await _run_cmd("wsl", "-d", d, "-u", "root", "--", "cp", wsl_src, f"{BRIDGE_INSTALL_PATH}/{BRIDGE_SCRIPT_NAME}", timeout=10)
                await _run_cmd("wsl", "-d", d, "-u", "root", "--", "chmod", "+x", f"{BRIDGE_INSTALL_PATH}/{BRIDGE_SCRIPT_NAME}", timeout=10)
            steps_completed.append("bridge script deployed")
        except Exception as exc:
            logger.warning("Bridge script deployment failed: %s", exc)

        # Persist the distro name so future lookups find it immediately
        save_transfera_wsl_distro(d)
        return Tier2StepResult(step_id=StepID.PROVISION_LINUX, completed=True, details={"steps_completed": steps_completed})

    async def start_bridge(self, distro: str | None = None) -> BridgeStatus:
        status = BridgeStatus()
        status.restart_count = self._bridge_restart_count

        # Resolve distro before doing anything else
        d = get_transfera_wsl_distro() if distro is None else distro
        if d is None:
            status.error = (
                "No WSL distribution found. "
                "Please install Ubuntu from the Microsoft Store."
            )
            status.error_code = "NO_WSL_DISTRO"
            return status

        if self._bridge_restart_count >= 3:
            status.error = (
                "Bridge failed to start after 3 attempts. "
                "Check the bridge error log in Advanced settings."
            )
            status.last_error = self._bridge_last_stderr
            return status

        # Retry hygiene: kill any bridge from a previous attempt before starting fresh
        existing_reachable = False
        check: BridgeStatus | None = None
        try:
            check = await self.get_bridge_status(d)
            existing_reachable = check.reachable
        except Exception:
            pass

        if existing_reachable and check is not None:
            self._bridge_restart_count = 0
            self._bridge_last_stderr = None
            self._bridge_intentional_stop = False
            # Start watchdog if not already running
            if (self._bridge_watchdog_task is None
                    or self._bridge_watchdog_task.done()):
                self._bridge_watchdog_task = asyncio.create_task(
                    self._bridge_watchdog_loop(d),
                    name="bridge-watchdog",
                )
                logger.info("Bridge watchdog task started")
            return check

        # Clean up any leftover bridge process from an earlier (failed) attempt
        await self.stop_bridge(d)

        # Wait for WSL networking to be ready before spawning the bridge
        # so it doesn't try to bind before the interface is up.
        network_ok = False
        for _ in range(20):
            try:
                rc, out, _ = await _run_cmd("wsl", "-d", d, "--", "bash", "-c",
                    "hostname -I 2>/dev/null | grep -q . && echo OK || echo WAIT",
                    timeout=10)
                if rc == 0 and "OK" in out:
                    network_ok = True
                    break
            except Exception:
                pass
            await asyncio.sleep(1.0)
        if not network_ok:
            logger.warning("WSL network not ready after 20s — starting bridge anyway")

        bridge_path = f"{BRIDGE_INSTALL_PATH}/{BRIDGE_SCRIPT_NAME}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl", "-d", d, "-u", "root", "--",
                "python3", bridge_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            logger.info("Bridge process started (pid=%s)", proc.pid)
            # Poll generously — first-time cold start inside WSL can be slow
            for _ in range(120):
                await asyncio.sleep(0.5)
                if proc.returncode is not None:
                    # Process exited — capture output before continuing
                    captured_stdout, captured_stderr = await proc.communicate()
                    raw_stderr = captured_stderr if captured_stderr else b""
                    for enc in ('utf-16-le', 'utf-8', 'cp1252'):
                        try:
                            stderr_text = raw_stderr.decode(enc).replace('\x00', '').strip()
                            break
                        except (UnicodeDecodeError, ValueError):
                            continue
                    else:
                        stderr_text = raw_stderr.decode('utf-8', errors='replace').strip()
                    stderr_text = re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', stderr_text)
                    stdout_text = captured_stdout.decode("utf-8", errors="replace").strip() if captured_stdout else ""
                    body = stderr_text or stdout_text
                    msg = body or f"Bridge exited with code {proc.returncode}"
                    logger.error(
                        "Bridge process exited unexpectedly (rc=%s):\n%s",
                        proc.returncode, msg[:500],
                    )
                    self._bridge_restart_count += 1
                    status.restart_count = self._bridge_restart_count
                    status.last_error = msg
                    # Log first 50 lines for diagnostics
                    if body:
                        lines = body.splitlines()
                        for i, line in enumerate(lines[:50]):
                            logger.error("bridge[stderr:%d] %s", i, line)
                    break
                check = await self.get_bridge_status(d)
                if check.reachable:
                    self._bridge_restart_count = 0
                    self._bridge_last_stderr = None
                    self._bridge_intentional_stop = False
                    # Start watchdog if not already running
                    if (self._bridge_watchdog_task is None
                            or self._bridge_watchdog_task.done()):
                        self._bridge_watchdog_task = asyncio.create_task(
                            self._bridge_watchdog_loop(d),
                            name="bridge-watchdog",
                        )
                        logger.info("Bridge watchdog task started")
                    return check
            if not status.error and not status.reachable:
                status.error = "Bridge started but not reachable after 60 seconds"
                self._bridge_restart_count += 1
                status.restart_count = self._bridge_restart_count
        except Exception as exc:
            status.error = str(exc)
            self._bridge_restart_count += 1
            status.restart_count = self._bridge_restart_count
        self._bridge_last_stderr = status.last_error
        return status

    async def stop_bridge(self, distro: str | None = None) -> None:
        # Signal the watchdog not to restart, then cancel it
        self._bridge_intentional_stop = True
        if self._bridge_watchdog_task is not None and not self._bridge_watchdog_task.done():
            self._bridge_watchdog_task.cancel()
            try:
                await self._bridge_watchdog_task
            except asyncio.CancelledError:
                pass
            self._bridge_watchdog_task = None
            logger.info("Bridge watchdog cancelled (stop_bridge called)")

        d = get_transfera_wsl_distro() if distro is None else distro
        if d is None:
            return
        try:
            await _run_cmd("wsl", "-d", d, "-u", "root", "--", "bash", "-c",
                "PID=$(cat /tmp/transfera-bridge.pid 2>/dev/null) && kill $PID 2>/dev/null; "
                "rm -f /tmp/transfera-bridge.pid; "
                "pkill -f wsl_bridge.py 2>/dev/null; true",
                timeout=10)
        except Exception:
            pass
        # Small cooldown so the old process actually exits before we proceed
        await asyncio.sleep(1)

    async def _bridge_watchdog_loop(self, distro: str) -> None:
        """
        Background watchdog: probe the bridge every 8 seconds while it's running.

        If two consecutive probes both fail (16s combined), attempt a restart via
        the existing start_bridge() which has its own 3-attempt retry logic.
        This restores mid-transfer resilience without duplicating restart logic.
        """
        PROBE_INTERVAL = 8.0      # seconds between health checks
        FAIL_THRESHOLD = 2         # consecutive failures before restart attempt

        logger.info("Bridge watchdog started for distro=%s", distro)
        consecutive_failures = 0

        try:
            while True:
                await asyncio.sleep(PROBE_INTERVAL)

                if self._bridge_intentional_stop:
                    logger.info("Bridge watchdog: intentional stop detected — exiting")
                    return

                try:
                    status = await self.get_bridge_status(distro)
                    if status.reachable:
                        consecutive_failures = 0
                        continue
                    else:
                        consecutive_failures += 1
                        logger.warning(
                            "Bridge watchdog: probe failed (%d/%d consecutive)",
                            consecutive_failures, FAIL_THRESHOLD,
                        )
                except Exception as exc:
                    consecutive_failures += 1
                    logger.warning(
                        "Bridge watchdog: probe raised exception (%d/%d): %s",
                        consecutive_failures, FAIL_THRESHOLD, exc,
                    )

                if consecutive_failures < FAIL_THRESHOLD:
                    continue

                # Two consecutive failures — attempt restart
                logger.warning(
                    "Bridge watchdog: bridge appears dead (2 consecutive failed probes) "
                    "— attempting restart via start_bridge()"
                )
                consecutive_failures = 0

                # Reset the restart counter so start_bridge()'s own 3-attempt
                # limit applies fresh (we are recovering from a mid-run crash,
                # not a startup failure, so the counter should not carry over).
                self._bridge_restart_count = 0

                try:
                    restart_status = await self.start_bridge(distro)
                    if restart_status.reachable:
                        logger.info(
                            "Bridge watchdog: bridge restarted successfully (port=%d)",
                            BRIDGE_PORT,
                        )
                    else:
                        logger.error(
                            "Bridge watchdog: restart failed — %s",
                            restart_status.error or restart_status.last_error,
                        )
                except Exception as exc:
                    logger.error("Bridge watchdog: restart raised exception: %s", exc)

        except asyncio.CancelledError:
            logger.info("Bridge watchdog: cancelled cleanly")
            raise

    async def cleanup_orphaned_bridge(self, distro: str | None = None) -> None:
        """Check for a leftover bridge process from a previous session and kill it."""
        d = get_transfera_wsl_distro() if distro is None else distro
        if d is None:
            return
        reachable = False
        try:
            check = await self.get_bridge_status(d)
            reachable = check.reachable
        except Exception:
            pass
        if reachable:
            return
        logger.info("No reachable bridge found — checking for orphaned process")
        await self.stop_bridge(d)

    async def get_bridge_status(self, distro: str | None = None) -> BridgeStatus:
        d = get_transfera_wsl_distro() if distro is None else distro
        status = BridgeStatus()
        status.last_error = self._bridge_last_stderr
        status.restart_count = self._bridge_restart_count
        reachable, devices = await self._probe_bridge(f"http://127.0.0.1:{BRIDGE_PORT}")
        if reachable:
            status.reachable = True
            status.running = True
            status.devices = devices
            return status
        if d is None:
            status.error = "Bridge is not reachable"
            return status
        try:
            rc, out, _ = await _run_cmd("wsl", "-d", d, "--", "hostname", "-I", timeout=5)
            if rc == 0 and out.strip():
                wsl_ip = out.strip().split()[0]
                reachable, devices = await self._probe_bridge(f"http://{wsl_ip}:{BRIDGE_PORT}")
                if reachable:
                    status.reachable = True
                    status.running = True
                    status.devices = devices
                    return status
        except Exception:
            pass
        status.error = "Bridge is not reachable"
        return status

    async def _probe_bridge(self, base_url: str) -> tuple[bool, list[dict]]:
        try:
            import aiohttp  # type: ignore[import-untyped]
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status != 200:
                        return False, []
                async with session.get(f"{base_url}/api/ios-devices", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return True, data.get("devices", [])
        except ImportError:
            import json as _json
            import urllib.request

            def _sync_probe() -> tuple[bool, list[dict]]:
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(f"{base_url}/health"), timeout=3
                    ) as resp:
                        if resp.status != 200:
                            return False, []
                    with urllib.request.urlopen(
                        urllib.request.Request(f"{base_url}/api/ios-devices"), timeout=5
                    ) as resp2:
                        data = _json.loads(resp2.read())
                        return True, data.get("devices", [])
                except Exception:
                    return False, []

            return await asyncio.to_thread(_sync_probe)
        except Exception:
            pass
        return False, []

    async def list_usb_devices(self) -> list[USBDeviceInfo]:
        devices: list[USBDeviceInfo] = []
        try:
            rc, out, err = await _run_cmd("usbipd", "list", timeout=15)
            if rc != 0:
                return devices
            in_connected = False
            for line in out.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if any(stripped.startswith(h) for h in ("BUSID", "---", "Connected", "Persisted")):
                    if "Connected" in stripped:
                        in_connected = True
                    continue
                if not in_connected:
                    continue
                parts = stripped.split(None, 3)
                if len(parts) >= 3:
                    busid = parts[0]
                    if not re.match(r"^\d+-\d+", busid):
                        continue
                    vid_pid = parts[1] if len(parts) > 1 else ""
                    device_name = parts[2] if len(parts) > 2 else ""
                    state = parts[3] if len(parts) > 3 else "Not shared"
                    for known_state in ("Shared (forced)", "Attached (forced)", "Shared", "Attached", "Not shared"):
                        if stripped.endswith(known_state):
                            state = known_state
                            name_end = stripped.rfind(known_state)
                            device_name = stripped[len(busid) + len(vid_pid) + 2:name_end].strip()
                            break
                    devices.append(USBDeviceInfo(busid=busid, vid_pid=vid_pid, device_name=device_name, state=state))
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Failed to list USB devices: %s", exc)
        return devices

    def get_bind_command(self, busid: str) -> dict:
        return {
            "executable": "powershell",
            "args": ["usbipd", "bind", "--busid", busid, "--force"],
            "description": f"Share USB device {busid} with WSL. Windows will ask for admin permission (UAC). This only needs to happen once per device.",
        }

    async def attach_device(self, busid: str, distro: str | None = None) -> Tier2DeviceStatus:
        d = get_transfera_wsl_distro() if distro is None else distro
        if d is None:
            return Tier2DeviceStatus(
                busid=busid,
                error="No WSL distribution found. Cannot attach device. "
                      "Please install a WSL distribution first.",
            )
        status = Tier2DeviceStatus(busid=busid)
        try:
            rc, out, err = await _run_cmd("usbipd", "attach", "--wsl", "--busid", busid, "-d", d, timeout=30)
            if rc == 0:
                status.attached = True
            else:
                status.error = err.strip() or out.strip() or "Attach failed"
        except Exception as exc:
            status.error = str(exc)
        return status

    async def confirm_device_in_wsl(self, serial: str | None = None, distro: str | None = None, timeout: float = 15.0) -> bool:
        d = get_transfera_wsl_distro() if distro is None else distro
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            status = await self.get_bridge_status(d)
            if status.reachable and status.devices:
                if serial is None:
                    for dev in status.devices:
                        if dev.get("serial"):
                            return True
                else:
                    for dev in status.devices:
                        if dev.get("serial") == serial:
                            return True
            await asyncio.sleep(1.0)
        logger.warning("Device not confirmed in WSL after %.0fs", timeout)
        return False

    async def handle_apple_device_event(self, busid: str, distro: str | None = None) -> Tier2DeviceStatus:
        devices = await self.list_usb_devices()
        target = next((d for d in devices if d.busid == busid), None)
        if not target:
            return Tier2DeviceStatus(busid=busid, error=f"Device {busid} not found")
        if not target.is_apple:
            return Tier2DeviceStatus(busid=busid, error=f"Device {busid} is not Apple (VID:PID={target.vid_pid})")
        status = Tier2DeviceStatus(busid=busid)
        if not target.is_bound:
            status.error = "Device needs bind -- use get_bind_command() to elevate"
            return status
        attach_result = await self.attach_device(busid, distro)
        if not attach_result.attached:
            status.error = f"Attach failed: {attach_result.error}"
            return status
        status.attached = True
        confirmed = await self.confirm_device_in_wsl(distro=distro)
        status.confirmed_in_wsl = confirmed
        if not confirmed:
            status.error = (
                "Device attached but not confirmed reachable in WSL. "
                "Tier 2 may not support this device on this hardware. "
                "Recommend using Tier 1 (Apple driver) instead."
            )
        return status

    async def auto_recover_apple_device(self) -> dict:
        """Self-healing USB passthrough for Apple devices.

        Called when ``usbmuxd`` (TCP 27015) is unreachable and Tier 2 is
        the fallback path.  Scans all USB devices visible to ``usbipd``
        and for every Apple device (VID ``05ac``):

        * If the device is **not bound** — records it in ``needs_bind`` so
          the caller can prompt the user for elevation.
        * If bound but **not attached** — attempts ``usbipd attach``
          automatically.
        * If already **attached** — marks success.

        Returns
        -------
        dict
            ``apple_devices_found`` — count of Apple devices detected.

            ``devices`` — per-device result list with keys ``busid``,
            ``vid_pid``, ``device_name``, ``state``, ``bound``,
            ``attached``, ``attach_result``, ``error``.

            ``attach_errors`` — list of ``{"busid": ..., "error": ...}``
            for devices whose auto-attach failed.

            ``needs_bind`` — list of ``busid`` strings that require a
            prior ``usbipd bind --force`` (elevation needed).

            ``needs_elevation`` — ``True`` when at least one device needs
            bind or attach failed with access-denied.

            ``success`` — ``True`` if at least one Apple device is now
            attached and usable.
        """
        devices = await self.list_usb_devices()
        apple_devices = [d for d in devices if d.is_apple]

        result: dict = {
            "apple_devices_found": len(apple_devices),
            "devices": [],
            "attach_errors": [],
            "needs_bind": [],
            "needs_elevation": False,
            "success": False,
        }

        for dev in apple_devices:
            dev_result: dict = {
                "busid": dev.busid,
                "vid_pid": dev.vid_pid,
                "device_name": dev.device_name,
                "state": dev.state,
                "bound": dev.is_bound,
                "attached": dev.is_attached,
                "attach_result": None,
                "error": None,
            }

            # -- Device is not bound -- record the bind requirement
            if not dev.is_bound:
                result["needs_bind"].append(dev.busid)
                result["needs_elevation"] = True
                dev_result["error"] = (
                    f"Device {dev.busid} ({dev.device_name}) is not bound. "
                    f"Run 'usbipd bind --busid {dev.busid} --force' as Administrator "
                    f"before it can be attached to WSL."
                )
                result["devices"].append(dev_result)
                continue

            # -- Device is already attached -- nothing to do
            if dev.is_attached:
                dev_result["attach_result"] = "already_attached"
                result["success"] = True
                result["devices"].append(dev_result)
                continue

            # -- Bound but not attached -- attempt auto-attach
            try:
                attach_status = await self.attach_device(dev.busid)
                if attach_status.attached:
                    dev_result["attach_result"] = "attached"
                    result["success"] = True
                else:
                    dev_result["attach_result"] = "failed"
                    error_text = attach_status.error or "unknown"
                    dev_result["error"] = error_text
                    result["attach_errors"].append({
                        "busid": dev.busid,
                        "error": error_text,
                    })
                    # Access-denied during attach means elevation needed
                    if "access denied" in error_text.lower() or "5" in error_text:
                        result["needs_elevation"] = True
            except Exception as exc:
                dev_result["attach_result"] = "failed"
                error_text = str(exc)
                dev_result["error"] = error_text
                result["attach_errors"].append({
                    "busid": dev.busid,
                    "error": error_text,
                })

            result["devices"].append(dev_result)

        if not apple_devices:
            result["needs_elevation"] = False

        return result
