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
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

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

# Set once the usbmux-import failure has been logged: the device list is
# polled every few seconds, and repeating this warning would flood the log.
_warned_usbmux_import = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IOS_SOURCE_PREFIX = "ios://"
DCIM_PATH = "/DCIM"

# Bounds for operations with no internal timeouts (patchable in tests).
# Without these, a stalled device hangs the request past the client's
# timeout and surfaces as a bare fetch failure.
_TRUST_PROBE_TIMEOUT = 8.0
_BROWSE_TIMEOUT = 25.0
# A wedged AFC read must fail the file, never hang the batch/transfer:
# full reads get a generous budget (large videos on slow links), prefix
# reads and stats must return quickly.
_READ_TIMEOUT_SECONDS = 300.0
_PARTIAL_READ_TIMEOUT_SECONDS = 60.0
_STAT_TIMEOUT_SECONDS = 60.0


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
# Friendly device model names (lockdown reports ProductType identifiers)
# ---------------------------------------------------------------------------
PRODUCT_TYPE_NAMES: dict[str, str] = {
    "iPhone10,1": "iPhone 8",
    "iPhone10,4": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X",
    "iPhone10,6": "iPhone X",
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd generation)",
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,6": "iPhone SE (3rd generation)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,5": "iPhone 16e",
}


def friendly_device_model(product_type: str | None) -> str:
    """Map a lockdown ProductType identifier to a marketing name.

    Unknown/empty identifiers fall back to the raw value (or "iPhone") so
    future models keep working without a table update.
    """
    if not product_type or not product_type.strip():
        return "iPhone"
    return PRODUCT_TYPE_NAMES.get(product_type.strip(), product_type.strip())


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
            "pymobiledevice3 not installed — iOS device support unavailable (import error: %s)",
            _PYMOBILEDEVICE3_IMPORT_ERROR,
        )
        return []

    try:
        from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import list_devices
    except Exception as exc:
        global _warned_usbmux_import
        if not _warned_usbmux_import:
            _warned_usbmux_import = True
            logger.warning(
                "Failed to import pymobiledevice3 usbmux: %s "
                "(on Windows this usually means pywin32 is missing — "
                "pip install pywin32; further occurrences logged at debug)",
                exc,
            )
        else:
            logger.debug("pymobiledevice3 usbmux import still failing: %s", exc)
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
                model = friendly_device_model(info.get("ProductType", "iPhone"))
                ios_version = info.get("ProductVersion", "unknown")
                connection_type = getattr(mux_dev, "connection_type", "USB")

                # Determine trust status. all_values dumps the whole lockdown
                # domain tree (dozens of USB round trips) with no internal
                # timeout — bound it so a locked/slow device degrades to a
                # clear LOCKED state instead of hanging the request past the
                # client's timeout. Matches the handshake-timeout treatment.
                status = DeviceStatus.READY
                try:
                    _ = await asyncio.wait_for(
                        asyncio.to_thread(lambda: lockdown.all_values),
                        timeout=_TRUST_PROBE_TIMEOUT,
                    )
                except TimeoutError:
                    logger.debug(
                        "Device %s trust probe timed out — treating as locked",
                        serial,
                    )
                    status = DeviceStatus.LOCKED
                except Exception:
                    status = DeviceStatus.NOT_TRUSTED

                logger.info(
                    "Device detected: %s (%s) serial=%s status=%s ios=%s",
                    device_name,
                    model,
                    serial,
                    status.value,
                    ios_version,
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
        except TimeoutError:
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


async def query_lockdown_versions() -> dict[str, dict[str, str]]:
    """
    Best-effort ``{serial: {name, model, ios_version}}`` via usbmux lockdown
    ``short_info`` only — no trust probe.

    Unlike :func:`list_ios_devices` this never runs the multi-second
    ``all_values`` trust check, so it stays cheap enough to call on every
    device-list poll. Devices that are untrusted/locked (or any failure)
    are simply absent from the result. Returns ``{}`` when pymobiledevice3
    or usbmuxd is unavailable.
    """
    if not _PYMOBILEDEVICE3_AVAILABLE:
        return {}
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import list_devices
    except Exception:
        return {}
    try:
        mux_devices = list_devices()
    except Exception:
        return {}
    if not mux_devices:
        return {}

    async def _one(mux_dev) -> tuple[str, dict[str, str] | None]:
        try:
            lockdown = await asyncio.wait_for(
                asyncio.to_thread(create_using_usbmux, serial=mux_dev.serial, autopair=False),
                timeout=2.0,
            )
            try:
                info = lockdown.short_info
                return mux_dev.serial, {
                    "name": info.get("DeviceName", ""),
                    "model": friendly_device_model(info.get("ProductType", "")),
                    "ios_version": info.get("ProductVersion", ""),
                }
            finally:
                lockdown.close()
        except Exception:
            return mux_dev.serial, None

    results = await asyncio.gather(*[_one(m) for m in mux_devices])
    return {serial: info for serial, info in results if info}


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
                "Device not trusted. Please unlock the device and tap 'Trust This Computer' when prompted."
            )
        raise RuntimeError(f"Failed to connect to device: {exc}")

    try:
        afc = AfcService(lockdown=lockdown)
        return afc, lockdown
    except Exception as exc:
        lockdown.close()
        raise RuntimeError(f"Failed to open AFC service: {exc}")


