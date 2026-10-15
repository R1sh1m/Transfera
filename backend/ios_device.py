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
import random
import re
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
# Resilience for the flaky Windows usbmux path: right after plug/unlock the
# phone shows up in WPD instantly but usbmux needs another second or two to
# enumerate it, and a busy usbmux can briefly return [] or refuse the
# lockdown handshake. A few quick retries with a little jitter turn those
# transient misses into a stable Tier 1 row instead of a USB-fallback
# flicker. Total retry budget stays bounded (~1.5 s for empty-list retries,
# ~2 s sleeps across lockdown attempts excluding handshake timeouts).
_LIST_EMPTY_RETRIES = 3
_LIST_EMPTY_RETRY_DELAY = 0.4
_LIST_RETRY_JITTER = 0.15
_LOCKDOWN_TIMEOUT = 2.0
_LOCKDOWN_RETRIES = 2
_DRIVER_SOCKET_TIMEOUT = 2.0
# Trust-probe cache: the all_values dump costs dozens of USB round trips
# (up to _TRUST_PROBE_TIMEOUT each poll). Steady-state polls reuse the
# cached verdict and skip the probe; a full probe runs only for a new
# serial, an explicit force_full_probe (retry/recover), or cache expiry.
_TRUST_CACHE_TTL_SECONDS = 30.0
# Grace window for a recent READY: a single trust-probe *timeout* inside
# this window keeps reporting READY for one poll instead of flickering to
# LOCKED (the cache entry is left untouched so the next poll re-probes
# promptly; a second consecutive timeout is accepted as genuinely locked).
_TRUST_RECENT_READY_WINDOW_SECONDS = 90.0
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
# Trust-probe cache (per serial, short TTL)
# ---------------------------------------------------------------------------
# Maps canonical serial -> (verdict, monotonic timestamp, consecutive
# probe-timeout count). Guarded because enumeration runs lockdown I/O on
# worker threads while the cache itself is touched from the event loop.
_TRUST_CACHE: dict[str, tuple[DeviceStatus, float, int]] = {}
_TRUST_CACHE_GUARD = threading.Lock()


def _retry_delay() -> float:
    """Empty-list/lockdown retry delay with a little jitter."""
    return _LIST_EMPTY_RETRY_DELAY + random.uniform(0, _LIST_RETRY_JITTER)


def _trust_cache_key(serial: str | None) -> str:
    try:
        return canonical_device_serial(serial)
    except Exception:
        return (serial or "").lower()


def _get_cached_trust(serial: str) -> DeviceStatus | None:
    """Fresh cached trust verdict, or None on miss/expiry."""
    key = _trust_cache_key(serial)
    with _TRUST_CACHE_GUARD:
        entry = _TRUST_CACHE.get(key)
    if entry is None:
        return None
    status, timestamp, _streak = entry
    if time.monotonic() - timestamp < _TRUST_CACHE_TTL_SECONDS:
        return status
    return None


def _last_known_trust(serial: str) -> tuple[DeviceStatus | None, float | None, int]:
    """Last cached verdict regardless of TTL -> (status, age_s, timeout_streak)."""
    key = _trust_cache_key(serial)
    with _TRUST_CACHE_GUARD:
        entry = _TRUST_CACHE.get(key)
    if entry is None:
        return None, None, 0
    status, timestamp, streak = entry
    return status, time.monotonic() - timestamp, streak


def _set_cached_trust(serial: str, status: DeviceStatus, timeout_streak: int = 0) -> None:
    with _TRUST_CACHE_GUARD:
        _TRUST_CACHE[_trust_cache_key(serial)] = (status, time.monotonic(), timeout_streak)


def _note_trust_timeout_grace(serial: str) -> None:
    """Record one graced probe timeout without refreshing the verdict timestamp."""
    key = _trust_cache_key(serial)
    with _TRUST_CACHE_GUARD:
        entry = _TRUST_CACHE.get(key)
        if entry is None:
            return
        status, timestamp, streak = entry
        _TRUST_CACHE[key] = (status, timestamp, streak + 1)


