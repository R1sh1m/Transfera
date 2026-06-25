"""
Transfera v2 -- DeviceBackend Abstraction
Three-tier waterfall: AFC (Apple driver) -> WPD (Windows Portable Devices)
-> WSL2/usbipd-win bridge.  Every device operation flows through this chain
automatically; the caller never picks a backend.

Order rationale:
  1. AFC -- first-party Apple support, fastest and most feature-complete when
     the Apple Mobile Device Support driver is installed.
  2. WPD -- device-agnostic, works via the Windows WPD COM API through a
     small native helper.  Engages when Apple drivers are absent or broken.
  3. WSL2 bridge -- last resort, routes through a Linux userspace stack
     inside WSL2.  Requires usbipd-win and a running bridge.

A per-device preference is persisted (keyed by stable serial, not display
name) so the last-successful tier is tried first on next connection -- but
the full waterfall always runs if that tier fails this time.

A global "prefer_tier2" setting lets users opt out of the Apple driver
entirely (off by default).

iOS Isolation:
  When prefer_tier2 is True AND the serial is recognised as an iOS device
  (40-char hex UDID or UUID pattern), the waterfall bypasses WPD entirely.
  WPD is an MTP-only transport incapable of full iOS filesystem access;
  routing iOS queries through it would always fail or return an empty DCIM-only
  view.  The isolation ensures the Tier 2 WSL bridge (or Tier 1 AFC) is used
  exclusively for genuine iOS devices, while WPD continues to serve other
  MTP devices (cameras, Android phones) unaffected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote as _url_quote

from backend.ios_device import (
    AFCFileReader,
    DeviceFileInfo,
    DeviceStatus,
    IOSDevice,
    check_driver_status,
    is_ios_support_available,
)
from backend.ios_device import (
    browse_device_directory as _browse_tier1,
)
from backend.ios_device import (
    get_device_file_info as _file_info_tier1,
)
from backend.ios_device import (
    list_ios_devices as _list_tier1,
)
from backend.ios_device import (
    read_device_file as _read_tier1,
)

try:
    from pymobiledevice3.exceptions import (
        ConnectionFailedToUsbmuxdError,
        DeviceHasPasscodeSetError,
        FatalPairingError,
        MuxException,
        NotPairedError,
        PairingDialogResponsePendingError,
        PasscodeRequiredError,
        UserDeniedPairingError,
    )
    _HAS_PYMOBILE_EXC = True
except ImportError:
    _HAS_PYMOBILE_EXC = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PREFERENCE_DIR = Path.home() / ".transfera"
_DEVICE_TIER_FILE = _PREFERENCE_DIR / "device_tier_preferences.json"

# Regex for iOS UDID detection.
# iOS UDIDs are either 40-character hex strings or the "0000XXXX-XXXXXXXX"
# UUID format used by newer devices.  WPD device paths start with "\\?\" or
# contain "vid_" (USB VID substring).
_IOS_UDID_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{8})$")
_WPD_PATH_PREFIX = "\\\\?\\"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_ios_serial(serial: str) -> bool:
    """Return True if *serial* looks like an iOS UDID rather than a WPD PnP path.

    iOS UDID formats:
      - Legacy: 40 hex characters (e.g. "a4c1e2f3b5d6789012345678abcdef0123456789")
      - Modern: 8-hex + hyphen + 8-hex (e.g. "00008100-1234ABCD")

    WPD device paths start with ``\\\\?\\`` or contain ``vid_``.
    """
    if serial.startswith(_WPD_PATH_PREFIX) or "vid_" in serial.lower():
        return False
    return bool(_IOS_UDID_RE.match(serial))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class DeviceAccessTier(str, Enum):
    TIER_1 = "tier1"
    TIER_2 = "tier2"
    WPD = "wpd"
    NONE = "none"


# ---------------------------------------------------------------------------
# Tier probe result (for error reporting when both tiers fail)
# ---------------------------------------------------------------------------
@dataclass
class TierProbeResult:
    tier: DeviceAccessTier
    available: bool
    error: str | None = None
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract DeviceBackend protocol
# ---------------------------------------------------------------------------
class DeviceBackend(ABC):
    """
    Abstract interface for device operations.
    Both Tier 1 (Apple driver) and Tier 2 (WSL bridge) implement this.
    """

    @property
    @abstractmethod
    def tier(self) -> DeviceAccessTier:
        """Which tier this backend represents."""
        ...

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this backend is configured and could potentially serve requests."""
        ...

    @abstractmethod
    async def is_available(self) -> TierProbeResult:
        """
        Probe whether this backend can serve requests right now.
        Returns a TierProbeResult with availability + error details.
        """
        ...

    @abstractmethod
    async def list_devices(self) -> list[IOSDevice]:
        """Enumerate connected iOS devices via this backend."""
        ...

    @abstractmethod
    async def browse(self, serial: str, path: str) -> list[DeviceFileInfo]:
        """List contents of a directory on the device."""
        ...

    @abstractmethod
    async def file_info(self, serial: str, path: str) -> DeviceFileInfo:
        """Get metadata for a single file/directory on the device."""
        ...

    @abstractmethod
    async def read_file(self, serial: str, path: str) -> bytes:
        """Read entire file contents from the device."""
        ...

    @abstractmethod
    def create_file_reader(self, serial: str, path: str) -> Any:
        """
        Create an async file-like reader for streaming large files.
        Returns either an AFCFileReader (Tier 1) or _BridgeFileReader (Tier 2).
        """
        ...  # type: ignore[return]