# ---------------------------------------------------------------------------
# Pooled AFC sessions (browse/thumbnail hot path)
# ---------------------------------------------------------------------------
# Every browse/read used to pay a full usbmux + lockdown + pairing handshake
# per call (up to 10 s budget each) — thumbnailing a 100-photo folder meant
# 100 handshakes. The pool keeps up to _AFC_POOL_SIZE live
# (lockdown, AfcService) pairs per serial. pymobiledevice3 services are not
# safe for concurrent use, so each slot has its own asyncio.Lock and a
# caller holds it for the whole op (up to 4-way parallelism per device).
# Slots idle past _AFC_IDLE_TTL_SECONDS are evicted lazily; a failed op
# drops its slot so the next caller reconnects transparently.
_AFC_POOL_SIZE = 4
_AFC_IDLE_TTL_SECONDS = 120.0


@dataclass
class _PooledAfcSlot:
    afc: Any
    lockdown: Any
    lock: asyncio.Lock
    last_used: float
    ephemeral: bool = False


_AFC_POOL: dict[str, list[_PooledAfcSlot]] = {}
_AFC_POOL_GUARD = threading.Lock()


def _sweep_idle_afc_slots(serial: str, now: float) -> None:
    """Close and remove idle-expired slots. Caller must hold _AFC_POOL_GUARD."""
    slots = _AFC_POOL.get(serial)
    if not slots:
        return
    live: list[_PooledAfcSlot] = []
    for slot in slots:
        if slot.lock.locked() or now - slot.last_used < _AFC_IDLE_TTL_SECONDS:
            live.append(slot)
            continue
        for closable in (slot.afc, slot.lockdown):
            try:
                closable.close()
            except Exception:
                pass
    if live:
        _AFC_POOL[serial] = live
    else:
        _AFC_POOL.pop(serial, None)


async def _acquire_afc_slot(serial: str) -> _PooledAfcSlot:
    """Hand out a locked slot: pooled reuse, fresh pooled fill, or one-off.

    Falls back to a non-pooled ephemeral connection when all pooled slots
    are busy (same as legacy behavior) so callers never deadlock waiting.
    """
    now = time.monotonic()
    with _AFC_POOL_GUARD:
        _sweep_idle_afc_slots(serial, now)
        slots = _AFC_POOL.get(serial, [])
        for slot in slots:
            if not slot.lock.locked():
                slot.last_used = now
                locked_slot = slot
                break
        else:
            locked_slot = None
        needs_new_pooled = locked_slot is None and len(slots) < _AFC_POOL_SIZE

    if locked_slot is not None:
        await locked_slot.lock.acquire()
        return locked_slot

    # Connect outside the guard (network I/O must never hold it).
    afc, lockdown = await _get_afc_service(serial)
    fresh = _PooledAfcSlot(afc=afc, lockdown=lockdown, lock=asyncio.Lock(), last_used=time.monotonic())
    await fresh.lock.acquire()
    if needs_new_pooled:
        with _AFC_POOL_GUARD:
            # Re-check under guard: a racer may have filled the pool first.
            if len(_AFC_POOL.get(serial, [])) < _AFC_POOL_SIZE:
                _AFC_POOL.setdefault(serial, []).append(fresh)
                return fresh
        # Lost the race: use it once, then close (never pool it).
    fresh.ephemeral = True
    return fresh


def _release_afc_slot(serial: str, slot: _PooledAfcSlot) -> None:
    slot.last_used = time.monotonic()
    if slot.ephemeral:
        for closable in (slot.afc, slot.lockdown):
            try:
                closable.close()
            except Exception:
                pass
    try:
        slot.lock.release()
    except RuntimeError:
        pass


async def _drop_afc_slot(serial: str, slot: _PooledAfcSlot) -> None:
    """Forget a suspect slot (op failed mid-use); the next caller reconnects."""
    with _AFC_POOL_GUARD:
        slots = _AFC_POOL.get(serial, [])
        if slot in slots:
            slots.remove(slot)
        if not slots:
            _AFC_POOL.pop(serial, None)
    for closable in (slot.afc, slot.lockdown):
        try:
            closable.close()
        except Exception:
            pass
    try:
        slot.lock.release()
    except RuntimeError:
        pass


