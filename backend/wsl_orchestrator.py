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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BRIDGE_PORT = 18920
DISTRO_NAME = "Ubuntu"
APPLE_VID = "05ac"
STATE_DIR = Path.home() / ".transfera"
STATE_FILE = STATE_DIR / "tier2_state.json"
BRIDGE_SCRIPT_NAME = "wsl_bridge.py"
BRIDGE_INSTALL_PATH = "/opt/transfera-bridge"


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
        self.saved_at = datetime.now(timezone.utc).isoformat()
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


async def _run_cmd(
    *args: str,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
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

    async def _find_distro(self) -> str | None:
        """Return installed WSL distro name, or None if none found. Cached 30s."""
        now = asyncio.get_event_loop().time()
        if self._cached_distro_name is not None and now - self._distro_cache_time < 30:
            return self._cached_distro_name
        try:
            rc, out, _ = await _run_cmd("wsl", "--list", "--verbose", timeout=10)
            if rc == 0 and out.strip():
                # First pass: look for the preferred distro (Ubuntu) by name
                for line in out.splitlines():
                    name = _is_valid_distro_line(line)
                    if name and name.lower() == DISTRO_NAME.lower():
                        self._cached_distro_name = DISTRO_NAME
                        self._distro_cache_time = now
                        return self._cached_distro_name
                # Second pass: take any registered distro
                for line in out.splitlines():
                    name = _is_valid_distro_line(line)
                    if name:
                        self._cached_distro_name = name
                        self._distro_cache_time = now
                        return self._cached_distro_name
        except Exception:
            pass
        self._cached_distro_name = None
        self._distro_cache_time = now
        return None

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
                for line in out.splitlines():
                    name = _is_valid_distro_line(line)
                    if name and name.lower() == DISTRO_NAME.lower():
                        status.distro_name = DISTRO_NAME
                        if "2" in line:
                            status.distro_ready = True
                        break
                if not status.distro_name:
                    for line in out.splitlines():
                        name = _is_valid_distro_line(line)
                        if name:
                            status.distro_name = name
                            status.distro_ready = True
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
            try:
                rc, out, _ = await _run_cmd(
                    "wsl", "-d", status.distro_name or DISTRO_NAME,
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
                "cmd", "/c",
                'systeminfo | findstr /i "Hyper-V"',
                timeout=30,
            )
            if "Yes" in out or "Able" in out:
                return True
            rc, out, _ = await _run_cmd("wsl", "--status", timeout=10)
            if "virtualization" in out.lower():
                if "enabled" in out.lower() or "available" in out.lower():
                    return True
                if "not enabled" in out.lower() or "not available" in out.lower():
                    return False
            rc, out, _ = await _run_cmd(
                "powershell", "-NoProfile", "-Command",
                "Get-ComputerInfo -Property *HyperV* | Select-Object -ExpandProperty *HyperV*",
                timeout=15,
            )
            if "True" in out:
                return True
        except Exception:
            pass
        return True

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
        except asyncio.TimeoutError:
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
        d = distro or DISTRO_NAME
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
        apt_commands = [
            ("apt-get", "update", "-y", "Update package lists"),
            ("apt-get", "install", "-y", "linux-tools-common", "hwdata", "usbutils", "python3", "python3-pip", "python3-venv", "usbmuxd", "curl", "Install USB/IP tools, Python, usbmuxd"),
        ]
        for cmd_tuple in apt_commands:
            cmd_args = cmd_tuple[:-1]
            desc = cmd_tuple[-1]
            try:
                rc, out, err = await _run_cmd("wsl", "-d", d, "-u", "root", "--", *cmd_args, timeout=120)
                if rc != 0 and "already" not in out.lower():
                    raw = err.strip() or out.strip()
                    if "no distribution with the supplied name" in raw.lower() or "wsl_e_distro_not_found" in raw.lower():
                        hint = (
                            f"The Linux distribution '{d}' is no longer registered. "
                            f"Open a PowerShell terminal and run:\n"
                            f"  wsl --install -d Ubuntu\n"
                            f"then retry this setup step."
                        )
                        return Tier2StepResult(step_id=StepID.PROVISION_LINUX, completed=False, error=hint, details={"steps_completed": steps_completed})
                    return Tier2StepResult(step_id=StepID.PROVISION_LINUX, completed=False, error=f"Failed to {desc}: {raw}", details={"steps_completed": steps_completed})
                steps_completed.append(desc)
            except Exception as exc:
                return Tier2StepResult(step_id=StepID.PROVISION_LINUX, completed=False, error=f"Failed to {desc}: {exc}", details={"steps_completed": steps_completed})

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

        return Tier2StepResult(step_id=StepID.PROVISION_LINUX, completed=True, details={"steps_completed": steps_completed})

    async def start_bridge(self, distro: str | None = None) -> BridgeStatus:
        d = distro or DISTRO_NAME
        status = BridgeStatus()

        # Retry hygiene: kill any bridge from a previous attempt before starting fresh
        existing_reachable = False
        check: BridgeStatus | None = None
        try:
            check = await self.get_bridge_status(d)
            existing_reachable = check.reachable
        except Exception:
            pass

        if existing_reachable and check is not None:
            return check

        # Clean up any leftover bridge process from an earlier (failed) attempt
        await self.stop_bridge(d)

        bridge_path = f"{BRIDGE_INSTALL_PATH}/{BRIDGE_SCRIPT_NAME}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl", "-d", d, "-u", "root", "--",
                "python3", bridge_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info("Bridge process started (pid=%s)", proc.pid)
            # Poll generously — first-time cold start inside WSL can be slow
            for _ in range(120):
                await asyncio.sleep(0.5)
                check = await self.get_bridge_status(d)
                if check.reachable:
                    return check
            status.error = "Bridge started but not reachable after 60 seconds"
        except Exception as exc:
            status.error = str(exc)
        return status

    async def stop_bridge(self, distro: str | None = None) -> None:
        d = distro or DISTRO_NAME
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

    async def cleanup_orphaned_bridge(self, distro: str | None = None) -> None:
        """Check for a leftover bridge process from a previous session and kill it."""
        d = distro or DISTRO_NAME
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
        d = distro or DISTRO_NAME
        status = BridgeStatus()
        reachable, devices = await self._probe_bridge(f"http://127.0.0.1:{BRIDGE_PORT}")
        if reachable:
            status.reachable = True
            status.running = True
            status.devices = devices
            return status
        installed = await self._find_distro()
        if installed is None:
            status.error = "Bridge is not reachable"
            return status
        if distro is None:
            d = installed
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
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status != 200:
                        return False, []
                async with session.get(f"{base_url}/api/ios-devices", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return True, data.get("devices", [])
        except ImportError:
            import urllib.request
            try:
                req = urllib.request.Request(f"{base_url}/health")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        req2 = urllib.request.Request(f"{base_url}/api/ios-devices")
                        with urllib.request.urlopen(req2, timeout=5) as resp2:
                            import json as _json
                            data = _json.loads(resp2.read())
                            return True, data.get("devices", [])
            except Exception:
                pass
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
        d = distro or DISTRO_NAME
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
        d = distro or DISTRO_NAME
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
