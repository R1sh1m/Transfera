"""
Transfera WSL Bridge Service
Runs inside WSL2, exposes the same API shape as the Windows-side
iOS device endpoints. Started automatically by the orchestrator.

This bridge provides the Tier 2 access path: it uses pymobiledevice3
inside WSL2 to talk to an iPhone attached via usbipd-win, bypassing
the Windows WPD/MTP stack entirely.

Error Response Contract (shared with Windows-side Tier2Backend):
  Every endpoint that can fail due to device state MUST return a JSON
  body with at minimum ``{"status": "<state>", "detail": "<message>"}``.

  Status values:
    - "locked"       — device is locked (screen on, passcode required)
    - "not_trusted"  — no pair record or trust handshake incomplete
    - "error"        — transient/recoverable error
    - "not_found"    — requested path does not exist on device
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import signal
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger("wsl_bridge")

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse
except ImportError:
    logger.error("FastAPI not installed. Run: pip3 install fastapi uvicorn[standard]")
    sys.exit(1)

app = FastAPI(title="Transfera WSL Bridge", version="1.0.0")
BRIDGE_PORT = 18920
PID_FILE = "/tmp/transfera-bridge.pid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_error(status: str, detail: str) -> dict:
    """Return a standardised error dict matching the bridge error contract."""
    return {"status": status, "detail": detail}


def _classify_lockdown_error(exc: Exception) -> tuple[str, str]:
    """Classify a lockdown/connection exception into a (status, message) pair.

    Inspects the exception message for known strings from pymobiledevice3
    to determine whether the device is locked, not trusted, or simply
    unreachable.
    """
    exc_str = str(exc).lower()

    # Locked device: lockdown connection times out because the device
    # isn't handing out services while locked.
    if isinstance(exc, asyncio.TimeoutError):
        return (
            "locked",
            "Device connection timed out. The device may be locked. "
            "Please unlock your device and ensure the cable is connected.",
        )

    # Not paired / trust handshake missing
    if "not paired" in exc_str or "trust" in exc_str or "pair" in exc_str:
        return (
            "not_trusted",
            "Device is not trusted. Please unlock your device and tap "
            "'Trust This Computer' when prompted.",
        )

    # Connection refused / usbmuxd not running
    if "connection refused" in exc_str or "usbmux" in exc_str:
        return (
            "error",
            f"Cannot connect to usbmuxd: {exc}. Ensure the device is "
            "connected via USB and usbmuxd is running.",
        )

    # Generic fallback
    return ("error", str(exc)[:500])


# ---------------------------------------------------------------------------
# Device listing
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "tier": 2, "wm": "wsl2"}


@app.get("/api/ios-devices")
async def list_devices():
    """Identical response shape to the Windows-side /api/ios-devices.

    Each device entry includes a ``status`` field that is one of:
      - "ready"        — device is connected, paired, and services are available
      - "locked"       — device is physically locked (passcode required)
      - "not_trusted"  — device pair record is missing/expired
      - "error"        — unexpected failure during lockdown
    """
    try:
        from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import list_devices
    except ImportError:
        return {"available": False, "driver_status": "no_pymobiledevice3", "devices": []}

    try:
        mux_devices = list_devices()
    except ConnectionFailedToUsbmuxdError:
        return {"available": True, "driver_status": "no_driver", "devices": []}
    except Exception:
        return {"available": True, "driver_status": "no_driver", "devices": []}

    devices = []
    for mux_dev in mux_devices:
        serial = mux_dev.serial
        try:
            lockdown = await asyncio.wait_for(
                asyncio.to_thread(create_using_usbmux, serial=serial, autopair=False), timeout=5.0,
            )
            try:
                info = lockdown.short_info
                status = "ready"
                try:
                    # Attempt to access all_values — this requires trust
                    _ = lockdown.all_values
                except Exception:
                    status = "not_trusted"

                devices.append({
                    "serial": serial,
                    "name": info.get("DeviceName", "Unknown Device"),
                    "model": info.get("ProductType", "iPhone"),
                    "ios_version": info.get("ProductVersion", "unknown"),
                    "connection_type": getattr(mux_dev, "connection_type", "USB"),
                    "status": status,
                })
            finally:
                lockdown.close()
        except TimeoutError:
            # Locked device — lockdown connection times out.
            devices.append({
                "serial": serial, "name": "Unknown Device", "model": "iPhone",
                "ios_version": "unknown", "connection_type": "USB", "status": "locked",
            })
        except Exception as exc:
            exc_str = str(exc).lower()
            if "not paired" in exc_str or "trust" in exc_str or "pair" in exc_str:
                devices.append({
                    "serial": serial, "name": "Unknown Device", "model": "iPhone",
                    "ios_version": "unknown", "connection_type": "USB", "status": "not_trusted",
                })
            else:
                devices.append({
                    "serial": serial, "name": "Unknown Device", "model": "iPhone",
                    "ios_version": "unknown", "connection_type": "USB", "status": "error",
                    "error_detail": str(exc)[:200],
                })

    return {"available": True, "driver_status": "ready", "devices": devices}


# ---------------------------------------------------------------------------
# Browse (directory listing)
# ---------------------------------------------------------------------------
@app.post("/api/ios-devices/browse")
async def browse_device(serial: str, path: str = "/"):
    """Browse a directory on the device via AFC.

    Returns structured errors per the bridge error contract:
      - HTTP 423 + ``{"status": "locked", ...}`` for locked devices
      - HTTP 423 + ``{"status": "not_trusted", ...}`` for untrusted devices
      - HTTP 404 + ``{"status": "not_found", ...}`` for missing paths
      - HTTP 503 + ``{"status": "error", ...}`` for transient errors
    """
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail=_build_error("error", "pymobiledevice3 not installed in WSL"),
        )

    # Acquire lockdown connection with a generous timeout.
    # The device may need time to respond if it was just connected.
    lockdown = None
    try:
        lockdown = await asyncio.wait_for(
            asyncio.to_thread(create_using_usbmux, serial=serial, autopair=True), timeout=10.0,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=423,
            detail=_build_error(
                "locked",
                "Device connection timed out via WSL bridge. "
                "The device may be locked. Please unlock it and try again.",
            ),
        )
    except Exception as exc:
        status, message = _classify_lockdown_error(exc)
        if status == "locked":
            raise HTTPException(status_code=423, detail=_build_error(status, message))
        if status == "not_trusted":
            raise HTTPException(status_code=423, detail=_build_error(status, message))
        raise HTTPException(
            status_code=502,
            detail=_build_error(status, f"Cannot connect to device via WSL bridge: {message}"),
        )

    # Open AFC service on the connected device
    afc = AfcService(lockdown=lockdown)
    try:
        entries = await asyncio.to_thread(afc.listdir, path)
        result = []
        for name in entries:
            full_path = posixpath.join(path, name) if path != "/" else f"/{name}"
            try:
                info = await asyncio.to_thread(afc.stat, full_path)
                is_dir = info.get("st_ifmt") == "S_IFDIR"
                size = int(info.get("st_size", 0))
                mtime = info.get("st_mtime")
                mtime_val = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime or 0)
                result.append({"name": name, "path": full_path, "is_dir": is_dir, "size": size, "mtime": mtime_val})
            except Exception:
                # Entry exists but stat failed (e.g. symlink, broken pipe).
                # List it anyway with default metadata so the user can see
                # something is there.
                result.append({"name": name, "path": full_path, "is_dir": False, "size": 0, "mtime": 0})
        return {"serial": serial, "path": path, "entries": result}
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=_build_error("not_found", f"Path not found on device: {path}"),
        )
    except Exception as exc:
        exc_str = str(exc).lower()
        if "lock" in exc_str or "trust" in exc_str:
            raise HTTPException(
                status_code=423,
                detail=_build_error("locked", f"AFC operation failed (device may be locked): {exc}"),
            )
        raise HTTPException(
            status_code=502,
            detail=_build_error("error", f"AFC browse failed: {exc}"),
        )
    finally:
        try:
            afc.close()
        except Exception:
            pass
        if lockdown is not None:
            try:
                lockdown.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# File info (single path stat)
# ---------------------------------------------------------------------------
@app.post("/api/ios-devices/file-info")
async def get_file_info(serial: str, path: str = "/"):
    """Get info for a single file/directory via AFC."""
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail=_build_error("error", "pymobiledevice3 not installed in WSL"),
        )

    lockdown = None
    try:
        lockdown = await asyncio.wait_for(
            asyncio.to_thread(create_using_usbmux, serial=serial, autopair=True), timeout=10.0,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=423,
            detail=_build_error("locked", "Device connection timed out via WSL bridge."),
        )
    except Exception as exc:
        status, message = _classify_lockdown_error(exc)
        raise HTTPException(
            status_code=423 if status in ("locked", "not_trusted") else 502,
            detail=_build_error(status, message),
        )

    afc = AfcService(lockdown=lockdown)
    try:
        info = await asyncio.to_thread(afc.stat, path)
        is_dir = info.get("st_ifmt") == "S_IFDIR"
        size = int(info.get("st_size", 0))
        mtime = info.get("st_mtime")
        mtime_val = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime or 0)
        return {
            "name": posixpath.basename(path), "path": path,
            "is_dir": is_dir, "size": size, "mtime": mtime_val,
        }
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=_build_error("not_found", f"Path not found on device: {path}"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_build_error("error", f"AFC stat failed: {exc}"),
        )
    finally:
        try:
            afc.close()
        except Exception:
            pass
        if lockdown is not None:
            try:
                lockdown.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# File read (streaming)
# ---------------------------------------------------------------------------
@app.get("/api/ios-devices/file/{serial}/{path:path}")
async def read_file(serial: str, path: str):
    """Stream a file from the device via AFC."""
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail=_build_error("error", "pymobiledevice3 not installed in WSL"),
        )

    lockdown = None
    try:
        lockdown = await asyncio.wait_for(
            asyncio.to_thread(create_using_usbmux, serial=serial, autopair=True), timeout=10.0,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=423,
            detail=_build_error("locked", "Device connection timed out via WSL bridge."),
        )
    except Exception as exc:
        status, message = _classify_lockdown_error(exc)
        raise HTTPException(
            status_code=423 if status in ("locked", "not_trusted") else 502,
            detail=_build_error(status, message),
        )

    afc = AfcService(lockdown=lockdown)
    try:
        data = await asyncio.to_thread(afc.get_file_contents, path)
        return StreamingResponse(iter([data]), media_type="application/octet-stream")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=_build_error("not_found", f"File not found on device: {path}"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_build_error("error", f"AFC read failed: {exc}"),
        )
    finally:
        try:
            afc.close()
        except Exception:
            pass
        if lockdown is not None:
            try:
                lockdown.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# usbmuxd daemon management
# ---------------------------------------------------------------------------
def _ensure_usbmuxd():
    """Start usbmuxd daemon if not running."""
    if os.path.exists("/var/run/usbmuxd"):
        return
    try:
        import subprocess
        subprocess.Popen(
            ["usbmuxd", "-f"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(10):
            if os.path.exists("/var/run/usbmuxd"):
                return
            time.sleep(0.5)
    except FileNotFoundError:
        logger.warning("usbmuxd not found -- device listing will not work")
    except Exception as exc:
        logger.warning("Failed to start usbmuxd: %s", exc)


def _cleanup(signum, frame):
    """Remove PID file on exit."""
    try:
        Path(PID_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    print("BRIDGE_STARTING", flush=True)
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    # Write PID file before attempting to bind so the orchestrator can
    # verify the process started even if it crashes immediately after.
    Path(PID_FILE).write_text(str(os.getpid()))
    logger.info("Starting Transfera WSL Bridge on port %d", BRIDGE_PORT)
    _ensure_usbmuxd()

    try:
        uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT, log_level="info")
    except Exception as exc:
        logger.exception("Bridge crashed: %s", exc)
        print(f"BRIDGE_CRASHED: {exc}", flush=True)
        sys.exit(1)