# ---------------------------------------------------------------------------
# Tier 1: Native Windows + Apple driver
# ---------------------------------------------------------------------------
class Tier1Backend(DeviceBackend):
    """Direct access via pymobiledevice3 on Windows (requires Apple Mobile Device Support)."""

    @property
    def tier(self) -> DeviceAccessTier:
        return DeviceAccessTier.TIER_1

    @property
    def is_configured(self) -> bool:
        """Tier 1 is always 'configured' -- availability depends on the driver."""
        return True

    async def is_available(self) -> TierProbeResult:
        if not is_ios_support_available():
            return TierProbeResult(
                tier=self.tier,
                available=False,
                error="pymobiledevice3 not installed",
                details={"import_error": "pymobiledevice3 import failed"},
            )
        status = await asyncio.to_thread(check_driver_status)
        if status == "ready":
            return TierProbeResult(tier=self.tier, available=True)
        return TierProbeResult(
            tier=self.tier,
            available=False,
            error=f"Apple driver status: {status}",
            details={"driver_status": status},
        )

    async def list_devices(self) -> list[IOSDevice]:
        return await _list_tier1()

    async def browse(self, serial: str, path: str) -> list[DeviceFileInfo]:
        return await _browse_tier1(serial, path)

    async def file_info(self, serial: str, path: str) -> DeviceFileInfo:
        return await _file_info_tier1(serial, path)

    async def read_file(self, serial: str, path: str) -> bytes:
        return await _read_tier1(serial, path)

    def create_file_reader(self, serial: str, path: str) -> Any:
        return AFCFileReader(serial, path)