@asynccontextmanager
async def pooled_afc_service(serial: str):
    """Yield a locked AFC service for one op, reusing pooled sessions."""
    slot = await _acquire_afc_slot(serial)
    try:
        yield slot.afc
    except Exception:
        await _drop_afc_slot(serial, slot)
        raise
    else:
        _release_afc_slot(serial, slot)


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

    Uses a pooled AFC session (no per-call handshake).
    """
    async with pooled_afc_service(serial) as afc:
        # AFC listdir/stat have no internal timeouts — a stalled device
        # would hang the request past the client's timeout and surface as
        # a bare fetch failure. Bound the whole walk so callers get a
        # clear 503 ("reconnect and retry") instead.
        try:
            return await asyncio.wait_for(
                _browse_device_directory_inner(afc, path),
                timeout=_BROWSE_TIMEOUT,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"Device stopped responding while listing {path} — reconnect the device and retry."
            ) from exc


async def _browse_device_directory_inner(afc, path: str) -> list[DeviceFileInfo]:
    """AFC walk implementation (bounded by the caller's wait_for)."""
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
            result.append(
                DeviceFileInfo(
                    name=name,
                    path=full_path,
                    is_dir=is_dir,
                    size=size,
                    mtime=mtime_val,
                )
            )
        except Exception:
            # Can't stat — still list it
            result.append(
                DeviceFileInfo(
                    name=name,
                    path=full_path,
                    is_dir=False,
                    size=0,
                    mtime=0,
                )
            )
    return result


async def get_device_file_info(serial: str, path: str) -> DeviceFileInfo:
    """Get info for a single file/directory on the device (pooled session)."""
    async with pooled_afc_service(serial) as afc:
        info = await asyncio.wait_for(
            asyncio.to_thread(afc.stat, path),
            timeout=_STAT_TIMEOUT_SECONDS,
        )
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


async def read_device_file(serial: str, path: str) -> bytes:
    """
    Read entire file contents from the iOS device.

    Use for small to medium files. For large files, use streaming.
    Uses a pooled AFC session (no per-call handshake). Bounded: a stalled
    read raises TimeoutError (slot dropped) instead of hanging the caller.
    """
    async with pooled_afc_service(serial) as afc:
        return await asyncio.wait_for(
            asyncio.to_thread(afc.get_file_contents, path),
            timeout=_READ_TIMEOUT_SECONDS,
        )


async def read_device_file_partial(serial: str, path: str, max_bytes: int) -> bytes | None:
    """
    Read only the first *max_bytes* bytes of a file on the iOS device.

    Thumbnail fast path: iOS HEIC/JPEG files embed a small JPEG preview in
    their EXIF header, which sits in the first ~128 KB. Uses a pooled AFC
    session (no per-call handshake). Bounded; returns None on any failure.
    """
    try:
        async with pooled_afc_service(serial) as afc:
            handle = await asyncio.wait_for(
                asyncio.to_thread(lambda: afc.fopen(path)),
                timeout=_PARTIAL_READ_TIMEOUT_SECONDS,
            )
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(lambda: afc.fread(handle, max_bytes)),
                    timeout=_PARTIAL_READ_TIMEOUT_SECONDS,
                )
                return data if data else None
            finally:
                try:
                    await asyncio.to_thread(lambda: afc.fclose(handle))
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("read_device_file_partial failed for %s: %s", path, exc)
        return None


async def get_device_info(serial: str) -> dict[str, str]:
    """Get device filesystem info (total capacity, free space, etc.)."""
    async with pooled_afc_service(serial) as afc:
        return await asyncio.wait_for(
            asyncio.to_thread(afc.get_device_info),
            timeout=_STAT_TIMEOUT_SECONDS,
        )


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
        info = await asyncio.wait_for(
            asyncio.to_thread(self._afc.stat, self.path),
            timeout=_STAT_TIMEOUT_SECONDS,
        )
        self._size = int(info.get("st_size", 0))
        self._handle = await asyncio.wait_for(
            asyncio.to_thread(self._afc.fopen, self.path),
            timeout=_STAT_TIMEOUT_SECONDS,
        )
        return self

    async def read(self, n: int = -1) -> bytes:
        """Read up to n bytes. -1 reads all remaining. Bounded per call."""
        if self._afc is None or self._handle is None:
            return b""
        if n == -1:
            n = self._size - self._pos
        if n <= 0:
            return b""
        data = await asyncio.wait_for(
            asyncio.to_thread(self._afc.fread, self._handle, n),
            timeout=_READ_TIMEOUT_SECONDS,
        )
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
    without_prefix = source_path[len(IOS_SOURCE_PREFIX) :]
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