def invalidate_trust_cache(serial: str | None = None) -> None:
    """Drop cached trust verdict(s) so the next poll runs a full probe.

    Called on explicit retry/recover paths (e.g. a terminal locked/trust
    error from an operation) and keyed canonically so a Tier-1 UDID
    invalidates the same entry a PnP id would use.
    """
    with _TRUST_CACHE_GUARD:
        if serial is None:
            _TRUST_CACHE.clear()
        else:
            _TRUST_CACHE.pop(_trust_cache_key(serial), None)


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------
async def list_ios_devices(*, force_full_probe: bool = False) -> list[IOSDevice]:
    """
    Enumerate connected iOS devices via usbmux.

    Returns a list of IOSDevice objects. Each device includes its
    real name, UDID, model, iOS version, and connection status.

    The expensive ``all_values`` trust probe is cached per serial for
    ``_TRUST_CACHE_TTL_SECONDS``: steady-state polls reuse the cached
    verdict from ``short_info`` alone. A full probe runs for a new
    serial, when ``force_full_probe`` is set (explicit retry/recover),
    or after cache expiry. A single probe *timeout* never flips a
    recently-READY device to LOCKED without confirmation (one grace
    poll; the next poll re-probes promptly).

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

    mux_devices: list | None = None
    for _attempt in range(_LIST_EMPTY_RETRIES + 1):
        try:
            mux_devices = list_devices()
            break
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
            if _attempt < _LIST_EMPTY_RETRIES:
                logger.debug(
                    "list_devices attempt %d failed (%s) — retrying",
                    _attempt + 1,
                    exc,
                )
                await asyncio.sleep(_retry_delay())
                continue
            logger.warning("Failed to list usbmux devices: %s", exc)
            return []

    if not mux_devices:
        # Transient enumeration race: WPD sees the phone instantly while
        # usbmux needs another second or two after plug/unlock. One poll
        # returning [] must not flip the UI to USB fallback, so retry
        # briefly before accepting "no devices".
        for _retry in range(_LIST_EMPTY_RETRIES):
            await asyncio.sleep(_retry_delay())
            try:
                mux_devices = list_devices()
            except (ConnectionFailedToUsbmuxdError, ConnectionError):
                break
            except Exception as exc:
                logger.debug("usbmux empty-list retry failed: %s", exc)
                break
            if mux_devices:
                logger.debug(
                    "usbmux found %d device(s) after empty-list retry",
                    len(mux_devices),
                )
                break
        if not mux_devices:
            logger.debug("usbmux returned 0 devices")
            return []

    logger.info("usbmux found %d connected device(s)", len(mux_devices))

    async def _create_lockdown_with_retry(serial: str):
        last_exc: Exception | None = None
        for _attempt in range(_LOCKDOWN_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(create_using_usbmux, serial=serial, autopair=False),
                    timeout=_LOCKDOWN_TIMEOUT,
                )
            except TimeoutError as exc:
                last_exc = exc
                if _attempt < _LOCKDOWN_RETRIES:
                    logger.debug(
                        "Device %s lockdown handshake timed out (attempt %d) — retrying",
                        serial,
                        _attempt + 1,
                    )
                    await asyncio.sleep(_retry_delay())
                    continue
                raise
            except Exception as exc:
                exc_str = str(exc).lower()
                # Trust/pairing failures are deterministic — no retry.
                if "not paired" in exc_str or "trust" in exc_str:
                    raise
                if _attempt < _LOCKDOWN_RETRIES:
                    logger.debug(
                        "Device %s lockdown failed (%s, attempt %d) — retrying",
                        serial,
                        exc,
                        _attempt + 1,
                    )
                    await asyncio.sleep(_retry_delay())
                    last_exc = exc
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise TimeoutError(f"lockdown handshake failed for {serial}")

    async def _get_device_info_task(mux_dev) -> IOSDevice:
        serial = mux_dev.serial
        try:
            lockdown = await _create_lockdown_with_retry(serial)
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
                # The verdict is cached per serial: a fresh cached READY
                # skips the probe entirely so steady-state polls stay cheap
                # and cannot flicker on one slow round trip.
                cached_trust = None if force_full_probe else _get_cached_trust(serial)
                if cached_trust is not None:
                    status = cached_trust
                    logger.debug(
                        "Device %s trust verdict reused from cache (%s)",
                        serial,
                        status.value,
                    )
                else:
                    status = DeviceStatus.READY
                    try:
                        _ = await asyncio.wait_for(
                            asyncio.to_thread(lambda: lockdown.all_values),
                            timeout=_TRUST_PROBE_TIMEOUT,
                        )
                        _set_cached_trust(serial, DeviceStatus.READY)
                    except TimeoutError:
                        last, age, streak = _last_known_trust(serial)
                        if (
                            not force_full_probe
                            and last == DeviceStatus.READY
                            and age is not None
                            and age < _TRUST_RECENT_READY_WINDOW_SECONDS
                            and streak < 1
                        ):
                            # Single transient timeout on a recently-READY
                            # phone: keep READY for this poll without
                            # refreshing the cache, so the next poll
                            # re-probes promptly. A second consecutive
                            # timeout is accepted as genuinely locked.
                            logger.debug(
                                "Device %s trust probe timed out %.1fs after READY — "
                                "keeping ready for one poll (grace)",
                                serial,
                                age,
                            )
                            _note_trust_timeout_grace(serial)
                            status = DeviceStatus.READY
                        else:
                            logger.debug(
                                "Device %s trust probe timed out — treating as locked",
                                serial,
                            )
                            _set_cached_trust(serial, DeviceStatus.LOCKED)
                            status = DeviceStatus.LOCKED
                    except Exception:
                        _set_cached_trust(serial, DeviceStatus.NOT_TRUSTED)
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

    # Quick non-blocking check: try to connect to usbmuxd socket.
    # 0.5 s proved too tight on slow machines — a busy usbmuxd would miss
    # the deadline and the UI would flicker to USB fallback for one poll.
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_DRIVER_SOCKET_TIMEOUT)
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


def drop_afc_slots(serial: str | None = None) -> int:
    """Close and drop pooled AFC slots so the next caller reconnects.

    Parameters
    ----------
    serial : str | None
        Device serial/UDID to drop, or None to drop all slots.

    Matching is by canonical equality (see ``canonical_device_serial``):
    passing a Tier-1 UDID drops PnP-keyed slots embedding that UDID and
    vice versa, and UDID case differences are ignored. Ids without an
    embedded UDID match exactly.

    Slots held by an in-flight op are closed and removed from the pool;
    their owner's lock is left alone (the op fails, drops the no-longer
    -pooled slot as a no-op, and releases normally). Returns the number
    of slots dropped.
    """
    with _AFC_POOL_GUARD:
        if serial is None:
            keys = list(_AFC_POOL.keys())
        else:
            try:
                wanted = canonical_device_serial(serial)
            except Exception:
                wanted = serial
            keys = []
            for key in list(_AFC_POOL.keys()):
                try:
                    match = canonical_device_serial(key) == wanted
                except Exception:
                    match = key == serial
                if match:
                    keys.append(key)
        dropped_slots: list[_PooledAfcSlot] = []
        for key in keys:
            slots = _AFC_POOL.pop(key, [])
            dropped_slots.extend(slots)
    for slot in dropped_slots:
        for closable in (slot.afc, slot.lockdown):
            try:
                closable.close()
            except Exception:
                pass
    if dropped_slots:
        logger.info(
            "Dropped %d pooled AFC slot(s)%s",
            len(dropped_slots),
            f" for device {serial}" if serial is not None else "",
        )
    return len(dropped_slots)


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


# ---------------------------------------------------------------------------
# Lightweight AFC keep-alive / warmup (preview hot path)
# ---------------------------------------------------------------------------
# A pooled slot idle past _AFC_IDLE_TTL_SECONDS is evicted, so the first
# preview request after an idle gap pays a full usbmux + lockdown +
# pairing handshake again. ensure_afc_warm() keeps one slot warm with a
# single cheap NOOP (get_device_info): when a slot was used within
# _AFC_WARM_TTL_SECONDS it returns immediately with zero I/O, otherwise
# it performs exactly one bounded op and releases the lock before
# returning — the lock is never held longer than one op.
_AFC_WARM_TTL_SECONDS = 20.0
_AFC_WARM_TIMEOUT_SECONDS = 8.0


def _afc_pool_has_fresh_slot(serial: str, now: float) -> bool:
    """True when a pooled (non-ephemeral) slot was used within the warm TTL."""
    with _AFC_POOL_GUARD:
        slots = _AFC_POOL.get(serial, [])
        return any(not s.ephemeral and now - s.last_used < _AFC_WARM_TTL_SECONDS for s in slots)


async def ensure_afc_warm(serial: str) -> bool:
    """Ensure a live pooled AFC session exists for *serial* (best-effort).

    Returns True when the pool is warm (fresh slot reused or NOOP
    succeeded), False when the device is unreachable — callers must
    proceed with the normal path regardless. Never raises, never holds
    the slot lock beyond the single NOOP op.
    """
    if _afc_pool_has_fresh_slot(serial, time.monotonic()):
        return True
    try:
        async with pooled_afc_service(serial) as afc:
            await asyncio.wait_for(
                asyncio.to_thread(afc.get_device_info),
                timeout=_AFC_WARM_TIMEOUT_SECONDS,
            )
        logger.debug("AFC slot warmed for device %s", serial)
        return True
    except Exception as exc:
        logger.debug("AFC warmup NOOP failed for %s: %s", serial, exc)
        return False


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
# Streaming reads are capped (audit item 5): a wedged AFC handle must fail
# the file, never buffer an unbounded response into memory.
_AFC_MAX_READ_BYTES = 512 * 1024 * 1024
_AFC_CLOSE_TIMEOUT_S = 2.0


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
        # Hard cap so a corrupt size header cannot OOM the transfer.
        n = min(n, _AFC_MAX_READ_BYTES)
        data = await asyncio.wait_for(
            asyncio.to_thread(self._afc.fread, self._handle, n),
            timeout=_READ_TIMEOUT_SECONDS,
        )
        self._pos += len(data)
        return data

    async def close(self, lock_timeout: float = _AFC_CLOSE_TIMEOUT_S):
        """Close the file handle and AFC connection (bounded, audit item 5).

        Each close step gets *lock_timeout* seconds: a wedged device must
        fail teardown fast instead of hanging the batch/shutdown join.
        Never raises.
        """
        try:
            if self._handle is not None and self._afc is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._afc.fclose, self._handle),
                        timeout=lock_timeout,
                    )
                except Exception:
                    pass
        finally:
            self._handle = None
            if self._afc is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._afc.close),
                        timeout=lock_timeout,
                    )
                except Exception:
                    try:
                        self._afc.close()
                    except Exception:
                        pass
                self._afc = None
            if self._lockdown is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._lockdown.close),
                        timeout=lock_timeout,
                    )
                except Exception:
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


_UDID_SEARCH_RE = re.compile(r"[0-9a-fA-F]{40}")


def canonical_device_serial(serial: str | None) -> str:
    """Stable identity for a device across Tier 1 / WPD serial formats.

    A WPD PnP id often embeds the 40-hex UDID among separators; extracting
    it keeps cutoff keys, selection matching, and tier prefs stable when
    the serving tier flaps. Ids without an embedded UDID (SWD#WPDBUSENUM
    form) are returned lowercased — matching the frontend ``canonicalSerial``
    fallback — so equality checks never flip on path/serial case.
    """
    if not serial:
        return ""
    match = _UDID_SEARCH_RE.search(serial)
    if match:
        return match.group(0).lower()
    return serial.lower()


def normalise_ios_selection_path(path: str) -> str:
    """Normalise a user-selected ``ios://`` URI for stable matching.

    Backslashes become forward slashes and the device *serial* segment is
    lowercased (serials are case-insensitive hex/PnP ids); the on-device
    path case (e.g. ``/DCIM``) is preserved verbatim so diagnostics and
    display never show a confusing ``dcim``-vs-``DCIM`` mismatch.
    """
    cleaned = path.replace("\\", "/")
    without_prefix = cleaned[len(IOS_SOURCE_PREFIX) :]
    parts = without_prefix.split("/", 1)
    serial = parts[0].lower()
    rest = f"/{parts[1]}" if len(parts) > 1 else ""
    return f"{IOS_SOURCE_PREFIX}{serial}{rest}"