# ---------------------------------------------------------------------------
# Tier 2: WSL2 + usbipd-win bridge
# ---------------------------------------------------------------------------
class Tier2Backend(DeviceBackend):
    """
    Access via WSL2 + usbipd-win bridge.
    Communicates with the bridge service running inside WSL2 via HTTP.
    """

    def __init__(self):
        self._bridge_url: str | None = None

    @property
    def tier(self) -> DeviceAccessTier:
        return DeviceAccessTier.TIER_2

    @property
    def is_configured(self) -> bool:
        """Tier 2 is configured only when the bridge URL is set."""
        return self._bridge_url is not None

    def set_bridge_url(self, url: str) -> None:
        self._bridge_url = url

    async def is_available(self) -> TierProbeResult:
        if self._bridge_url is None:
            return TierProbeResult(
                tier=self.tier,
                available=False,
                error="No bridge URL configured",
            )
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session, session.get(
                f"{self._bridge_url}/api/ios-devices",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    return TierProbeResult(tier=self.tier, available=True)
                return TierProbeResult(
                    tier=self.tier,
                    available=False,
                    error=f"Bridge returned HTTP {resp.status}",
                )
        except Exception as exc:
            return TierProbeResult(
                tier=self.tier,
                available=False,
                error=f"Bridge unreachable: {exc}",
            )

    async def list_devices(self) -> list[IOSDevice]:
        import aiohttp
        devices: list[IOSDevice] = []
        try:
            async with aiohttp.ClientSession() as session, session.get(
                f"{self._bridge_url}/api/ios-devices",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Tier2 list_devices: bridge returned %d: %s",
                        resp.status, body[:200],
                    )
                    return devices
                data = await resp.json()
                for d in data.get("devices", []):
                    devices.append(IOSDevice(
                        serial=d["serial"],
                        name=d.get("name", "Unknown"),
                        model=d.get("model", "iPhone"),
                        ios_version=d.get("ios_version", "unknown"),
                        connection_type=d.get("connection_type", "USB"),
                        status=DeviceStatus(d.get("status", "ready")),
                    ))
        except aiohttp.ClientError as exc:
            logger.warning("Tier2 list_devices: connection failed: %s", exc)
        except Exception as exc:
            logger.warning("Tier2 list_devices: unexpected error: %s", exc)
        return devices

    async def browse(self, serial: str, path: str) -> list[DeviceFileInfo]:
        import aiohttp
        async with aiohttp.ClientSession() as session, session.post(
            f"{self._bridge_url}/api/ios-devices/browse",
            json={"serial": serial, "path": path},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                try:
                    body = await resp.json()
                except Exception:
                    body = {}
                detail = body.get("detail", body.get("message", "Browse failed"))
                status = body.get("status", "error")
                # Map bridge error statuses to appropriate exceptions so the
                # waterfall can detect and propagate them cleanly.
                if status == "locked":
                    raise DeviceLockedError(serial, detail)
                if status == "not_trusted":
                    raise DeviceNotTrustedError(serial, detail)
                raise RuntimeError(detail)
            data = await resp.json()
            return [
                DeviceFileInfo(
                    name=e["name"], path=e["path"],
                    is_dir=e["is_dir"], size=e["size"], mtime=e["mtime"],
                )
                for e in data.get("entries", [])
            ]

    async def file_info(self, serial: str, path: str) -> DeviceFileInfo:
        import aiohttp
        async with aiohttp.ClientSession() as session, session.post(
            f"{self._bridge_url}/api/ios-devices/file-info",
            json={"serial": serial, "path": path},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                error = await resp.json()
                raise RuntimeError(error.get("detail", "File info failed"))
            data = await resp.json()
            return DeviceFileInfo(
                name=data["name"], path=data["path"],
                is_dir=data["is_dir"], size=data["size"], mtime=data["mtime"],
            )

    async def read_file(self, serial: str, path: str) -> bytes:
        import aiohttp
        async with aiohttp.ClientSession() as session, session.get(
            f"{self._bridge_url}/api/ios-devices/file/{_url_quote(serial, safe='')}{path}",
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                try:
                    body = await resp.json()
                    detail = body.get("detail", "")
                except Exception:
                    detail = (await resp.text())[:200]
                raise RuntimeError(
                    f"File read failed (HTTP {resp.status}): {detail}"
                )
            return await resp.read()

    def create_file_reader(self, serial: str, path: str) -> Any:  # type: ignore[override]
        if self._bridge_url is not None:
            return _BridgeFileReader(serial, path, self._bridge_url)
        from backend.ios_device import AFCFileReader
        return AFCFileReader(serial, path)


# ---------------------------------------------------------------------------
# Bridge file reader (Tier 2 streaming)
# ---------------------------------------------------------------------------
class _BridgeFileReader:
    """Async file reader that streams from the WSL bridge instead of buffering the entire file."""

    def __init__(self, serial: str, path: str, bridge_url: str):
        self.serial = serial
        self.path = path
        self._bridge_url = bridge_url
        self._session = None
        self._resp = None
        self._pos = 0
        self._size = 0

    async def open(self):
        import aiohttp
        self._session = aiohttp.ClientSession()
        try:
            self._resp = await self._session.get(
                f"{self._bridge_url}/api/ios-devices/file/{_url_quote(self.serial, safe='')}{self.path}",
                timeout=aiohttp.ClientTimeout(total=60),
            )
            if self._resp.status != 200:
                raise RuntimeError(f"Failed to open file: {self._resp.status}")

            content_length = self._resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    self._size = int(content_length)
                except (ValueError, TypeError):
                    self._size = 0
        except Exception:
            if self._resp is not None:
                self._resp.close()
            if self._session is not None:
                await self._session.close()
            self._resp = None
            self._session = None
            raise
        return self

    async def read(self, n: int = -1) -> bytes:
        if self._resp is None:
            return b""
        if n == -1:
            chunk = await self._resp.content.read()
        elif n <= 0:
            return b""
        else:
            chunk = await self._resp.content.read(n)
        self._pos += len(chunk)
        return chunk

    async def close(self):
        if self._resp is not None:
            self._resp.close()
            self._resp = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._pos = 0

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *args):
        await self.close()

    @property
    def size(self) -> int:
        return self._size

    @property
    def position(self) -> int:
        return self._pos


# ---------------------------------------------------------------------------
# Per-device tier preference persistence
# ---------------------------------------------------------------------------
def _load_device_tier_prefs() -> dict[str, str]:
    """Load persisted per-device tier preferences from disk."""
    if _DEVICE_TIER_FILE.exists():
        try:
            return json.loads(_DEVICE_TIER_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_device_tier_prefs(prefs: dict[str, str]) -> None:
    """Persist per-device tier preferences to disk."""
    _PREFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    _DEVICE_TIER_FILE.write_text(
        json.dumps(prefs, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Structured error types for iOS device access failures
# ---------------------------------------------------------------------------
class DeviceLockedError(RuntimeError):
    """Raised when the iOS device is locked and cannot be accessed."""

    def __init__(self, serial: str, detail: str | None = None):
        self.serial = serial
        self.status = "locked"
        self.message = detail or "Please unlock your device and tap 'Trust This Computer' on your iPhone"
        super().__init__(self.message)


class DeviceNotTrustedError(RuntimeError):
    """Raised when the iOS device has not completed the trust handshake."""

    def __init__(self, serial: str, detail: str | None = None):
        self.serial = serial
        self.status = "not_trusted"
        self.message = detail or "Please tap 'Trust This Computer' on your iPhone"
        super().__init__(self.message)


class WpdDeviceAccessDenied(RuntimeError):
    """Raised when WPD reports access denied for the given device/path."""

    def __init__(self, serial: str, path: str, detail: str | None = None):
        self.serial = serial
        self.path = path
        self.detail = detail or "WPD returned access denied -- device may not support browsing this path"
        super().__init__(self.detail)


# ---------------------------------------------------------------------------
# DeviceBackendManager -- the single entry point (3-step waterfall)
# ---------------------------------------------------------------------------
class DeviceBackendManager:
    """
    Unified entry point for all device operations.

    Waterfall order (default):  AFC -> WPD -> WSL2 bridge
    If prefer_tier2 is set:     WPD -> WSL2 bridge -> AFC
    (AFC is deprioritized, never removed -- it may still work.)

    iOS Isolation:
      When a serial is identified as an iOS device (iOS UDID format) AND
      prefer_tier2 is True, WPD is excluded from the waterfall entirely.
      This prevents WPD's MTP-only DCIM view from masking the full AFC
      filesystem that Tier 2 provides.  WPD remains available for non-iOS
      MTP devices (cameras, Android phones).

    Per-device optimization:
      The last-successful tier for a device is persisted by serial and
      tried first on next connection.  If it fails this time, the full
      waterfall runs.

    Error semantics:
      - "no device found" (backend returned empty list, no exception):
        Not an error.  Don't log as failure.  If no earlier backend found
        a device, silently continue to the next backend.
      - "device found but failed to use" (exception from a backend that
        has a device mapping):  A real failure.  Log it clearly, then
        fall through to the next backend.
      - DeviceLockedError / DeviceNotTrustedError:  Terminal failures that
        should NOT be swallowed or fallen-through.  These bubble up to the
        API layer so the frontend can display the correct prompt.
    """

    def __init__(self):
        self._tier1 = Tier1Backend()
        self._tier2 = Tier2Backend()
        self._wpd = None  # Lazy-initialized to avoid import at module load
        self._device_tier_map: dict[str, DeviceAccessTier] = {}
        self._prefer_tier2: bool = False
        self._wsl_orchestrator = None

        # Load persisted per-device preferences (serial -> tier value)
        self._device_tier_prefs = _load_device_tier_prefs()

        # Tier 2 probe result (error details)
        self._tier2_error: str | None = None

        # Track which serials are iOS UDIDs vs WPD PnP paths so we can
        # apply iOS isolation rules without re-checking every call.
        self._ios_serials: set[str] = set()

        # Auto-activation state -- set after initialize() probes each tier
        self._apple_driver_installable: bool = False
        self._apple_driver_package_name: str | None = None
        self._apple_driver_package_version: str | None = None
        self._pymobiledevice3_installable: bool = False
        self._bridge_auto_started: bool = False
        self._wsl_setup_suggested: bool = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """
        Check which tiers are available at startup.
        Sets up the WSL orchestrator and bridge URL if Tier 2 is reachable.
        Initializes WPD backend if wpd_helper.exe is present.
        """
        # Check Tier 1
        t1_probe = await self._tier1.is_available()
        if t1_probe.available:
            logger.info("DeviceBackend: Tier 1 (Apple driver) available")
        else:
            logger.info("DeviceBackend: Tier 1 (Apple driver) not available: %s", t1_probe.error)
            # Auto-activation: if the driver is missing and winget can install it,
            # set a flag so the frontend can offer a one-click install prompt.
            if t1_probe.error and "no_driver" in t1_probe.error:
                try:
                    from backend.ios_driver_installer import check_winget_available_async, verify_package_async
                    winget_ok, _ = await check_winget_available_async()
                    if winget_ok:
                        pkg = await verify_package_async()
                        if pkg.success:
                            self._apple_driver_installable = True
                            self._apple_driver_package_name = pkg.package_name
                            self._apple_driver_package_version = pkg.version
                            logger.info(
                                "DeviceBackend: Apple driver installable via winget (%s %s)",
                                pkg.package_name or "Apple.AppleMobileDeviceSupport",
                                pkg.version or "latest",
                            )
                except Exception as exc:
                    logger.debug("DeviceBackend: Apple driver install check failed: %s", exc)

            # pymobiledevice3 installability: if the Python package is missing,
            # check whether pip is available so the frontend can offer to
            # install it.
            if t1_probe.error and "import_error" in t1_probe.error:
                import shutil
                pip_path = shutil.which("pip") or shutil.which("pip3")
                if pip_path:
                    self._pymobiledevice3_installable = True
                    logger.info(
                        "DeviceBackend: pymobiledevice3 not installed but pip is available "
                        "at %s", pip_path,
                    )

            # Self-healing: the Apple service may be installed but stopped.
            # Try to restart it before giving up on Tier 1 entirely.
            try:
                from backend.ios_driver_installer import ensure_apple_service_running
                service_result = await ensure_apple_service_running()
                if service_result.state == "running":
                    logger.info(
                        "DeviceBackend: Apple service revived -- re-probing Tier 1"
                    )
                    t1_retry = await self._tier1.is_available()
                    if t1_retry.available:
                        t1_probe = t1_retry
                        logger.info("DeviceBackend: Tier 1 now available after service restart")
                elif service_result.state == "elevation_required":
                    logger.info(
                        "DeviceBackend: Apple service needs elevation to start -- "
                        "frontend will prompt user"
                    )
                elif service_result.state == "not_installed":
                    logger.debug(
                        "DeviceBackend: Apple service not installed -- will use Tier 2"
                    )
            except Exception as exc:
                logger.debug("DeviceBackend: Apple service recovery attempt failed: %s", exc)

        # Check Tier 2
        try:
            from backend.wsl_orchestrator import BRIDGE_PORT, WSLOrchestrator
            self._wsl_orchestrator = WSLOrchestrator()
            status = await self._wsl_orchestrator.get_bridge_status()
            if status.reachable:
                self._tier2.set_bridge_url(f"http://127.0.0.1:{BRIDGE_PORT}")
                self._tier2_error = None
                logger.info("DeviceBackend: Tier 2 (WSL bridge) available")
            else:
                self._tier2_error = status.error or status.last_error
                logger.info("DeviceBackend: Tier 2 bridge not reachable -- checking auto-start")
                # Auto-activation: if WSL distro is ready but bridge isn't running,
                # try to auto-start the bridge.
                try:
                    feasibility = await self._wsl_orchestrator.check_feasibility()
                    if feasibility.distro_ready:
                        logger.info("DeviceBackend: WSL distro ready -- auto-starting bridge")
                        await self._wsl_orchestrator.start_bridge()
                        retry_status = await self._wsl_orchestrator.get_bridge_status()
                        if retry_status.reachable:
                            self._tier2.set_bridge_url(f"http://127.0.0.1:{BRIDGE_PORT}")
                            self._bridge_auto_started = True
                            self._tier2_error = None
                            logger.info("DeviceBackend: Bridge auto-started successfully")
                        else:
                            self._tier2_error = retry_status.error or retry_status.last_error
                            # Bridge start failed -- try USB passthrough recovery
                            # before giving up.  The Apple device may exist in
                            # usbipd but not be attached to WSL.
                            try:
                                recovery = await self._wsl_orchestrator.auto_recover_apple_device()
                                if recovery.get("success"):
                                    logger.info(
                                        "DeviceBackend: Apple device auto-attached via usbipd -- "
                                        "retrying bridge probe"
                                    )
                                    retry2 = await self._wsl_orchestrator.get_bridge_status()
                                    if retry2.reachable:
                                        self._tier2.set_bridge_url(f"http://127.0.0.1:{BRIDGE_PORT}")
                                        self._bridge_auto_started = True
                                        self._tier2_error = None
                                        logger.info("DeviceBackend: Bridge reachable after USB attach")
                                elif recovery.get("needs_bind"):
                                    logger.info(
                                        "DeviceBackend: Apple device needs bind before attach -- "
                                        "busids: %s", recovery["needs_bind"]
                                    )
                                elif recovery.get("needs_elevation"):
                                    logger.info(
                                        "DeviceBackend: Apple device attach needs elevation -- "
                                        "frontend will prompt"
                                    )
                            except Exception as recovery_exc:
                                logger.debug("DeviceBackend: USB passthrough recovery failed: %s", recovery_exc)
                    elif feasibility.wsl_installed or not feasibility.error:
                        self._wsl_setup_suggested = True
                        logger.info(
                            "DeviceBackend: WSL available but not ready -- surfacing setup card"
                        )
                    else:
                        self._wsl_setup_suggested = True
                        logger.info(
                            "DeviceBackend: WSL not installed -- surfacing setup card"
                        )
                except Exception as exc:
                    logger.debug("DeviceBackend: WSL auto-activation check failed: %s", exc)
        except Exception as exc:
            logger.debug("DeviceBackend: Tier 2 initialization failed: %s", exc)

        # Check WPD
        try:
            from backend.wpd_backend import WpdBackend
            self._wpd = WpdBackend()
            if self._wpd.is_configured:
                wpd_probe = await self._wpd.is_available()
                if wpd_probe.available:
                    logger.info("DeviceBackend: WPD backend available")
                else:
                    logger.info("DeviceBackend: WPD backend not available: %s", wpd_probe.error)
            else:
                logger.debug("DeviceBackend: wpd_helper.exe not found")
        except Exception as exc:
            logger.debug("DeviceBackend: WPD backend initialization failed: %s", exc)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @property
    def prefer_tier2(self) -> bool:
        return self._prefer_tier2

    @prefer_tier2.setter
    def prefer_tier2(self, value: bool) -> None:
        self._prefer_tier2 = value
        logger.info("DeviceBackend: prefer_tier2 set to %s", value)

    def reset_device_tier_preferences(self) -> None:
        """Clear all per-device tier preference mappings (in-memory and on disk)."""
        self._device_tier_prefs = {}
        if _DEVICE_TIER_FILE.exists():
            try:
                _DEVICE_TIER_FILE.unlink()
            except OSError as exc:
                logger.warning("Could not delete device tier prefs file: %s", exc)

    async def get_active_tier(self) -> DeviceAccessTier:
        """Return the overall active tier (first *available* backend in waterfall order)."""
        for backend in self._waterfall_order(is_ios_query=False):
            if not backend.is_configured:
                continue
            try:
                probe = await backend.is_available()
                if probe.available:
                    return backend.tier
            except Exception:
                continue
        return DeviceAccessTier.NONE

    def get_device_tier(self, serial: str) -> DeviceAccessTier | None:
        """Return which tier is currently serving a specific device."""
        return self._device_tier_map.get(serial)

    def get_orchestrator(self):
        """Return the WSL orchestrator (for Tier 2 setup routes)."""
        return self._wsl_orchestrator

    # ------------------------------------------------------------------
    # Auto-activation status (probed during initialize)
    # ------------------------------------------------------------------
    @property
    def apple_driver_installable(self) -> bool:
        """True when the Apple driver is missing but winget can install it."""
        return self._apple_driver_installable

    async def recheck_driver_installable(self) -> bool:
        """Re-probe whether the Apple driver is missing and installable via winget.
        Called after a driver install attempt so the frontend can update the banner."""
        self._apple_driver_installable = False
        t1_probe = await self._tier1.is_available()
        if t1_probe.available:
            return False
        if t1_probe.error and "no_driver" in t1_probe.error:
            try:
                from backend.ios_driver_installer import check_winget_available_async, verify_package_async
                winget_ok, _ = await check_winget_available_async()
                if winget_ok:
                    pkg = await verify_package_async()
                    if pkg.success:
                        self._apple_driver_installable = True
                        self._apple_driver_package_name = pkg.package_name
                        self._apple_driver_package_version = pkg.version
                        return True
            except Exception as exc:
                logger.debug("DeviceBackend: recheck_driver_installable failed: %s", exc)
        return False

    @property
    def apple_driver_package_name(self) -> str | None:
        return self._apple_driver_package_name

    @property
    def apple_driver_package_version(self) -> str | None:
        return self._apple_driver_package_version

    @property
    def pymobiledevice3_installable(self) -> bool:
        """True when pymobiledevice3 is missing but pip can install it."""
        return self._pymobiledevice3_installable

    @property
    def bridge_auto_started(self) -> bool:
        """True when the WSL bridge was auto-started during initialize()."""
        return self._bridge_auto_started

    @property
    def wsl_setup_suggested(self) -> bool:
        """True when WSL is not available and the setup wizard should be shown."""
        return self._wsl_setup_suggested

    @property
    def tier2_error(self) -> str | None:
        """Error from the last Tier 2 bridge probe."""
        return self._tier2_error

    @property
    def tier2_available(self) -> bool:
        """Whether the Tier 2 bridge is configured (reachable)."""
        return self._tier2.is_configured

    @property
    def ios_available(self) -> bool:
        """True if any tier has found at least one iOS device."""
        return len(self._device_tier_map) > 0

    # ------------------------------------------------------------------
    # iOS detection helpers
    # ------------------------------------------------------------------
    def _classify_serials(self, devices: list[IOSDevice]) -> None:
        """Update the internal set of known iOS serials from a device list.

        WPD PnP paths (e.g. ``\\\\?\\usb#vid_05ac...``) are excluded;
        only real iOS UDIDs are tracked.  This lets the waterfall skip
        WPD for iOS devices when prefer_tier2 is active.
        """
        for d in devices:
            if is_ios_serial(d.serial):
                self._ios_serials.add(d.serial)
            else:
                self._ios_serials.discard(d.serial)

    def _is_ios_device(self, serial: str) -> bool:
        """Return True if *serial* is known to be an iOS UDID.

        Checks both the runtime set (populated by device listings) and
        the static regex, so a serial is identified as iOS even before
        the first successful listing if it matches the UDID pattern.
        """
        return serial in self._ios_serials or is_ios_serial(serial)

    # ------------------------------------------------------------------
    # Waterfall ordering
    # ------------------------------------------------------------------
    def _waterfall_order(self, is_ios_query: bool = False) -> list[DeviceBackend]:
        """
        Return all backends in the order they should be tried.

        Default:  AFC -> WPD -> WSL2 bridge
        prefer_tier2:  WPD -> WSL2 bridge -> AFC
        (AFC is never removed, just deprioritized when the user opts out.)

        iOS Isolation (is_ios_query=True):
          WPD is EXCLUDED from the chain unconditionally.  WPD can only
          see the DCIM/Photos namespace via MTP and would mask the full
          AFC filesystem that the user actually needs.  Only AFC (Tier 1)
          and the WSL bridge (Tier 2) support complete iOS filesystem
          access.
        """
        all_backends: list[DeviceBackend] = [self._tier1, self._tier2]
        if self._wpd and self._wpd.is_configured:
            all_backends.append(self._wpd)

        if self._prefer_tier2:
            order = [b for b in all_backends if b is not self._tier1] + [self._tier1]
        else:
            result: list[DeviceBackend] = [self._tier1]
            if self._wpd and self._wpd.is_configured:
                result.append(self._wpd)
            result.append(self._tier2)
            order = result

        # iOS isolation: WPD is MTP-only and incapable of full iOS
        # filesystem access.  Strip it unconditionally when the target
        # is an iOS device, regardless of prefer_tier2.
        if is_ios_query:
            order = [b for b in order if b.tier != DeviceAccessTier.WPD]

        return order

    def _resolve_backend(self, serial: str) -> DeviceBackend:
        """
        Resolve which backend to try first for a given device.

        Uses per-device persisted preference as an optimization hint.
        If the preferred backend isn't available, falls through to the
        normal waterfall order -- this method never blocks the chain.

        Strict type filtering:
          If the serial matches an iOS UDID pattern, WPD is NEVER
          returned, regardless of per-device preference or waterfall
          ordering.  WPD is an MTP-only transport that cannot provide
          full iOS filesystem access; routing iOS through it would
          always return truncated results.
        """
        is_ios = self._is_ios_device(serial)
        preferred_tier_str = self._device_tier_prefs.get(serial)
        if preferred_tier_str:
            try:
                preferred_tier = DeviceAccessTier(preferred_tier_str)
            except ValueError:
                preferred_tier = None

            if preferred_tier is not None:
                for backend in self._waterfall_order(is_ios_query=is_ios):
                    if backend.tier == preferred_tier and backend.is_configured:
                        if is_ios and backend.tier == DeviceAccessTier.WPD:
                            break
                        return backend

        order = self._waterfall_order(is_ios_query=is_ios)
        for backend in order:
            if is_ios and backend.tier == DeviceAccessTier.WPD:
                continue
            if backend.is_configured:
                return backend

        return order[0] if order else self._tier1

    def _fallback_chain(self, preferred: DeviceBackend, is_ios: bool) -> list[DeviceBackend]:
        """Return the ordered fallback backends after *preferred*."""
        order = self._waterfall_order(is_ios_query=is_ios)
        try:
            idx = order.index(preferred)
            return order[idx + 1:]
        except ValueError:
            return [b for b in order if b is not preferred]

    # ------------------------------------------------------------------
    # Core operations -- 3-step waterfall with failure semantics
    # ------------------------------------------------------------------
    async def list_devices(
        self,
    ) -> tuple[list[IOSDevice], DeviceAccessTier]:
        """
        Enumerate devices via the 3-step waterfall.

        For each backend in order:
          1. If the backend isn't available, skip it silently.
          2. If it returns devices -> return them immediately.
          3. If it returns an empty list (no device found):
             - Don't log as an error.
             - Continue to next backend.
          4. If it raises an exception (device found but failed to use):
             - Log clearly as a real failure (may indicate missing driver,
               WPD client-info mismatch, etc.).
             - Continue to next backend.
        """
        # This call is NOT ios-specific (it's enumerating ALL devices),
        # so we use the non-iOS-isolated waterfall to let WPD show
        # non-iOS MTP devices too.
        for backend in self._waterfall_order(is_ios_query=False):
            if not backend.is_configured:
                continue

            probe = await backend.is_available()
            if not probe.available:
                logger.debug(
                    "DeviceBackend: skipping %s -- not available: %s",
                    backend.tier.value, probe.error,
                )
                continue

            try:
                devices = await backend.list_devices()
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "DeviceBackend: %s listed devices but failed to use them: %s",
                    backend.tier.value, error_msg,
                )
                continue

            if devices:
                # If a backend found devices, but all of them are in non-ready states
                # (locked, not trusted, or error), and we have fallback backends available,
                # we should continue checking the fallback backends to see if they can
                # access the device in a ready/usable state (e.g. WPD backend).
                all_non_ready = all(d.status in (DeviceStatus.NOT_TRUSTED, DeviceStatus.LOCKED, DeviceStatus.ERROR) for d in devices)
                if all_non_ready:
                    # Check if there are other configured/available backends in the waterfall order
                    has_alternatives = False
                    waterfall = self._waterfall_order(is_ios_query=False)
                    try:
                        current_idx = waterfall.index(backend)
                        for alt_backend in waterfall[current_idx + 1:]:
                            if alt_backend.is_configured:
                                alt_probe = await alt_backend.is_available()
                                if alt_probe.available:
                                    has_alternatives = True
                                    break
                    except ValueError:
                        pass

                    if has_alternatives:
                        logger.info(
                            "DeviceBackend: %s found devices but all are in non-ready states %s. "
                            "Checking fallback backends for a usable connection.",
                            backend.tier.value, [d.status.value for d in devices]
                        )
                        continue

                # Classify serials for iOS isolation tracking
                self._classify_serials(devices)
                found_serials = {d.serial for d in devices}
                stale = [s for s in self._device_tier_map if s not in found_serials]
                for s in stale:
                    del self._device_tier_map[s]
                    self._device_tier_prefs.pop(s, None)
                for d in devices:
                    self._device_tier_map[d.serial] = backend.tier
                    self._device_tier_prefs[d.serial] = backend.tier.value
                _save_device_tier_prefs(self._device_tier_prefs)
                if stale:
                    logger.debug(
                        "DeviceBackend: purged %d stale device(s) from tier map",
                        len(stale),
                    )
                return devices, backend.tier

            logger.debug(
                "DeviceBackend: %s found no devices -- checking next backend",
                backend.tier.value,
            )

        if self._device_tier_map:
            logger.debug(
                "DeviceBackend: clearing %d stale device(s) from tier map (no devices found)",
                len(self._device_tier_map),
            )
            self._device_tier_map.clear()
            self._device_tier_prefs.clear()
            _save_device_tier_prefs(self._device_tier_prefs)

        self._ios_serials.clear()
        return [], DeviceAccessTier.NONE

    # ------------------------------------------------------------------
    # Operation waterfall (browse / file_info / read)
    # ------------------------------------------------------------------
    async def _run_operation(
        self,
        serial: str,
        operation: str,
        fn,
        *args,
    ):
        """
        Run an operation with the 3-step waterfall.

        1. Try the resolved backend (per-device pref or first in chain).
        2. If it fails:
           - DeviceLockedError / DeviceNotTrustedError:  Terminal.
             Do NOT fall through.  Re-raise immediately so the caller
             sees the actual user-facing error, not a generic "all
             backends failed" summary.
           - Other errors:  If the backend has a device mapping (device
             was found but operation failed), log as a real failure.
           - Fall through to the next backend in the chain.
        3. If all fail, raise a single error with per-step details.
        """
        is_ios = self._is_ios_device(serial)
        preferred = self._resolve_backend(serial)
        chain = [preferred] + self._fallback_chain(preferred, is_ios)
        attempted: list[tuple[DeviceAccessTier, str]] = []
        last_exc: Exception | None = None

        for backend in chain:
            if not backend.is_configured:
                continue

            probe = await backend.is_available()
            if not probe.available:
                continue

            try:
                result = await fn(backend, *args)
                self._device_tier_map[serial] = backend.tier
                if backend.tier != preferred.tier:
                    self._device_tier_prefs[serial] = backend.tier.value
                    _save_device_tier_prefs(self._device_tier_prefs)
                return result
            except (DeviceLockedError, DeviceNotTrustedError) as exc:
                # Terminal failure -- do NOT fall through to other backends.
                # The device is either locked or untrusted, and no other tier
                # can bypass that.  Re-raise immediately so the API layer
                # returns the correct status to the frontend.
                logger.warning(
                    "DeviceBackend: %s %s terminal for device %s: %s",
                    backend.tier.value, operation, serial, exc,
                )
                raise
            # -- pymobiledevice3 exception mapping (only when lib is loaded) --
            except Exception as exc:
                if _HAS_PYMOBILE_EXC:
                    if isinstance(exc, (PasscodeRequiredError, DeviceHasPasscodeSetError)):
                        logger.warning(
                            "DeviceBackend: %s %s device %s locked (passcode required): %s",
                            backend.tier.value, operation, serial, exc,
                        )
                        raise DeviceLockedError(
                            serial,
                            detail="Your iPhone is locked. Please unlock it and tap "
                                   "'Trust This Computer' when prompted.",
                        ) from exc
                    if isinstance(exc, (NotPairedError, PairingDialogResponsePendingError,
                                        UserDeniedPairingError, FatalPairingError)):
                        logger.warning(
                            "DeviceBackend: %s %s device %s not trusted: %s",
                            backend.tier.value, operation, serial, exc,
                        )
                        raise DeviceNotTrustedError(
                            serial,
                            detail="Please tap 'Trust This Computer' on your iPhone "
                                   "and enter your passcode, then try again.",
                        ) from exc
                    if isinstance(exc, (MuxException, ConnectionFailedToUsbmuxdError)):
                        error_msg = f"{type(exc).__name__}: {exc}"
                        logger.warning(
                            "DeviceBackend: %s %s usbmux connection failed for %s: %s",
                            backend.tier.value, operation, serial, error_msg,
                        )
                        attempted.append((backend.tier, error_msg))
                        last_exc = exc
                        continue
                # -- end pymobiledevice3 mapping --
                error_msg = f"{type(exc).__name__}: {exc}"
                is_known_device = serial in self._device_tier_map

                if is_known_device:
                    logger.warning(
                        "DeviceBackend: %s %s failed for device %s (was previously connected): %s",
                        backend.tier.value, operation, serial, error_msg,
                    )
                else:
                    logger.debug(
                        "DeviceBackend: %s %s failed for %s: %s",
                        backend.tier.value, operation, serial, error_msg,
                    )

                attempted.append((backend.tier, error_msg))
                last_exc = exc

        lines = [f"All backends failed for {operation}({serial}):"]
        for tier, err in attempted:
            lines.append(f"  {tier.value}: {err}")

        if not attempted:
            lines.append("  (no backends were available to attempt)")

        raise RuntimeError("\n".join(lines)) from last_exc

    async def browse_device(
        self, serial: str, path: str,
    ) -> list[DeviceFileInfo]:
        """Browse a directory on a device with automatic 3-step fallback."""

        # Normalise path separators: iOS and the WSL bridge use forward
        # slashes exclusively.  WPD internally converts, but we normalise
        # at the entry point to prevent backslash contamination.
        normalised_path = path.replace("\\", "/")
        if not normalised_path.startswith("/"):
            normalised_path = f"/{normalised_path}"

        async def _browse(backend: DeviceBackend, p: str):
            return await backend.browse(serial, p)

        return await self._run_operation(serial, "browse", _browse, normalised_path)

    async def get_device_file_info(
        self, serial: str, path: str,
    ) -> DeviceFileInfo:
        """Get file info with automatic 3-step fallback."""

        normalised_path = path.replace("\\", "/")

        async def _file_info(backend: DeviceBackend, p: str):
            return await backend.file_info(serial, p)

        return await self._run_operation(serial, "file_info", _file_info, normalised_path)

    async def read_device_file(
        self, serial: str, path: str,
    ) -> bytes:
        """Read a file from the device with automatic 3-step fallback."""

        normalised_path = path.replace("\\", "/")

        async def _read(backend: DeviceBackend, p: str):
            return await backend.read_file(serial, p)

        return await self._run_operation(serial, "read", _read, normalised_path)

    def create_file_reader(self, serial: str, path: str) -> Any:
        """
        Create an async file-like reader for a device file, dispatching to
        the correct tier based on the per-device tier map.

        The returned object supports the async context manager protocol with
        ``async read(n)`` and a ``size`` property.  Both ``AFCFileReader``
        and ``_BridgeFileReader`` implement this interface.

        Falls back to Tier 1 (AFC) if the device has no tier mapping yet.
        """
        tier = self._device_tier_map.get(serial)

        normalised_path = path.replace("\\", "/")

        if tier == DeviceAccessTier.WPD and self._wpd and self._wpd.is_configured:
            return self._wpd.create_file_reader(serial, normalised_path)

        if tier == DeviceAccessTier.TIER_2 and self._tier2.is_configured:
            return self._tier2.create_file_reader(serial, normalised_path)

        return self._tier1.create_file_reader(serial, normalised_path)

    def create_tier2_afc_reader(self, serial: str, path: str):
        """Create a file reader that goes through the Tier 2 bridge."""
        normalised_path = path.replace("\\", "/")
        if self._tier2._bridge_url:
            return _BridgeFileReader(serial, normalised_path, self._tier2._bridge_url)
        from backend.ios_device import AFCFileReader
        return AFCFileReader(serial, normalised_path)

    # ------------------------------------------------------------------
    # Probe all tiers (for error reporting / diagnostic UI)
    # ------------------------------------------------------------------
    async def probe_all_tiers(self) -> list[TierProbeResult]:
        """Probe all three tiers and return their results."""
        results = []
        for backend in self._waterfall_order(is_ios_query=False):
            results.append(await backend.is_available())
        return results

    # Legacy compatibility -- callers that expect a 2-tuple
    async def probe_both_tiers(self) -> tuple[TierProbeResult, TierProbeResult]:
        """Probe Tier 1 and Tier 2 (for backward-compatible error UI)."""
        t1 = await self._tier1.is_available()
        t2 = await self._tier2.is_available()
        return t1, t2


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_instance: DeviceBackendManager | None = None


def get_device_backend_manager() -> DeviceBackendManager:
    global _instance
    if _instance is None:
        _instance = DeviceBackendManager()
    return _instance
