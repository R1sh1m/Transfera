"""
Transfera v2 — iOS Device Integration
Provides iPhone/iPad detection, DCIM browsing, and file transfer via AFC.

Requires `pymobiledevice3` (optional dependency) and Apple's official
Windows driver (iTunes or Apple Devices app from the Microsoft Store).

Gracefully degrades if pymobiledevice3 is not installed or no driver
is present — never crashes the app at startup.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional pymobiledevice3 import with graceful fallback
# ---------------------------------------------------------------------------
_PYMOBILEDEVICE3_AVAILABLE = False
_PYMOBILEDEVICE3_IMPORT_ERROR: str | None = None
_PYMOBILEDEVICE3_ENV_INFO: str | None = None
try:
    import pymobiledevice3  # noqa: F401
    _PYMOBILEDEVICE3_AVAILABLE = True
except ImportError as exc:
    import sys as _sys
    _PYMOBILEDEVICE3_IMPORT_ERROR = str(exc)
    _PYMOBILEDEVICE3_ENV_INFO = (
        f"python={_sys.executable}  "
        f"prefix={getattr(_sys, 'prefix', '?')}  "
        f"base_prefix={getattr(_sys, 'base_prefix', '?')}  "
        f"path={_sys.path}"
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IOS_SOURCE_PREFIX = "ios://"
DCIM_PATH = "/DCIM"


# ---------------------------------------------------------------------------
# Error states (three genuinely different states)
# ---------------------------------------------------------------------------
class DeviceStatus(str, Enum):
    READY = "ready"
    NOT_TRUSTED = "not_trusted"
    LOCKED = "locked"
    NO_DRIVER = "no_driver"
    NOT_FOUND = "not_found"
    DISCONNECTED = "disconnected"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class IOSDevice:
    serial: str
    name: str
    model: str
    ios_version: str
    connection_type: str  # "USB" or "Network"
    status: DeviceStatus
    error_detail: str | None = None


@dataclass
class DeviceFileInfo:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------
def is_ios_support_available() -> bool:
    """Check if pymobiledevice3 is installed."""
    if not _PYMOBILEDEVICE3_AVAILABLE:
        logger.warning(
            "pymobiledevice3 not available: %s (env: %s)",
            _PYMOBILEDEVICE3_IMPORT_ERROR,
            _PYMOBILEDEVICE3_ENV_INFO,
        )
    return _PYMOBILEDEVICE3_AVAILABLE


def _require_pymobiledevice3():
    """Raise a clear error if pymobiledevice3 is not installed."""
    if not _PYMOBILEDEVICE3_AVAILABLE:
        raise RuntimeError(
            "iOS device support requires pymobiledevice3. "
            "Install it with: pip install pymobiledevice3\n"
            f"  python executable: {_PYMOBILEDEVICE3_ENV_INFO}"
        )


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------
async def list_ios_devices() -> list[IOSDevice]:
    """
    Enumerate connected iOS devices via usbmux.

    Returns a list of IOSDevice objects. Each device includes its
    real name, UDID, model, iOS version, and connection status.

    Handles the following error states gracefully:
    - pymobiledevice3 not installed → returns empty list
    - No Apple driver installed → returns empty list
    - No devices connected → returns empty list
    - Device not trusted → returns device with status=NOT_TRUSTED
    """
    if not _PYMOBILEDEVICE3_AVAILABLE:
        logger.debug(
            "pymobiledevice3 not installed — iOS device support unavailable "
            "(import error: %s)", _PYMOBILEDEVICE3_IMPORT_ERROR,
        )
        return []

    try:
        from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import list_devices
    except Exception as exc:
        logger.warning("Failed to import pymobiledevice3 usbmux: %s", exc)
        return []

    try:
        mux_devices = list_devices()
    except ConnectionFailedToUsbmuxdError:
        logger.info(
            "usbmuxd not running — Apple Mobile Device Support driver not detected. "
            "Install iTunes or Apple Devices from the Microsoft Store."
        )
        return []
    except ConnectionError:
        logger.info("usbmuxd connection refused — no Apple driver detected")
        return []
    except Exception as exc:
        logger.warning("Failed to list usbmux devices: %s", exc)
        return []

    if not mux_devices:
        logger.debug("usbmux returned 0 devices")
        return []

    logger.info("usbmux found %d connected device(s)", len(mux_devices))

    async def _get_device_info_task(mux_dev) -> IOSDevice:
        serial = mux_dev.serial
        try:
            lockdown = await asyncio.wait_for(
                asyncio.to_thread(create_using_usbmux, serial=serial, autopair=False),
                timeout=2.0,
            )
            try:
                info = lockdown.short_info
                device_name = info.get("DeviceName", "Unknown iPhone")
                model = info.get("ProductType", "iPhone")
                ios_version = info.get("ProductVersion", "unknown")
                connection_type = getattr(mux_dev, "connection_type", "USB")

                # Determine trust status
                status = DeviceStatus.READY
                try:
                    # Attempt to access all_values — this requires trust
                    _ = lockdown.all_values
                except Exception:
                    status = DeviceStatus.NOT_TRUSTED

                logger.info(
                    "Device detected: %s (%s) serial=%s status=%s ios=%s",
                    device_name, model, serial, status.value, ios_version,
                )
                return IOSDevice(
                    serial=serial,
                    name=device_name,
                    model=model,
                    ios_version=ios_version,
                    connection_type=connection_type,
                    status=status,
                )
            finally:
                lockdown.close()
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Device %s timed out during lockdown — device may be locked", serial)
            return IOSDevice(
                serial=serial,
                name="Unknown Device",
                model="iPhone",
                ios_version="unknown",
                connection_type="USB",
                status=DeviceStatus.LOCKED,
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if "not paired" in exc_str or "trust" in exc_str:
                logger.info("Device %s not paired — requires Trust This Computer", serial)
                return IOSDevice(
                    serial=serial,
                    name="Unknown Device",
                    model="iPhone",
                    ios_version="unknown",
                    connection_type="USB",
                    status=DeviceStatus.NOT_TRUSTED,
                )
            else:
                logger.warning("Failed to get info for device %s: %s", serial, exc)
                return IOSDevice(
                    serial=serial,
                    name="Unknown Device",
                    model="iPhone",
                    ios_version="unknown",
                    connection_type="USB",
                    status=DeviceStatus.ERROR,
                )

    tasks = [_get_device_info_task(mux_dev) for mux_dev in mux_devices]
    devices = await asyncio.gather(*tasks)
    return list(devices)


# ---------------------------------------------------------------------------
# Driver status check (independent of device enumeration)
# ---------------------------------------------------------------------------
def check_driver_status() -> str:
    """
    Check whether Apple's Windows driver (usbmuxd) is running.
    Returns one of: "ready", "no_driver", "no_pymobiledevice3".
    """
    if not _PYMOBILEDEVICE3_AVAILABLE:
        return "no_pymobiledevice3"

    try:
        pass
    except Exception:
        return "no_pymobiledevice3"

    # Quick non-blocking check: try to connect to usbmuxd socket
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        # usbmuxd on Windows listens on 127.0.0.1:27015
        sock.connect(("127.0.0.1", 27015))
        sock.close()
        return "ready"
    except (TimeoutError, ConnectionRefusedError, OSError):
        return "no_driver"
    except Exception:
        return "no_driver"


# ---------------------------------------------------------------------------
# AFC file operations (require a connected, trusted device)
# ---------------------------------------------------------------------------
async def _get_afc_service(serial: str):
    """
    Get an AfcService for the specified device.

    Raises clear errors for each failure mode.
    """
    _require_pymobiledevice3()

    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    try:
        lockdown = await asyncio.wait_for(
            asyncio.to_thread(create_using_usbmux, serial=serial, autopair=True),
            timeout=10.0,
        )
    except ConnectionError:
        raise RuntimeError(
            "Cannot connect to device. Ensure Apple Mobile Device Support "
            "is installed (iTunes or Apple Devices from the Microsoft Store) "
            "and the device is connected via USB."
        )
    except TimeoutError:
        raise RuntimeError(
            "Device connection timed out. The device may be locked. "
            "Please unlock the device and tap 'Trust This Computer'."
        )
    except Exception as exc:
        if "not paired" in str(exc).lower() or "trust" in str(exc).lower():
            raise RuntimeError(
                "Device not trusted. Please unlock the device and tap "
                "'Trust This Computer' when prompted."
            )
        raise RuntimeError(f"Failed to connect to device: {exc}")

    try:
        afc = AfcService(lockdown=lockdown)
        return afc, lockdown
    except Exception as exc:
        lockdown.close()
        raise RuntimeError(f"Failed to open AFC service: {exc}")


async def browse_device_directory(serial: str, path: str = "/") -> list[DeviceFileInfo]:
    """
    List contents of a directory on the iOS device.

    Parameters
    ----------
    serial : str
        Device UDID/serial number.
    path : str
        Absolute path on the device (e.g. "/DCIM", "/DCIM/100APPLE").

    Returns
    -------
    list[DeviceFileInfo]
        Directory entries with name, path, is_dir, size, mtime.
    """
    afc, lockdown = await _get_afc_service(serial)
    try:
        entries = await asyncio.to_thread(afc.listdir, path)
        result: list[DeviceFileInfo] = []
        for name in entries:
            full_path = posixpath.join(path, name) if path != "/" else f"/{name}"
            try:
                info = await asyncio.to_thread(afc.stat, full_path)
                is_dir = info.get("st_ifmt") == "S_IFDIR"
                size = int(info.get("st_size", 0))
                mtime = info.get("st_mtime")
                mtime_val = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime or 0)
                result.append(DeviceFileInfo(
                    name=name,
                    path=full_path,
                    is_dir=is_dir,
                    size=size,
                    mtime=mtime_val,
                ))
            except Exception:
                # Can't stat — still list it
                result.append(DeviceFileInfo(
                    name=name,
                    path=full_path,
                    is_dir=False,
                    size=0,
                    mtime=0,
                ))
        return result
    finally:
        afc.close()
        lockdown.close()


async def get_device_file_info(serial: str, path: str) -> DeviceFileInfo:
    """Get info for a single file/directory on the device."""
    afc, lockdown = await _get_afc_service(serial)
    try:
        info = await asyncio.to_thread(afc.stat, path)
        is_dir = info.get("st_ifmt") == "S_IFDIR"
        size = int(info.get("st_size", 0))
        mtime = info.get("st_mtime")
        mtime_val = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime or 0)
        return DeviceFileInfo(
            name=posixpath.basename(path),
            path=path,
            is_dir=is_dir,
            size=size,
            mtime=mtime_val,
        )
    finally:
        afc.close()
        lockdown.close()


async def read_device_file(serial: str, path: str) -> bytes:
    """
    Read entire file contents from the iOS device.

    Use for small to medium files. For large files, use streaming.
    """
    afc, lockdown = await _get_afc_service(serial)
    try:
        return await asyncio.to_thread(afc.get_file_contents, path)
    finally:
        afc.close()
        lockdown.close()


async def get_device_info(serial: str) -> dict[str, str]:
    """Get device filesystem info (total capacity, free space, etc.)."""
    afc, lockdown = await _get_afc_service(serial)
    try:
        return await asyncio.to_thread(afc.get_device_info)
    finally:
        afc.close()
        lockdown.close()


# ---------------------------------------------------------------------------
# Streaming read (for large files in the transfer engine)
# ---------------------------------------------------------------------------
class AFCFileReader:
    """
    Async file-like reader for iOS device files.

    Implements the async read protocol expected by the transfer engine:
    - `read(n)` → bytes
    - `close()` → None
    """

    def __init__(self, serial: str, path: str):
        self.serial = serial
        self.path = path
        self._afc = None
        self._lockdown = None
        self._handle = None
        self._size = 0
        self._pos = 0

    async def open(self):
        """Open the file handle on the device."""
        self._afc, self._lockdown = await _get_afc_service(self.serial)
        info = await asyncio.to_thread(self._afc.stat, self.path)
        self._size = int(info.get("st_size", 0))
        self._handle = await asyncio.to_thread(self._afc.fopen, self.path)
        return self

    async def read(self, n: int = -1) -> bytes:
        """Read up to n bytes. -1 reads all remaining."""
        if self._afc is None or self._handle is None:
            return b""
        if n == -1:
            n = self._size - self._pos
        if n <= 0:
            return b""
        data = await asyncio.to_thread(self._afc.fread, self._handle, n)
        self._pos += len(data)
        return data

    async def close(self):
        """Close the file handle and AFC connection."""
        try:
            if self._handle is not None and self._afc is not None:
                try:
                    await asyncio.to_thread(self._afc.fclose, self._handle)
                except Exception:
                    pass
        finally:
            self._handle = None
            if self._afc is not None:
                try:
                    self._afc.close()
                except Exception:
                    pass
                self._afc = None
            if self._lockdown is not None:
                try:
                    self._lockdown.close()
                except Exception:
                    pass
                self._lockdown = None

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
# Helper: check if a source path is an iOS device path
# ---------------------------------------------------------------------------
def is_ios_source(source_path: str) -> bool:
    """Check if the source path is an iOS device path (ios:// prefix)."""
    return source_path.startswith(IOS_SOURCE_PREFIX)


def parse_ios_source(source_path: str) -> tuple[str, str]:
    """
    Parse an iOS source path into (serial, afc_path).

    Format: ios://<serial>/path/on/device
    Example: ios://ABCDEFGH/DCIM → ("ABCDEFGH", "/DCIM")
    """
    without_prefix = source_path[len(IOS_SOURCE_PREFIX):]
    parts = without_prefix.split("/", 1)
    serial = parts[0]
    path = f"/{parts[1]}" if len(parts) > 1 else "/"
    return serial, path


def is_wpd_device_id(device_id: str) -> bool:
    """Check if a device_id looks like a WPD PnP path instead of an iOS UDID.

    WPD device paths start with "\\\\?\\" and often contain "vid_" (USB VID).
    Real iOS UDIDs are 40-char hex strings or "0000XXXX-XXXXXXXX" format.
    """
    return device_id.startswith("\\\\?\\") or "vid_" in device_id
