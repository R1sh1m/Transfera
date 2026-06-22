"""
Transfera WSL Bridge Service
Runs inside WSL2, exposes the same API shape as the Windows-side
iOS device endpoints. Started automatically by the orchestrator.
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
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger("wsl_bridge")

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse
    import uvicorn
except ImportError:
    logger.error("FastAPI not installed. Run: pip3 install fastapi uvicorn[standard]")
    sys.exit(1)

app = FastAPI(title="Transfera WSL Bridge", version="1.0.0")
BRIDGE_PORT = 18920
PID_FILE = "/tmp/transfera-bridge.pid"


@app.get("/health")
async def health():
    return {"status": "ok", "tier": 2, "wm": "wsl2"}


@app.get("/api/ios-devices")
async def list_devices():
    """Identical response shape to the Windows-side /api/ios-devices."""
    try:
        from pymobiledevice3.usbmux import list_devices
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
    except ImportError:
        return {"available": False, "driver_status": "no_pymobiledevice3", "devices": []}

    try:
        mux_devices = await list_devices()
    except ConnectionFailedToUsbmuxdError:
        return {"available": True, "driver_status": "no_driver", "devices": []}
    except Exception:
        return {"available": True, "driver_status": "no_driver", "devices": []}

    devices = []
    for mux_dev in mux_devices:
        serial = mux_dev.serial
        try:
            lockdown = await asyncio.wait_for(
                create_using_usbmux(serial=serial, autopair=False), timeout=5.0,
            )
            try:
                info = lockdown.short_info
                status = "ready"
                try:
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
        except asyncio.TimeoutError:
            devices.append({
                "serial": serial, "name": "Unknown Device", "model": "iPhone",
                "ios_version": "unknown", "connection_type": "USB", "status": "locked",
            })
        except Exception as exc:
            exc_str = str(exc).lower()
            if "not paired" in exc_str or "trust" in exc_str:
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


@app.post("/api/ios-devices/browse")
async def browse_device(serial: str, path: str = "/"):
    """Browse a directory on the device via AFC."""
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
    except ImportError:
        raise HTTPException(status_code=503, detail="pymobiledevice3 not installed in WSL")

    try:
        lockdown = await asyncio.wait_for(
            create_using_usbmux(serial=serial, autopair=True), timeout=10.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot connect to device: {exc}")

    afc = AfcService(lockdown=lockdown)
    await afc.__aenter__()
    try:
        entries = await afc.listdir(path)
        result = []
        for name in entries:
            full_path = posixpath.join(path, name) if path != "/" else f"/{name}"
            try:
                info = await afc.stat(full_path)
                is_dir = info.get("st_ifmt") == "S_IFDIR"
                size = int(info.get("st_size", 0))
                mtime = info.get("st_mtime")
                mtime_val = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime or 0)
                result.append({"name": name, "path": full_path, "is_dir": is_dir, "size": size, "mtime": mtime_val})
            except Exception:
                result.append({"name": name, "path": full_path, "is_dir": False, "size": 0, "mtime": 0})
        return {"serial": serial, "path": path, "entries": result}
    finally:
        await afc.aclose()
        lockdown.close()


@app.post("/api/ios-devices/file-info")
async def get_file_info(serial: str, path: str = "/"):
    """Get info for a single file/directory."""
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
    except ImportError:
        raise HTTPException(status_code=503, detail="pymobiledevice3 not installed in WSL")

    try:
        lockdown = await asyncio.wait_for(
            create_using_usbmux(serial=serial, autopair=True), timeout=10.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot connect to device: {exc}")

    afc = AfcService(lockdown=lockdown)
    await afc.__aenter__()
    try:
        info = await afc.stat(path)
        is_dir = info.get("st_ifmt") == "S_IFDIR"
        size = int(info.get("st_size", 0))
        mtime = info.get("st_mtime")
        mtime_val = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime or 0)
        return {
            "name": posixpath.basename(path), "path": path,
            "is_dir": is_dir, "size": size, "mtime": mtime_val,
        }
    finally:
        await afc.aclose()
        lockdown.close()


@app.get("/api/ios-devices/file/{serial}/{path:path}")
async def read_file(serial: str, path: str):
    """Stream a file from the device."""
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
    except ImportError:
        raise HTTPException(status_code=503, detail="pymobiledevice3 not installed in WSL")

    try:
        lockdown = await asyncio.wait_for(
            create_using_usbmux(serial=serial, autopair=True), timeout=10.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot connect to device: {exc}")

    afc = AfcService(lockdown=lockdown)
    await afc.__aenter__()
    try:
        data = await afc.get_file_contents(path)
        return StreamingResponse(iter([data]), media_type="application/octet-stream")
    finally:
        await afc.aclose()
        lockdown.close()


def _ensure_usbmuxd():
    """Start usbmuxd daemon if not running."""
    if Path("/var/run/usbmuxd").exists():
        return
    try:
        import subprocess
        subprocess.Popen(
            ["usbmuxd", "-f"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(10):
            if Path("/var/run/usbmuxd").exists():
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
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    _ensure_usbmuxd()
    Path(PID_FILE).write_text(str(os.getpid()))
    logger.info("Starting Transfera WSL Bridge on port %d", BRIDGE_PORT)
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT, log_level="info")
