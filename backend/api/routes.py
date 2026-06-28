"""
Transfera v2 — API Routes
All HTTP endpoints for the Transfera backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import joinedload

from backend.api import websocket as ws_events
from backend.api.auth import require_local_token
from backend.api.rate_limit import per_session_rate_limit
from backend.api.schemas import (
    BatchInfo,
    BatchList,
    ClearResponse,
    ClearSessionsRequest,
    ConfigResponse,
    DeviceImportStateListResponse,
    DeviceImportStateResponse,
    DevicePreferenceRequest,
    DevicePreferenceResponse,
    DirSizeRequest,
    DirSizeResponse,
    DiskSpaceRequest,
    DiskSpaceResponse,
    DuplicateCheckRequest,
    DuplicateEntrySchema,
    DuplicateReportResponse,
    DuplicateResolveRequest,
    FolderMetadataRequest,
    FolderMetadataResponse,
    InstallDriverResponse,
    InstallerStatusResponse,
    IOSBrowseRequest,
    IOSBrowseResponse,
    IOSDeviceFileEntry,
    IOSDeviceInfoRequest,
    IOSDeviceListResponse,
    MediaItemInfo,
    MediaList,
    PackageVerificationResponse,
    PathValidateRequest,
    PathValidateResponse,
    PreflightValidateRequest,
    PreflightValidateResponse,
    PrescanRequest,
    PrescanResponse,
    RecentItemProgress,
    ScanRequest,
    ScanResponse,
    SessionActionResponse,
    SessionCreate,
    SessionInfo,
    SessionList,
    SessionProgressResponse,
)
from backend.api.schemas import (
    IOSDeviceInfo as IOSDeviceInfoSchema,
)
from backend.api.source_types import (
    SourceRefLocal,
    legacy_string_to_source_ref,
    source_ref_to_legacy_string,
)
from backend.api.websocket import manager as ws_manager
from backend.config import (
    AUDIO_EXTENSIONS,
    BATCH_SIZE,
    CACHE_DIR,
    DB_DIR,
    DOCUMENT_EXTENSIONS,
    HOST,
    IMAGE_EXTENSIONS,
    LOCAL_SECRET_TOKEN,
    MAX_RETRY,
    PORT,
    VIDEO_EXTENSIONS,
)
from backend.database.manager import (
    session_scope,
    set_session_field,
)
from backend.database.models import (
    BatchStatus,
    HopStatus,
    MediaItem,
    SessionStatus,
    TransferBatch,
    TransferSession,
)
from backend.device_backend import (
    DeviceLockedError,
    DeviceNotTrustedError,
    WpdDeviceAccessDenied,
    get_device_backend_manager,
)
from backend.engines.batch_manager import create_batches
from backend.engines.cache_manager import cache_batch
from backend.engines.capture_time import extract_capture_datetime
from backend.engines.device_import_state import (
    clear_device_state,
    compute_cutoff_from_session,
    get_cutoff_datetime,
    get_device_state,
    list_all_device_states,
    upsert_device_state,
)
from backend.engines.duplicate_detector import check_batch, prescan_against_library
from backend.engines.importer import (
    import_batch,
    purge_hop1_cache_for_completed_items,
)
from backend.engines.recovery import recover_interrupted_batches
from backend.engines.reporter import generate_session_report
from backend.engines.scanner import scan as run_scan
from backend.engines.source_reader import DeviceSourceReader
from backend.engines.thumbnail_cache import thumbnail_cache
from backend.engines.thumbnail_ops import (
    mark_thumbnail_failed,
    mark_thumbnail_ready,
    resolve_thumbnail_source_path,
)
from backend.engines.thumbnailer import generate_thumbnail_bytes

# Cancellation flag for regenerate_thumbnails background thread.
# Set by clear_library or a new regeneration request to abort the prior run.
_regen_generation = 0
_regen_gen_lock = threading.Lock()
from backend.ios_device import (
    DeviceStatus,
    check_driver_status,
    is_ios_support_available,
    parse_ios_source,
)
from backend.ios_device import (
    list_ios_devices as _list_ios_devices_backend,
)

try:
    from pymobiledevice3.exceptions import (
        AfcException,
        ConnectionFailedToUsbmuxdError,
        DeviceHasPasscodeSetError,
        LockdownError,
        MuxException,
        NotPairedError,
        PasscodeRequiredError,
    )
    _HAS_PYMOBILE_EXC = True
except ImportError:
    _HAS_PYMOBILE_EXC = False
from backend.ios_driver_installer import get_installer_status as _get_status
from backend.ios_driver_installer import install_driver as _install_driver
from backend.ios_driver_installer import verify_package as _verify
from backend.tier2_manager import get_device_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Cooperative cancellation events — set when cancel_session signals a running
# background task to stop.  Checked inside cache_batch / import_batch loops.
_cancellation_events: dict[int, asyncio.Event] = {}

# selected_files are persisted to TransferSession.selected_files_json in the DB.
# No in-memory map needed.

# Thread/Task locks for session state modifications to prevent concurrent start/resume/pause races
_session_locks: dict[int, asyncio.Lock] = {}

# Registry of currently running transfer background tasks (session_id -> Task).
# Used to detect and cancel stale tasks before starting a new one.
_active_tasks: dict[int, asyncio.Task] = {}


def _get_session_lock(session_id: int) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


def _cleanup_session_state(session_id: int) -> None:
    """Remove all in-memory state for a completed/cancelled/failed session."""
    _cancellation_events.pop(session_id, None)
    _session_locks.pop(session_id, None)


# ---------------------------------------------------------------------------
# Health & Lifecycle
# ---------------------------------------------------------------------------
@router.get("/health")
async def health_check() -> dict:
    """Return service health status for frontend polling and startup detection."""
    return {"status": "ok", "version": "2.0"}


@router.post("/shutdown")
async def shutdown_endpoint(background_tasks: BackgroundTasks) -> dict:
    """Cooperatively shut down the application after returning the response."""
    # Close ExifTool session immediately to release resources
    try:
        from backend.engines.metadata_extractor import _exiftool_session
        _exiftool_session.close()
    except Exception:
        pass

    def kill_self():
        import os
        import signal
        import time
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    background_tasks.add_task(kill_self)
    return {"status": "shutting down"}


# ---------------------------------------------------------------------------
# Device Backend Status (auto-activation hints for the frontend)
# ---------------------------------------------------------------------------
@router.get("/device-backend/status")
async def device_backend_status(request: Request) -> dict:
    """Return auto-activation status for Apple driver and WSL bridge.

    The frontend uses these fields to show contextual setup cards or
    one-click install prompts on the Dashboard.
    """
    mgr = get_device_backend_manager()
    init_task = getattr(request.app.state, "device_manager_init_task", None)
    initializing = init_task is not None and not init_task.done()
    active_tier = (await mgr.get_active_tier()).value if not initializing else "none"
    return {
        "apple_driver_installable": mgr.apple_driver_installable,
        "apple_driver_package_name": mgr.apple_driver_package_name,
        "apple_driver_package_version": mgr.apple_driver_package_version,
        "pymobiledevice3_installable": mgr.pymobiledevice3_installable,
        "bridge_auto_started": mgr.bridge_auto_started,
        "wsl_setup_suggested": mgr.wsl_setup_suggested,
        "initializing": initializing,
        "active_tier": active_tier,
        "tier2_available": mgr.tier2_available,
        "tier2_error": mgr.tier2_error,
        "ios_available": mgr.ios_available,
    }


@router.post("/shutdown")
async def api_shutdown() -> dict:
    """
    Graceful shutdown trigger.

    The Electron main process calls this before force-killing the backend
    process tree.  This gives uvicorn and any in-flight work (transfers,
    WPD device queries, etc.) a short cooldown window to wind down.
    """
    async def _do_shutdown():
        await asyncio.sleep(0.5)
        logger.info("Shutdown requested — initiating graceful uvicorn shutdown")
        # Signal uvicorn to stop — this triggers the FastAPI lifespan teardown
        # (ExifTool session close, SQLAlchemy engine dispose) before process exit.
        loop = asyncio.get_event_loop()
        loop.stop()
    asyncio.create_task(_do_shutdown())
    return {"ok": True, "message": "Shutdown initiated"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@router.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    return ConfigResponse(
        port=PORT,
        host=HOST,
        batch_size=BATCH_SIZE,
        max_retry=MAX_RETRY,
        cache_dir=str(CACHE_DIR),
        db_dir=str(DB_DIR),
        image_extensions=sorted(IMAGE_EXTENSIONS),
        video_extensions=sorted(VIDEO_EXTENSIONS),
        audio_extensions=sorted(AUDIO_EXTENSIONS),
        document_extensions=sorted(DOCUMENT_EXTENSIONS),
    )


@router.get("/local-token")
async def get_local_token(request: Request) -> dict:
    """Return the local auth token. Only callable from localhost."""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Only callable from localhost")
    return {"local_secret_token": LOCAL_SECRET_TOKEN}


# ---------------------------------------------------------------------------
# iOS Device Support
# ---------------------------------------------------------------------------
@router.get("/ios-devices", response_model=IOSDeviceListResponse)
async def list_ios_devices() -> IOSDeviceListResponse:
    """List connected iOS devices. Returns availability flag + device list + tier info."""
    manager = get_device_manager()

    # Check Tier 1 availability for the driver_status field
    if is_ios_support_available():
        driver_status = check_driver_status()
    else:
        driver_status = "no_pymobiledevice3"

    # Use unified manager — tries Tier 1, falls back to Tier 2
    devices, tier = await manager.list_devices()

    # Get per-device tier info from the backend manager
    backend_mgr = get_device_backend_manager()

    if not devices and not is_ios_support_available():
        return IOSDeviceListResponse(
            available=False,
            driver_status="no_pymobiledevice3",
            prefer_tier2=backend_mgr.prefer_tier2,
            devices=[],
        )

    return IOSDeviceListResponse(
        available=True,
        driver_status=driver_status,
        prefer_tier2=backend_mgr.prefer_tier2,
        devices=[
            IOSDeviceInfoSchema(
                serial=d.serial,
                name=d.name,
                model=d.model,
                ios_version=d.ios_version,
                connection_type=d.connection_type,
                status=d.status.value,
                error_detail=getattr(d, "error_detail", None),
                active_tier=(backend_mgr.get_device_tier(d.serial) or tier).value,
            )
            for d in devices
        ],
    )


@router.post("/ios-devices/browse")
async def browse_ios_device(req: IOSBrowseRequest) -> IOSBrowseResponse:
    """Browse a directory on a connected iOS device.

    Returns detailed error schemas for device-locked, not-trusted, and
    path-not-found states instead of generic HTTP 400/404, so the
    Electron frontend can display a contextual prompt (e.g. "Please tap
    'Trust This Computer' on your iPhone") rather than a bare error toast.
    """
    # Normalise path separators at the API boundary.  iOS internal paths
    # and the WSL bridge both use forward slashes; backslashes from a
    # Windows file-picker or a WPD-derived path are converted here so
    # that every downstream backend receives a consistent POSIX-style path.
    normalised_path = req.path.replace("\\", "/")
    if not normalised_path.startswith("/"):
        normalised_path = f"/{normalised_path}"
    logger.debug(
        "browse_ios_device: serial=%s path=%r normalised=%r",
        req.serial, req.path, normalised_path,
    )

    manager = get_device_manager()

    # Verify device is connected and ready via unified manager.  Rather
    # than returning a bare 404/400 here, we first sniff the device
    # status from the most recent listing so we can differentiate
    # LOCKED / NOT_TRUSTED states early.
    devices, _ = await manager.list_devices()
    device = next((d for d in devices if d.serial == req.serial), None)

    if device is None:
        # Device not found in ANY tier's listing.  May have been
        # disconnected, or no backend can see it yet.
        raise HTTPException(
            status_code=404,
            detail={
                "status": "disconnected",
                "message": f"Device {req.serial} is not connected or not reachable. "
                "Ensure the device is plugged in via USB and unlocked.",
            },
        )

    # Early-exit for terminal device states that no backend can work around.
    if device.status == DeviceStatus.LOCKED:
        raise HTTPException(
            status_code=423,
            detail={
                "status": "locked",
                "message": "Your iPhone is locked. Please unlock it and tap "
                "'Trust This Computer' when prompted, then try again.",
            },
        )
    if device.status == DeviceStatus.NOT_TRUSTED:
        raise HTTPException(
            status_code=403,
            detail={
                "status": "not_trusted",
                "message": "Please tap 'Trust This Computer' on your iPhone "
                "and enter your passcode, then try again.",
            },
        )

    try:
        entries = await manager.browse_device(req.serial, normalised_path)
    except DeviceLockedError as exc:
        raise HTTPException(
            status_code=423,
            detail={"status": "locked", "message": exc.message},
        )
    except DeviceNotTrustedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"status": "not_trusted", "message": exc.message},
        )
    except WpdDeviceAccessDenied as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "wpd_denied",
                "message": str(exc),
            },
        )
    except RuntimeError as exc:
        error_text = str(exc)
        exc_lower = error_text.lower()
        if "locked" in exc_lower or "lock" in exc_lower:
            raise HTTPException(
                status_code=423,
                detail={
                    "status": "locked",
                    "message": "Your iPhone is locked. Please unlock it and "
                               "tap 'Trust This Computer' when prompted, then try again.",
                },
            )
        if "trust" in exc_lower or "pair" in exc_lower or "paired" in exc_lower:
            raise HTTPException(
                status_code=403,
                detail={
                    "status": "not_trusted",
                    "message": "Please tap 'Trust This Computer' on your "
                               "iPhone and enter your passcode, then try again.",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": error_text},
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "status": "not_found",
                "message": f"Path not found on device: {normalised_path}",
            },
        )
    except Exception as exc:
        if _HAS_PYMOBILE_EXC:
            if isinstance(exc, (PasscodeRequiredError, DeviceHasPasscodeSetError)):
                raise HTTPException(
                    status_code=423,
                    detail={
                        "status": "locked",
                        "message": "Your iPhone is locked. Please unlock it and "
                                   "tap 'Trust This Computer' when prompted, then try again.",
                    },
                )
            if isinstance(exc, NotPairedError):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "status": "not_trusted",
                        "message": "Please tap 'Trust This Computer' on your iPhone "
                                   "and enter your passcode, then try again.",
                    },
                )
            if isinstance(exc, (MuxException, ConnectionFailedToUsbmuxdError)):
                logger.warning(
                    "browse_ios_device: usbmux error for %s at %s: %s",
                    req.serial, normalised_path, exc,
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "mux_error",
                        "message": "Device connection lost. Please check the USB cable "
                                   "and ensure the device is unlocked.",
                    },
                )
            if isinstance(exc, AfcException):
                logger.warning(
                    "browse_ios_device: AFC error for %s at %s: %s",
                    req.serial, normalised_path, exc,
                )
                raise HTTPException(
                    status_code=400,
                    detail={
                        "status": "afc_error",
                        "message": f"File system error: {exc}",
                    },
                )
            if isinstance(exc, LockdownError):
                logger.warning(
                    "browse_ios_device: lockdown error for %s: %s",
                    req.serial, exc,
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "lockdown_error",
                        "message": "Failed to establish a secure session with the device. "
                                   "Please disconnect and reconnect the device.",
                    },
                )
        logger.exception(
            "Unexpected error browsing device %s at %s",
            req.serial, normalised_path,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": f"Unexpected error browsing device: {exc}",
            },
        )

    return IOSBrowseResponse(
        serial=req.serial,
        path=normalised_path,
        entries=[
            IOSDeviceFileEntry(
                name=e.name,
                path=e.path,
                is_dir=e.is_dir,
                size=e.size,
                mtime=e.mtime,
            )
            for e in entries
        ],
    )


@router.post("/ios-devices/file-info")
async def get_ios_device_file_info(req: IOSDeviceInfoRequest) -> IOSDeviceFileEntry:
    """Get file/directory info for a single path on an iOS device.

    Returns structured error responses for locked/not-trusted states
    (HTTP 423) consistent with the browse endpoint.
    """
    manager = get_device_manager()

    normalised_path = req.path.replace("\\", "/")

    devices, _ = await manager.list_devices()
    device = next((d for d in devices if d.serial == req.serial), None)
    if device is None:
        raise HTTPException(status_code=404, detail={"status": "disconnected", "message": "Device not found"})
    if device.status == DeviceStatus.LOCKED:
        raise HTTPException(
            status_code=423,
            detail={
                "status": "locked",
                "message": "Your iPhone is locked. Please unlock it and tap "
                "'Trust This Computer' when prompted, then try again.",
            },
        )
    if device.status == DeviceStatus.NOT_TRUSTED:
        raise HTTPException(
            status_code=403,
            detail={
                "status": "not_trusted",
                "message": "Please tap 'Trust This Computer' on your iPhone "
                "and enter your passcode, then try again.",
            },
        )

    try:
        info = await manager.get_device_file_info(req.serial, normalised_path)
    except DeviceLockedError as exc:
        raise HTTPException(status_code=423, detail={"status": "locked", "message": exc.message})
    except DeviceNotTrustedError as exc:
        raise HTTPException(status_code=403, detail={"status": "not_trusted", "message": exc.message})
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail={"status": "error", "message": str(exc)})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"status": "not_found", "message": f"Path not found on device: {normalised_path}"})

    return IOSDeviceFileEntry(
        name=info.name,
        path=info.path,
        is_dir=info.is_dir,
        size=info.size,
        mtime=info.mtime,
    )


# ---------------------------------------------------------------------------
# iOS Device Recovery (self-healing connectivity)
# ---------------------------------------------------------------------------
@router.post("/ios-devices/recover")
async def recover_ios_device() -> dict:
    """
    Attempt to self-heal iOS device connectivity.

    Two-phase recovery:
      1. Ensure Apple Mobile Device Service is running (Tier 1).
      2. If the Apple service is not installed at all, try usbipd
         passthrough so the WSL bridge (Tier 2) can reach the device.

    Returns a dict with ``service``, ``usb`` sub-results and an ``overall``
    status string the frontend can use to decide what to show.
    """
    mgr = get_device_manager()
    result: dict = {"service": {}, "usb": {}, "overall": "no_recovery_needed"}

    # Phase 1: ensure the Apple Windows service is running
    service = await mgr.ensure_apple_service_running()
    result["service"] = service

    if service["state"] in ("running",):
        result["overall"] = "service_restored"

    # Phase 2: if service is not_installed, try USB passthrough
    if service["state"] == "not_installed":
        usb = await mgr.auto_recover_apple_device()
        result["usb"] = usb
        if usb["success"]:
            result["overall"] = "usb_passthrough_restored"
        elif usb["needs_bind"]:
            result["overall"] = "needs_bind"
        elif usb["needs_elevation"]:
            result["overall"] = "needs_elevation"
        else:
            result["overall"] = "no_device_found"

    if service.get("needs_elevation") and service["state"] != "not_installed":
        result["overall"] = "elevation_required"

    return result


# ---------------------------------------------------------------------------
# iOS Driver Installer (auto-install Apple Mobile Device Support)
# ---------------------------------------------------------------------------
@router.get("/ios-driver/installer-status", response_model=InstallerStatusResponse)
async def get_installer_status() -> InstallerStatusResponse:
    """Check winget availability and current driver status."""
    status = _get_status()
    return InstallerStatusResponse(
        winget_available=status.winget_available,
        winget_version=status.winget_version,
        driver_status=status.driver_status,
    )


@router.post("/ios-driver/verify-package", response_model=PackageVerificationResponse)
async def verify_package() -> PackageVerificationResponse:
    """Verify the Apple.AppleMobileDeviceSupport package exists in winget."""
    result = _verify()
    return PackageVerificationResponse(
        success=result.success,
        package_id=result.package_id,
        package_name=result.package_name,
        version=result.version,
        error=result.error,
    )


@router.post("/ios-driver/install", response_model=InstallDriverResponse)
async def install_driver(_: None = Depends(require_local_token)) -> InstallDriverResponse:
    """
    Install Apple Mobile Device Support via winget (direct subprocess).

    Calls winget directly from Python — no PowerShell Start-Process
    wrapper.  Output is streamed to the backend logs for visibility.
    The winget "already installed" exit code is treated as success.

    After a successful install, re-probes the driver status so the
    frontend banner auto-dismisses.
    """
    result = await asyncio.to_thread(_install_driver, None, 180)

    if result["success"]:
        mgr = get_device_backend_manager()
        await mgr.recheck_driver_installable()
        return InstallDriverResponse(
            success=True,
            exit_code=result["exit_code"],
            message="Apple Mobile Device Support installed.",
        )

    message = result["error"] or "Installation failed"
    raise HTTPException(status_code=400, detail=message)


# ---------------------------------------------------------------------------
# pymobiledevice3 Installer (pip-based, for open-source AFC access)
# ---------------------------------------------------------------------------
@router.post("/pymobiledevice3/install")
async def install_pymobiledevice3() -> dict:
    """
    Install pymobiledevice3 via pip so open-source AFC is available.

    Requires that pip is on PATH.  The frontend should call
    ``/device-backend/status`` after this returns to refresh the
    ``pymobiledevice3_installable`` flag.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "pymobiledevice3",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return {"success": True, "message": "pymobiledevice3 installed"}
        detail = stderr.decode("utf-8", errors="replace").strip()
        return {"success": False, "message": detail or "pip install failed"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


# ---------------------------------------------------------------------------
# Device Backend Preference (Tier 1 vs Tier 2)
# ---------------------------------------------------------------------------
@router.get("/device-preference", response_model=DevicePreferenceResponse)
async def get_device_preference() -> DevicePreferenceResponse:
    """Get the global device backend preference."""
    mgr = get_device_backend_manager()
    return DevicePreferenceResponse(prefer_tier2=mgr.prefer_tier2)


@router.post("/device-preference", response_model=DevicePreferenceResponse)
async def set_device_preference(req: DevicePreferenceRequest) -> DevicePreferenceResponse:
    """
    Set the global device backend preference.

    When prefer_tier2 is True, Tier 2 (open-source WSL bridge) is tried
    first instead of Tier 1 (Apple driver). This is for users who
    specifically don't want Apple's driver installed at all.
    """
    mgr = get_device_backend_manager()
    mgr.prefer_tier2 = req.prefer_tier2
    return DevicePreferenceResponse(prefer_tier2=mgr.prefer_tier2)


# ---------------------------------------------------------------------------
# Device Import State (incremental import tracking)
# ---------------------------------------------------------------------------
@router.get("/device-import-state", response_model=DeviceImportStateListResponse)
async def list_device_import_states() -> DeviceImportStateListResponse:
    """List all devices with stored import state."""
    states = await list_all_device_states()
    return DeviceImportStateListResponse(
        devices=[
            DeviceImportStateResponse(
                device_id=s.device_id,
                device_name=s.device_name,
                last_successful_cutoff=s.last_successful_cutoff,
                last_import_session_id=s.last_import_session_id,
                updated_at=s.updated_at,
            )
            for s in states
        ]
    )


@router.get("/device-import-state/{device_id}", response_model=DeviceImportStateResponse)
async def get_device_import_state(device_id: str) -> DeviceImportStateResponse:
    """Get the import state for a specific device.

    The device_id is URL-decoded by FastAPI automatically.  Device IDs
    from WPD often contain characters like \\, ?, &, #, {, } which
    must be percent-encoded in the URL by the caller.

    Returns an empty state (no 404) for first-time devices that have
    never been connected before — the frontend uses this to decide
    whether a full scan is needed.
    """
    logger.debug("Device import state lookup: device_id=%r", device_id)
    state = await get_device_state(device_id)
    if state is None:
        return DeviceImportStateResponse(
            device_id=device_id,
            device_name=None,
            last_successful_cutoff=None,
            last_import_session_id=None,
            updated_at=None,
        )

    return DeviceImportStateResponse(
        device_id=state.device_id,
        device_name=state.device_name,
        last_successful_cutoff=state.last_successful_cutoff,
        last_import_session_id=state.last_import_session_id,
        updated_at=state.updated_at,
    )


@router.delete("/device-import-state/{device_id}")
async def clear_device_import_state(device_id: str) -> dict:
    """Clear/reset the import state for a device (forces full re-scan)."""
    logger.debug("Device import state clear: device_id=%r", device_id)
    deleted = await clear_device_state(device_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"No import state found for device: {device_id!r}",
        )

    return {"message": f"Import state cleared for device {device_id}"}


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
@router.post("/scan", response_model=ScanResponse)
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks) -> ScanResponse:
    # Resolve source: prefer source_ref, fall back to source_path string
    source_ref = req.source_ref
    if source_ref is None and req.source_path:
        source_ref = legacy_string_to_source_ref(req.source_path)
    if source_ref is None:
        raise HTTPException(status_code=400, detail="Either source_ref or source_path must be provided")

    # For local sources, validate the path exists
    if isinstance(source_ref, SourceRefLocal):
        source = Path(source_ref.path).resolve()
        if not source.exists():
            raise HTTPException(status_code=404, detail=f"Source not found: {source}")
        source_root_str = str(source)
    else:
        source_root_str = source_ref_to_legacy_string(source_ref)

    dest = Path(req.dest_path).resolve() if req.dest_path else CACHE_DIR
    session_name = req.session_name or f"scan-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

    # Create session
    async with session_scope() as session:
        ts = TransferSession(
            session_name=session_name,
            source_root=source_root_str,
            dest_root=str(dest),
        )
        session.add(ts)
        await session.flush()
        session_id = ts.id

    # Run scan in background thread
    background_tasks.add_task(_run_scan_background, session_id, source_root_str)

    return ScanResponse(
        session_id=session_id,
        status="scanning",
        message=f"Scan started for {source_root_str}",
    )


async def _run_scan_background(session_id: int, source_path: str) -> None:
    """Background task: scan source and emit progress events."""
    try:
        item_ids = await run_scan(source_path, session_id=session_id)

        # Create batches
        batch_ids = await create_batches(session_id, item_ids)

        # Emit events
        await ws_events.emit_scan_complete(session_id, len(item_ids))
        async with session_scope() as session:
            result = await session.execute(
                select(TransferBatch).where(TransferBatch.id.in_(batch_ids))
            )
            batches = list(result.scalars().all())

        if len(batches) < len(batch_ids):
            logger.warning(
                "Only retrieved %d of %d batches from database for session %d",
                len(batches), len(batch_ids), session_id
            )

        for batch in batches:
            await ws_events.emit_batch_created(
                session_id, batch.id, batch.batch_number, batch.total_items
            )

        # Mark session as ready
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.total_items = len(item_ids)
                ts.touch()

    except Exception as exc:
        logger.error("Scan background failed for session %d: %s", session_id, exc)
        await ws_events.emit_error(session_id, str(exc))


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------
@router.post("/sessions", response_model=SessionInfo)
async def create_session(req: SessionCreate) -> SessionInfo:
    # Resolve source: prefer source_ref, fall back to source_root string
    source_ref = req.source_ref
    if source_ref is None and req.source_root:
        source_ref = legacy_string_to_source_ref(req.source_root)
    if source_ref is None:
        raise HTTPException(status_code=400, detail="Either source_ref or source_root must be provided")

    # Convert to legacy string for DB storage
    source_root_str = source_ref_to_legacy_string(source_ref)

    if os.path.normpath(source_root_str) == os.path.normpath(req.dest_root):
        raise HTTPException(status_code=400, detail="Source and destination cannot be the same directory")

    async with session_scope() as session:
        ts = TransferSession(
            session_name=req.session_name,
            source_root=source_root_str,
            dest_root=req.dest_root,
            transfer_mode=req.transfer_mode,
            only_new_mode=req.only_new_since_last_import,
            folder_layout=req.folder_layout,
        )
        session.add(ts)
        await session.flush()

        # Persist selected_files to DB for consumption during batch building
        if req.selected_files:
            normalized = []
            for p in req.selected_files:
                if p.startswith("ios://"):
                    normalized.append(p.replace("\\", "/").lower())
                else:
                    normalized.append(str(Path(p).resolve()).lower())
            ts.selected_files_json = json.dumps(normalized)
            logger.info(
                "Session %d: %d selected file(s) persisted to DB",
                ts.id, len(normalized),
            )

        return _session_to_info(ts)


@router.get("/sessions", response_model=SessionList)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> SessionList:
    async with session_scope() as session:
        # Count
        count_q = select(func.count(TransferSession.id))
        total = (await session.execute(count_q)).scalar() or 0

        # Fetch page
        offset = (page - 1) * page_size
        result = await session.execute(
            select(TransferSession)
            .order_by(TransferSession.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = [_session_to_info(ts) for ts in result.scalars().all()]

    return SessionList(
        sessions=items,
        total=total,
    )


@router.get("/sessions/{session_id}", response_model=SessionInfo)
async def get_session_detail(session_id: int) -> SessionInfo:
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_to_info(ts)


@router.post("/sessions/{session_id}/start", response_model=SessionActionResponse)
async def start_session(session_id: int, background_tasks: BackgroundTasks) -> SessionActionResponse:
    lock = _get_session_lock(session_id)
    async with lock:
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if ts.status not in (SessionStatus.CREATED.value, SessionStatus.PAUSED.value):
                raise HTTPException(status_code=400, detail=f"Cannot start session in status: {ts.status}")

            was_paused = ts.status == SessionStatus.PAUSED.value

            ts.status = SessionStatus.RUNNING.value

            if was_paused:
                # Resume — accrue pause time instead of overwriting started_at
                if ts.paused_at:
                    paused = ts.paused_at.replace(tzinfo=UTC) if ts.paused_at.tzinfo is None else ts.paused_at
                    ms = (datetime.now(UTC) - paused).total_seconds() * 1000
                    ts.total_paused_ms += int(ms)
                    ts.paused_at = None
            else:
                # Fresh start
                ts.started_at = datetime.now(UTC)

            ts.touch()

        # If a previous task is still alive (e.g. it didn't see the cancel signal
        # yet because it was mid-I/O), cancel it now and wait briefly for it to exit.
        # This prevents two tasks from running the same session concurrently.
        existing_task = _active_tasks.pop(session_id, None)
        if existing_task is not None and not existing_task.done():
            existing_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(existing_task), timeout=2.0
                )
            except (TimeoutError, asyncio.CancelledError):
                pass  # Task has been cancelled or timed out — proceed

        # Create or reset the cancellation event so the background task can
        # listen for cooperative cancellation during Hop 1 / Hop 2 processing.
        _cancellation_events[session_id] = asyncio.Event()

        background_tasks.add_task(_run_transfer_background, session_id)

        await ws_events.emit_session_started(session_id)
        return SessionActionResponse(session_id=session_id, status="running", message="Session started")


@router.post("/sessions/{session_id}/pause", response_model=SessionActionResponse)
async def pause_session(session_id: int) -> SessionActionResponse:
    lock = _get_session_lock(session_id)
    async with lock:
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if ts.status != SessionStatus.RUNNING.value:
                raise HTTPException(status_code=400, detail=f"Cannot pause session in status: {ts.status}")
            ts.status = SessionStatus.PAUSED.value
            ts.paused_at = datetime.now(UTC)
            ts.touch()

        # Signal the running background task to stop after its current item.
        # The task checks cancel_event in both Hop 1 and Hop 2 inner loops.
        # When it sees the event AND the session status is PAUSED (not CANCELLED),
        # it exits cleanly leaving the batch in a resumable state.
        if session_id in _cancellation_events:
            _cancellation_events[session_id].set()

        await ws_events.emit_session_paused(session_id)
        return SessionActionResponse(session_id=session_id, status="paused", message="Session paused")


@router.post("/sessions/{session_id}/cancel", response_model=SessionActionResponse)
async def cancel_session(session_id: int) -> SessionActionResponse:
    lock = _get_session_lock(session_id)
    async with lock:
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts is None:
                raise HTTPException(status_code=404, detail="Session not found")
            ts.status = SessionStatus.CANCELLED.value
            ts.completed_at = datetime.now(UTC)
            ts.touch()

        # Signal cooperative cancellation so any in-flight Hop 1 / Hop 2
        # item loop can stop early rather than waiting for the batch to finish.
        if session_id in _cancellation_events:
            _cancellation_events[session_id].set()

        # Clean up the lock immediately on cancel — the background task will
        # clean the rest via _cleanup_session_state when it exits.
        _session_locks.pop(session_id, None)

        return SessionActionResponse(session_id=session_id, status="cancelled", message="Session cancelled")


@router.post("/sessions/clear", response_model=ClearResponse)
async def clear_sessions(req: ClearSessionsRequest | None = None, _: None = Depends(require_local_token)) -> ClearResponse:
    """Clear session history and associated data.

    Removes transfer sessions, their batches, and all associated media items.
    Evicts in-memory cached thumbnails for deleted items.  Never touches the
    user's actual transfer destination files.
    """
    older_than_days = req.older_than_days if req else None

    async with session_scope() as session:
        # Build the session query
        q = select(TransferSession)
        if older_than_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
            q = q.where(TransferSession.created_at < cutoff)

        result = await session.execute(q)
        sessions_to_delete = list(result.scalars().all())

        if not sessions_to_delete:
            return ClearResponse(
                message="No sessions to clear",
                sessions_cleared=0,
                batches_cleared=0,
                media_items_cleared=0,
                thumbnails_removed=0,
                cache_files_removed=0,
            )

        session_ids = [s.id for s in sessions_to_delete]

        # Collect item IDs being deleted so we can evict their thumbnails
        item_id_q = select(MediaItem.id).where(
            MediaItem.session_id.in_(session_ids)
        )
        item_id_result = await session.execute(item_id_q)
        item_ids_to_delete = [row[0] for row in item_id_result.all()]
        # Evict from in-memory cache
        thumbnail_cache.evict_items(item_ids_to_delete)

        # Count items and batches to delete
        media_count_q = select(func.count(MediaItem.id)).where(
            MediaItem.session_id.in_(session_ids)
        )
        media_count = (await session.execute(media_count_q)).scalar() or 0

        batch_count_q = select(func.count(TransferBatch.id)).where(
            TransferBatch.session_id.in_(session_ids)
        )
        batch_count = (await session.execute(batch_count_q)).scalar() or 0

        # Delete media items first (FK references batches and sessions)
        await session.execute(
            delete(MediaItem).where(MediaItem.session_id.in_(session_ids))
        )

        # Delete batches (FK references sessions)
        await session.execute(
            delete(TransferBatch).where(TransferBatch.session_id.in_(session_ids))
        )

        # Delete sessions
        await session.execute(
            delete(TransferSession).where(TransferSession.id.in_(session_ids))
        )

        await session.commit()

    # Clean up in-memory state for deleted sessions
    for sid in session_ids:
        _cleanup_session_state(sid)

    # Clear Hop 1 cache directory contents
    cache_files_removed = _clear_cache_dir()

    return ClearResponse(
        message=f"Cleared {len(sessions_to_delete)} session(s)",
        sessions_cleared=len(sessions_to_delete),
        batches_cleared=batch_count,
        media_items_cleared=media_count,
        thumbnails_removed=0,
        cache_files_removed=cache_files_removed,
    )


def _clear_cache_dir() -> int:
    """Recursively clear all contents of the cache directory."""
    cache_files_removed = 0
    if CACHE_DIR.is_dir():
        logger.debug("Clearing cache directory: %s", CACHE_DIR)
        for entry in os.listdir(CACHE_DIR):
            full_path = CACHE_DIR / entry
            if full_path.is_dir():
                try:
                    dir_file_count = sum(1 for _ in full_path.rglob("*") if _.is_file())
                    shutil.rmtree(full_path)
                    logger.debug("Removed cache subdirectory: %s", full_path)
                    cache_files_removed += dir_file_count
                except OSError as e:
                    logger.warning("Failed to remove cache subdirectory %s: %s", full_path, e)
            elif full_path.is_file():
                try:
                    full_path.unlink()
                    logger.debug("Removed cache file: %s", full_path)
                    cache_files_removed += 1
                except OSError as e:
                    logger.warning("Failed to remove cache file %s: %s", full_path, e)
    return cache_files_removed


@router.post("/cache/purge-completed")
async def purge_completed_cache(_: None = Depends(require_local_token)) -> dict:
    """One-time remediation: purge Hop 1 cache files for all items with
    confirmed Hop 2 success (``final_status == COMPLETED``).

    This cleans up accumulated cache files from prior transfers that were
    never cleaned up before the per-item cache cleanup was implemented.
    """
    removed = await purge_hop1_cache_for_completed_items(CACHE_DIR)
    return {
        "message": f"Purged {removed} cache file(s) for completed items",
        "cache_files_removed": removed,
    }


@router.post("/cache/purge-completed/dry-run")
async def purge_completed_cache_dry_run() -> dict:
    """Preview which Hop 1 cache files would be removed without deleting."""
    would_remove = await purge_hop1_cache_for_completed_items(CACHE_DIR, dry_run=True)
    return {
        "message": f"Would remove {would_remove} cache file(s) for completed items",
        "cache_files_removed": would_remove,
    }


async def _apply_duplicate_resolutions(batch_id: int, resolutions: list[dict]) -> None:
    """Apply user's duplicate resolution decisions before hop2 import."""
    skip_ids = {r["item_id"] for r in resolutions if r["action"] == "skip"}
    overwrite_ids = [r["item_id"] for r in resolutions if r["action"] == "overwrite"]

    async with session_scope() as session:
        # Skip: mark items as completed so importer skips them
        for item_id in skip_ids:
            item = await session.get(MediaItem, item_id)
            if item:
                item.hop2_status = HopStatus.COMPLETED.value
                item.final_status = HopStatus.COMPLETED.value
                item.error_message = "Skipped by user (duplicate resolution)"
                item.touch()

        # Overwrite: delete matching archive copies to avoid library duplicates
        for item_id in overwrite_ids:
            item = await session.get(MediaItem, item_id)
            if item:
                from sqlalchemy import or_
                conditions = []
                if item.source_hash:
                    conditions.append(MediaItem.source_hash == item.source_hash)
                if item.session_id:
                    conditions.append(
                        (func.lower(MediaItem.file_name) == func.lower(item.file_name)) &
                        (MediaItem.session_id == item.session_id)
                    )

                if conditions:
                    result = await session.execute(
                        select(MediaItem).where(
                            or_(*conditions),
                            MediaItem.id != item_id,
                            MediaItem.final_status == HopStatus.COMPLETED.value,
                        )
                    )
                    for archive_item in result.scalars().all():
                        # Try to locate and delete physical file on disk first
                        if archive_item.session_id:
                            archive_session = await session.get(TransferSession, archive_item.session_id)
                            if archive_session and archive_session.dest_root:
                                try:
                                    from backend.engines.importer import verify_file_hash
                                    from backend.engines.organizer import build_folder, derive_timestamp

                                    dest_root_path = Path(archive_session.dest_root)
                                    dt = derive_timestamp(archive_item)
                                    folder = build_folder(dest_root_path, dt, archive_session.folder_layout)

                                    base_name = archive_item.file_name
                                    p = Path(base_name)
                                    stem = p.stem
                                    suffix = p.suffix

                                    candidates = [folder / base_name]
                                    for i in range(1, 1000):
                                        candidates.append(folder / f"{stem}_{i:03d}{suffix}")

                                    target_file = None
                                    for cand in candidates:
                                        if cand.is_file():
                                            if archive_item.source_hash:
                                                if verify_file_hash(cand, archive_item.source_hash):
                                                    target_file = cand
                                                    break
                                            elif cand.stat().st_size == archive_item.file_size:
                                                target_file = cand
                                                break

                                    if target_file:
                                        logger.info(
                                            "Deleting physical duplicate file before overwriting: %s",
                                            target_file,
                                        )
                                        target_file.unlink(missing_ok=True)
                                    else:
                                        logger.warning(
                                            "Could not locate physical file for archive item %d (%s) under %s",
                                            archive_item.id,
                                            archive_item.file_name,
                                            folder,
                                        )
                                except Exception as file_exc:
                                    logger.error(
                                        "Error deleting physical file for archive item %d: %s",
                                        archive_item.id,
                                        file_exc,
                                    )

                        # Evict from thumbnail cache
                        thumbnail_cache.evict_items([archive_item.id])

                        # Delete database record
                        await session.delete(archive_item)

        await session.commit()

    if skip_ids or overwrite_ids:
        logger.info(
            "Applied duplicate resolutions for batch %d: %d skip, %d overwrite",
            batch_id, len(skip_ids), len(overwrite_ids),
        )


async def _phase_scan_and_create_batches(
    session_id: int,
    cancel_event: asyncio.Event | None,
) -> list[TransferBatch] | None:
    """Scan source, filter by selected files, create batches.

    Returns the list of pending batches, or None if the scan found 0 media
    items (the session is marked FAILED in that case).
    """
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        source_root = ts.source_root if ts else None
        only_new_mode = ts.only_new_mode if ts else False

        result = await session.execute(
            select(TransferBatch)
            .where(
                TransferBatch.session_id == session_id,
                TransferBatch.status.notin_([
                    BatchStatus.COMPLETED.value,
                    BatchStatus.FAILED.value,
                    BatchStatus.PARTIAL.value,
                ]),
            )
            .order_by(TransferBatch.batch_number)
        )
        existing_batches = list(result.scalars().all())

    if existing_batches:
        return existing_batches

    if not source_root:
        return []

    logger.info("Session %d has no batches — running scan on %s", session_id, source_root)

    # Load selected_files from DB (persisted at session creation)
    selected_set: set[str] | None = None
    async with session_scope() as session:
        ts_sel = await session.get(TransferSession, session_id)
        if ts_sel and ts_sel.selected_files_json:
            raw = json.loads(ts_sel.selected_files_json)
            selected_set = set(raw)
            logger.info(
                "Session %d: loaded %d selected file(s) from DB for filtered scan",
                session_id, len(selected_set),
            )

    cutoff_datetime = None
    if only_new_mode and source_root.startswith("ios://"):
        serial, _ = parse_ios_source(source_root)
        cutoff_datetime = await get_cutoff_datetime(serial)
        if cutoff_datetime is not None:
            logger.info(
                "Session %d: incremental mode active, cutoff=%s",
                session_id, cutoff_datetime.isoformat(),
            )
        else:
            logger.info(
                "Session %d: incremental mode active but no prior cutoff — full scan",
                session_id,
            )

    # Pass allowed_paths to scanner so it only processes selected files
    item_ids = await run_scan(
        source_root,
        session_id=session_id,
        cutoff_datetime=cutoff_datetime,
        allowed_paths=frozenset(selected_set) if selected_set else None,
    )

    if selected_set is not None:
        logger.info(
            "Session %d: selective scan completed, %d items queued (was %d selected paths)",
            session_id, len(item_ids), len(selected_set),
        )

    if not item_ids:
        if selected_set is not None:
            error_msg = (
                f"None of the {len(selected_set)} selected file(s) matched scanned media "
                f"at '{source_root}'. This may be a path normalisation issue — "
                "please try again by re-selecting files from the preview panel."
            )
        else:
            error_msg = (
                f"No media files found in source directory: {source_root}. "
                "The preflight validator counts all files, but the scanner "
                "only processes media files (images, video, audio, documents)."
            )
        logger.warning("Session %d: %s", session_id, error_msg)
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.status = SessionStatus.FAILED.value
                ts.error_message = error_msg
                ts.completed_at = datetime.now(UTC)
                ts.touch()
        await ws_events.emit_error(session_id, error_msg)
        try:
            report_path = await generate_session_report(session_id)
            async with session_scope() as session:
                ts = await session.get(TransferSession, session_id)
                if ts:
                    ts.session_report_path = str(report_path)
                    ts.touch()
        except Exception as report_exc:
            logger.error("Failed to generate report for session %d: %s", session_id, report_exc)
        return None

    batch_ids = await create_batches(session_id, item_ids)

    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts:
            ts.total_items = len(item_ids)
            ts.total_files = len(item_ids)
            ts.total_batches = len(batch_ids)
            ts.touch()

    await ws_events.emit_scan_complete(session_id, len(item_ids))
    async with session_scope() as session:
        result = await session.execute(
            select(TransferBatch).where(TransferBatch.id.in_(batch_ids))
        )
        batches = list(result.scalars().all())

    if len(batches) < len(batch_ids):
        logger.warning(
            "Only retrieved %d of %d batches from database for session %d",
            len(batches), len(batch_ids), session_id
        )

    for batch in batches:
        await ws_events.emit_batch_created(
            session_id, batch.id, batch.batch_number, batch.total_items
        )

    async with session_scope() as session:
        result = await session.execute(
            select(TransferBatch)
            .where(
                TransferBatch.session_id == session_id,
                TransferBatch.status.notin_([
                    BatchStatus.COMPLETED.value,
                    BatchStatus.FAILED.value,
                    BatchStatus.PARTIAL.value,
                ]),
            )
            .order_by(TransferBatch.batch_number)
        )
        return list(result.scalars().all())


async def _phase_execute_batches(
    session_id: int,
    batches: list[TransferBatch],
    cancel_event: asyncio.Event | None,
) -> bool:
    """Execute all batches through Hop 1 then Hop 2.

    Returns True if all batches completed without unresolved duplicates,
    False if duplicates paused the session.
    """
    duplicate_pause_requested = False

    for batch in batches:
        await set_session_field(session_id, "current_batch", batch.batch_number)
        if batch.status in (
            BatchStatus.COMPLETED.value,
            BatchStatus.FAILED.value,
        ):
            continue

        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts is None or ts.status != SessionStatus.RUNNING.value:
                return False

        try:
            report = await check_batch(batch.id)
        except ValueError:
            logger.warning(
                "Batch %d not found (session may have been cleared) — aborting session %d",
                batch.id, session_id,
            )
            return False

        if report.has_duplicates:
            async with session_scope() as _s:
                _ts_check = await _s.get(TransferSession, session_id)
                _db_resolved = (
                    _ts_check is not None
                    and _ts_check.resolved_batch_id == batch.id
                    and _ts_check.duplicate_resolutions_json is not None
                )
                _db_resolutions = []
                if _ts_check is not None and _ts_check.resolved_batch_id == batch.id:
                    if isinstance(_ts_check.duplicate_resolutions_json, str):
                        _db_resolutions = json.loads(_ts_check.duplicate_resolutions_json)
            if _db_resolved:
                if _db_resolutions:
                    await _apply_duplicate_resolutions(batch.id, _db_resolutions)
                async with session_scope() as _s:
                    _ts_clear = await _s.get(TransferSession, session_id)
                    if _ts_clear:
                        _ts_clear.resolved_batch_id = None
                        _ts_clear.duplicate_resolutions_json = None
                        _ts_clear.touch()
            else:
                await ws_events.emit_duplicates_detected(session_id, {
                    "batch_id": batch.id,
                    "exact_count": len(report.exact_duplicates),
                    "potential_count": len(report.potential_duplicates),
                    "summary": report.summary,
                    "exact_duplicates": [
                        {
                            "item_id": e.item_id,
                            "file_name": e.file_name,
                            "source_path": e.source_path,
                            "source_hash": e.source_hash,
                            "file_size": e.file_size,
                            "match_type": e.match_type,
                            "matched_path": e.matched_path,
                            "matched_item_id": e.matched_item_id,
                            "matched_file_size": e.matched_file_size,
                            "matched_date_taken": e.matched_date_taken,
                            "matched_thumbnail_url": e.matched_thumbnail_url,
                        }
                        for e in report.exact_duplicates
                    ],
                    "potential_duplicates": [
                        {
                            "item_id": e.item_id,
                            "file_name": e.file_name,
                            "source_path": e.source_path,
                            "source_hash": e.source_hash,
                            "file_size": e.file_size,
                            "match_type": e.match_type,
                            "matched_path": e.matched_path,
                            "matched_item_id": e.matched_item_id,
                            "matched_file_size": e.matched_file_size,
                            "matched_date_taken": e.matched_date_taken,
                            "matched_thumbnail_url": e.matched_thumbnail_url,
                        }
                        for e in report.potential_duplicates
                    ],
                    "paused_at": report.checked_at.isoformat(),
                })
                duplicate_pause_requested = True
                break

        # --- Hop 1: Source -> Cache ---
        await ws_events.emit_batch_processing(session_id, batch.id, batch.batch_number, batch.total_items)

        async def _hop1_progress_cb(processed: int, total: int, file_name: str, item_id: int) -> None:
            await ws_events.emit_hop1_progress(session_id, batch.id, processed, total, file_name, item_id=item_id)

        cached = await cache_batch(batch.id, cache_dir=CACHE_DIR, on_file_progress=_hop1_progress_cb, cancel_event=cancel_event, session_id=session_id)
        await ws_events.emit_hop1_complete(session_id, batch.id, cached)

        # --- Hop 2: Cache -> Destination ---
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            dest_root = Path(ts.dest_root) if ts else CACHE_DIR
            move_mode = (ts.transfer_mode == "move") if ts else False

        async def _hop2_progress_cb(processed: int, total: int, file_name: str, item_id: int) -> None:
            await ws_events.emit_hop2_progress(session_id, batch.id, processed, total, file_name, item_id=item_id)

        imported = await import_batch(
            batch.id,
            dest_root=dest_root,
            cache_dir=CACHE_DIR,
            move_mode=move_mode,
            on_file_progress=_hop2_progress_cb,
            cancel_event=cancel_event,
            session_id=session_id,
        )
        await ws_events.emit_hop2_complete(session_id, batch.id, imported)
        await ws_events.emit_batch_complete(session_id, batch.id, batch.batch_number, "completed")

    if duplicate_pause_requested:
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.status = SessionStatus.PAUSED.value
                ts.touch()
        await ws_events.emit_session_paused(session_id)
        return False

    return True


async def _phase_finalize(session_id: int) -> None:
    """Compute final stats, broadcast completion, update device cutoff, generate report."""
    elapsed_seconds = 0
    ts_started_at = None
    ts_completed_at = None
    ts_total_paused_ms = 0
    ts_imported_files = 0
    ts_failed_files = 0
    final_status = SessionStatus.COMPLETED.value
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts:
            # Do not overwrite an explicit CANCELLED status — the user cancelled
            # intentionally. A lingering task should not change CANCELLED to FAILED.
            if ts.status == SessionStatus.CANCELLED.value:
                logger.info(
                    "Session %d is CANCELLED — skipping final status update from background task",
                    session_id,
                )
                _active_tasks.pop(session_id, None)
                return

            result = await session.execute(
                select(MediaItem).where(MediaItem.session_id == session_id)
            )
            items = list(result.scalars().all())

            completed_count = sum(
                1 for item in items if item.final_status == HopStatus.COMPLETED.value
            )
            failed_count = len(items) - completed_count

            ts.completed_items = completed_count
            ts.failed_items = failed_count

            volume_result = await session.execute(
                select(func.sum(MediaItem.file_size)).where(
                    MediaItem.session_id == session_id,
                    MediaItem.final_status == HopStatus.COMPLETED.value,
                )
            )
            ts.total_bytes_volume = volume_result.scalar() or 0

            if ts.total_items > 0:
                if completed_count == 0:
                    ts.status = SessionStatus.FAILED.value
                    ts.error_message = (
                        f"Session completed with 0 successful items out of {ts.total_items} attempted. "
                        "All items failed. Check the manifest or error log for details."
                    )
                elif completed_count < ts.total_items:
                    ts.status = SessionStatus.COMPLETED_WITH_ERRORS.value
                else:
                    ts.status = SessionStatus.COMPLETED.value
            else:
                ts.status = SessionStatus.FAILED.value
                ts.error_message = "Session completed with 0 items — no media files were processed"

            ts.completed_at = datetime.now(UTC)
            ts.touch()
            final_status = ts.status
            ts_started_at = ts.started_at
            ts_completed_at = ts.completed_at
            ts_total_paused_ms = ts.total_paused_ms or 0
            ts_imported_files = ts.imported_files or 0
            ts_failed_files = ts.failed_files or 0

    if ts_started_at and ts_completed_at:
        started = ts_started_at.replace(tzinfo=UTC) if ts_started_at.tzinfo is None else ts_started_at
        completed = ts_completed_at.replace(tzinfo=UTC) if ts_completed_at.tzinfo is None else ts_completed_at
        raw_ms = (completed - started).total_seconds() * 1000
        elapsed_ms = max(0, raw_ms - ts_total_paused_ms)
        elapsed_seconds = int(elapsed_ms / 1000)

    if final_status == SessionStatus.COMPLETED.value:
        await ws_manager.broadcast(session_id, "session_completed", {
            "session_id": session_id,
            "imported_files": ts_imported_files,
            "failed_files": ts_failed_files,
            "elapsed_seconds": elapsed_seconds,
        })
    elif final_status == SessionStatus.COMPLETED_WITH_ERRORS.value:
        await ws_manager.broadcast(session_id, "session_completed_with_errors", {
            "session_id": session_id,
            "imported_files": ts_imported_files,
            "failed_files": ts_failed_files,
            "elapsed_seconds": elapsed_seconds,
        })

    try:
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if (
                ts is not None
                and ts.only_new_mode
                and ts.source_root.startswith("ios://")
                and ts.status in {
                    SessionStatus.COMPLETED.value,
                    SessionStatus.COMPLETED_WITH_ERRORS.value,
                }
            ):
                serial, _ = parse_ios_source(ts.source_root)
                new_cutoff = await compute_cutoff_from_session(session_id)
                if new_cutoff is not None:
                    device_name = None
                    try:
                        raw_devices = await asyncio.to_thread(_list_ios_devices_backend)
                        for d in raw_devices:
                            if d.serial == serial:
                                device_name = d.name
                                break
                    except Exception:
                        pass
                    await upsert_device_state(
                        serial, device_name, new_cutoff, session_id,
                    )
                    logger.info(
                        "Session %d: device import cutoff updated to %s for %s",
                        session_id, new_cutoff.isoformat(), serial,
                    )
    except Exception as cutoff_exc:
        logger.error(
            "Failed to update device import cutoff for session %d: %s",
            session_id, cutoff_exc,
        )

    try:
        report_path = await generate_session_report(session_id)
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.session_report_path = str(report_path)
                ts.touch()
        logger.info("Session %d report saved at %s", session_id, report_path)
    except Exception as report_exc:
        logger.error("Failed to generate report for session %d: %s", session_id, report_exc)


async def _run_transfer_background(session_id: int) -> None:
    """Background task: process all batches through Hop 1 then Hop 2."""
    # Register this task so it can be cancelled if the session is paused/cancelled
    # by a concurrent request before we exit.
    _active_tasks[session_id] = asyncio.current_task()
    cancel_event = _cancellation_events.get(session_id)
    try:
        batches = await _phase_scan_and_create_batches(session_id, cancel_event)
        if batches is None:
            _active_tasks.pop(session_id, None)
            _cleanup_session_state(session_id)
            return

        completed = await _phase_execute_batches(session_id, batches, cancel_event)
        if not completed:
            _active_tasks.pop(session_id, None)
            _cleanup_session_state(session_id)
            return

        await _phase_finalize(session_id)
        _active_tasks.pop(session_id, None)
        _cleanup_session_state(session_id)

    except Exception as exc:
        _active_tasks.pop(session_id, None)
        _cleanup_session_state(session_id)
        logger.error("Transfer background failed for session %d: %s", session_id, exc)
        await ws_events.emit_error(session_id, str(exc))
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.status = SessionStatus.FAILED.value
                ts.error_message = str(exc)
                ts.touch()

        try:
            report_path = await generate_session_report(session_id)
            async with session_scope() as session:
                ts = await session.get(TransferSession, session_id)
                if ts:
                    ts.session_report_path = str(report_path)
                    ts.touch()
            logger.info("Session %d failure report saved at %s", session_id, report_path)
        except Exception as report_exc:
            logger.error("Failed to generate failure report for session %d: %s", session_id, report_exc)


# ---------------------------------------------------------------------------
# Duplicate Handling
# ---------------------------------------------------------------------------
@router.post("/duplicates/check", response_model=DuplicateReportResponse)
async def check_duplicates(req: DuplicateCheckRequest) -> DuplicateReportResponse:
    try:
        report = await check_batch(req.batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return DuplicateReportResponse(
        batch_id=report.batch_id,
        session_id=report.session_id,
        checked_at=report.checked_at,
        exact_duplicates=[
            DuplicateEntrySchema(
                item_id=e.item_id,
                file_name=e.file_name,
                source_path=e.source_path,
                source_hash=e.source_hash,
                file_size=e.file_size,
                match_type=e.match_type,
                matched_path=e.matched_path,
                matched_item_id=e.matched_item_id,
                matched_file_size=e.matched_file_size,
                matched_date_taken=e.matched_date_taken,
                matched_thumbnail_url=e.matched_thumbnail_url,
            )
            for e in report.exact_duplicates
        ],
        potential_duplicates=[
            DuplicateEntrySchema(
                item_id=e.item_id,
                file_name=e.file_name,
                source_path=e.source_path,
                source_hash=e.source_hash,
                file_size=e.file_size,
                match_type=e.match_type,
                matched_path=e.matched_path,
                matched_item_id=e.matched_item_id,
                matched_file_size=e.matched_file_size,
                matched_date_taken=e.matched_date_taken,
                matched_thumbnail_url=e.matched_thumbnail_url,
            )
            for e in report.potential_duplicates
        ],
        total_items_checked=report.total_items_checked,
        processing_paused=report.processing_paused,
        summary=report.summary,
    )


@router.post("/duplicates/prescan", response_model=PrescanResponse)
async def prescan_duplicates(req: PrescanRequest) -> PrescanResponse:
    """Fast hash-free pre-scan: compare candidates against library by (filename, size)."""
    result = await prescan_against_library(
        [c.model_dump() for c in req.candidates]
    )
    return PrescanResponse(**result)


@router.post("/sessions/{session_id}/duplicates/resolve", response_model=SessionActionResponse)
async def resolve_duplicates(
    session_id: int,
    request: DuplicateResolveRequest,
    background_tasks: BackgroundTasks,
) -> SessionActionResponse:
    """Receive duplicate resolution decisions and resume the session."""
    lock = _get_session_lock(session_id)
    async with lock:
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if ts.status != SessionStatus.PAUSED.value:
                raise HTTPException(status_code=400, detail=f"Session is not paused (status: {ts.status})")

            # Accrue pause time
            if ts.paused_at:
                paused = ts.paused_at.replace(tzinfo=UTC) if ts.paused_at.tzinfo is None else ts.paused_at
                ms = (datetime.now(UTC) - paused).total_seconds() * 1000
                ts.total_paused_ms += int(ms)
                ts.paused_at = None

            ts.status = SessionStatus.RUNNING.value
            ts.resolved_batch_id = request.batch_id
            ts.duplicate_resolutions_json = json.dumps([r.model_dump() for r in request.resolutions])
            ts.touch()

        # Re-trigger background processing
        background_tasks.add_task(_run_transfer_background, session_id)
        await ws_events.emit_session_started(session_id)
        await ws_events.emit_duplicates_resolved(session_id, request.batch_id)

        return SessionActionResponse(
            session_id=session_id,
            status="running",
            message="Duplicates resolved, transfer resumed",
        )


# ---------------------------------------------------------------------------
# Media Library
# ---------------------------------------------------------------------------
@router.get("/media", response_model=MediaList)
async def list_media(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    session_id: int | None = Query(None),
    hop1_status: str | None = Query(None),
    hop2_status: str | None = Query(None),
    final_status: str | None = Query(None),
    extension: str | None = Query(None),
    search: str | None = Query(None),
) -> MediaList:
    async with session_scope() as session:
        # Build base query
        q = select(MediaItem)
        count_q = select(func.count(MediaItem.id))

        # Apply filters
        filters = []
        if session_id is not None:
            filters.append(MediaItem.session_id == session_id)
        if hop1_status is not None:
            filters.append(MediaItem.hop1_status == hop1_status)
        if hop2_status is not None:
            filters.append(MediaItem.hop2_status == hop2_status)
        if final_status is not None:
            filters.append(MediaItem.final_status == final_status)
        if extension is not None:
            filters.append(MediaItem.extension == extension.lower())
        if search:
            filters.append(MediaItem.file_name.ilike(f"%{search}%"))

        for f in filters:
            q = q.where(f)
            count_q = count_q.where(f)

        total = (await session.execute(count_q)).scalar() or 0
        pages = math.ceil(total / page_size) if total > 0 else 1

        offset = (page - 1) * page_size
        order_col = func.coalesce(MediaItem.original_capture_time, MediaItem.created_at)
        result = await session.execute(
            q.order_by(order_col.desc()).offset(offset).limit(page_size)
        )
        items = [_media_to_info(mi) for mi in result.scalars().all()]

    return MediaList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


# ---------------------------------------------------------------------------
# Thumbnail serving
# ---------------------------------------------------------------------------
_PLACEHOLDER_JPEG: bytes | None = None


def _get_placeholder_jpeg() -> bytes:
    """Return a small grey placeholder JPEG for failed thumbnails.

    Generated once with Pillow and cached at module level for reuse.
    """
    global _PLACEHOLDER_JPEG
    if _PLACEHOLDER_JPEG is not None:
        return _PLACEHOLDER_JPEG

    try:
        import io

        from PIL import Image, ImageDraw

        size = 120
        img = Image.new("RGB", (size, size), (229, 231, 235))
        draw = ImageDraw.Draw(img)
        m = 24
        inner = (m, m, size - m, size - m)
        draw.rectangle(inner, outline=(156, 163, 175), width=2)
        draw.line((m + 4, m + 4, size - m - 4, size - m - 4), fill=(156, 163, 175), width=2)
        draw.line((size - m - 4, m + 4, m + 4, size - m - 4), fill=(156, 163, 175), width=2)

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        _PLACEHOLDER_JPEG = buf.getvalue()
    except Exception:
        _PLACEHOLDER_JPEG = b""

    return _PLACEHOLDER_JPEG


@router.get("/media/{item_id}/thumbnail")
async def get_media_thumbnail(item_id: int, request: Request):
    """Serve a thumbnail image from the in-memory LRU cache.

    Returns a placeholder JPEG for items whose thumbnail generation failed
    or whose source file is missing, eliminating 404 responses that
    trigger endless frontend retry loops.

    If the thumbnail has not been generated yet (status == "pending"),
    generates it via ``asyncio.to_thread`` so the CPU-bound decoding does
    not block the event loop, then caches the result before responding.

    Response headers include ``X-Thumbnail-Status`` (``"ready"``,
    ``"failed"``, or ``"not_found"``) so the frontend can distinguish
    real thumbnails from placeholders if needed.  The ``"not_found"``
    status is intentionally not persisted so thumbnails can be regenerated
    when external drives are reconnected.
    """
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)

    if db_item is None:
        raise HTTPException(status_code=404, detail="Media item not found")

    # Generate versioned ETag based on item ID and updated_at timestamp
    etag = f'"{item_id}-{int(db_item.updated_at.timestamp())}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    # Determine Cache-Control based on version parameter 't'
    has_version = "t" in request.query_params
    if has_version:
        cache_control = "public, max-age=31536000, immutable"
    else:
        cache_control = "no-cache, must-revalidate"

    if db_item.thumbnail_status == "failed":
        return Response(
            content=_get_placeholder_jpeg(),
            media_type="image/jpeg",
            headers={
                "Cache-Control": cache_control,
                "ETag": etag,
                "X-Thumbnail-Status": "failed",
            },
        )

    data = thumbnail_cache.get(item_id)
    if data is not None:
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": cache_control,
                "ETag": etag,
                "X-Thumbnail-Status": "ready",
            },
        )

    # ---- On cache miss: try to locate the file ----
    # Priority 1: source_path (works for copy-mode local transfers where source still exists)
    file_path: Path | None = None
    if not db_item.source_path.startswith("ios://"):
        try:
            source = Path(db_item.source_path)
            if source.is_file():
                file_path = source
        except Exception:
            pass

    # Priority 1.5: check Hop 1 local cache directory
    if file_path is None:
        from backend.engines.cache_manager import get_cache_path
        try:
            candidate = get_cache_path(CACHE_DIR, db_item.source_path, db_item.file_name)
            if candidate.is_file():
                file_path = candidate
        except Exception:
            pass

    # Priority 2: reconstruct from archive destination
    if file_path is None and db_item.session_id is not None:
        async with session_scope() as session:
            session_obj = await session.get(TransferSession, db_item.session_id)
        if session_obj and session_obj.dest_root:
            from backend.engines.organizer import locate_archive_file

            layout = getattr(session_obj, "folder_layout", "year/month")
            try:
                candidate = locate_archive_file(Path(session_obj.dest_root), db_item, layout=layout)
                if candidate is not None:
                    file_path = candidate
            except Exception:
                pass

    if file_path is not None:
        data = await asyncio.to_thread(generate_thumbnail_bytes, file_path)
        if data:
            thumbnail_cache.put(item_id, data)
            async with session_scope() as session:
                upd = await session.get(MediaItem, item_id)
                if upd:
                    upd.thumbnail_path = "memory"
                    upd.thumbnail_status = "ready"
                    upd.touch()
            return Response(
                content=data,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": cache_control,
                    "ETag": etag,
                    "X-Thumbnail-Status": "ready",
                },
            )

    # File not found anywhere — return placeholder but do NOT permanently mark as failed
    # (the file may be on a disconnected external drive; marking failed prevents future regen)
    return Response(
        content=_get_placeholder_jpeg(),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Thumbnail-Status": "not_found",
        },
    )


@router.get("/media/{item_id}/thumbnail/status")
async def get_thumbnail_status(item_id: int):
    """Return the thumbnail generation status for a media item.

    The frontend can poll this before showing the thumbnail <img> element
    so it knows whether to expect a real image or fallback placeholder.
    """
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Media item not found")
    if thumbnail_cache.has(item_id):
        return {"status": "ready"}
    return {"status": db_item.thumbnail_status}


@router.get("/media/thumbnail-cache-stats")
async def thumbnail_cache_stats():
    """Return LRU cache statistics for debugging."""
    return thumbnail_cache.stats()


async def _remove_orphaned_media_item(item_id: int) -> None:
    """Delete a MediaItem when both source and dest are gone.

    This is called during thumbnail regeneration when the item's original
    source file AND its transferred destination file no longer exist on
    disk.  The item represents a file that cannot be recovered — remove
    the database record and evict any cached thumbnail.
    """
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
        if db_item is None:
            return
        thumbnail_cache.evict_items([item_id])
        await session.delete(db_item)
        await session.commit()
        logger.info(
            "Removed orphaned MediaItem %d (source and dest both missing)", item_id,
        )


@router.post("/media/regenerate-thumbnails")
async def regenerate_thumbnails():
    """Kick off background thumbnail generation for all completed items
    that are missing a ``thumbnail_path``.

    Returns immediately with counts of eligible, succeeded, and failed items
    (including the IDs of failed items) so the frontend can update its state.
    The actual generation runs in a background thread so the HTTP response is fast.
    """
    # Bump the generation counter — any prior regen thread will
    # see a stale generation and stop.
    global _regen_generation
    with _regen_gen_lock:
        _regen_generation += 1
        current_gen = _regen_generation

    async with session_scope() as session:
        q = (
            select(MediaItem)
            .options(joinedload(MediaItem.session))
            .where(MediaItem.thumbnail_path.is_(None))
            .where(MediaItem.thumbnail_status != "failed")
            .where(MediaItem.final_status == HopStatus.COMPLETED.value)
        )
        result = await session.execute(q)
        items = list(result.unique().scalars().all())

        # Extract all data needed by the background thread while the session is still open.
        item_data = [
            {
                "id": item.id,
                "source_path": item.source_path,
                "dest_root": Path(item.session.dest_root) if item.session else None,
                "file_name": item.file_name,
                "file_size": item.file_size,
                "extension": item.extension,
                "date_taken": item.date_taken,
                "original_capture_time": item.original_capture_time,
                "created_at": item.created_at,
                "folder_layout": (item.session.folder_layout if item.session else None) or "year/month",
            }
            for item in items
        ]

    if not items:
        return {"message": "No items need thumbnail generation", "count": 0, "stale_count": 0}

    def _generate_all(gen: int) -> None:
            from backend.engines.thread_runner import submit_and_wait

            for entry in item_data:
                with _regen_gen_lock:
                    if gen != _regen_generation:
                        logger.info("Thumbnail regen cancelled by a newer request")
                        break

                item_id = entry["id"]
                source_path = entry["source_path"]
                dest_root = entry["dest_root"]
                try:
                    file_path = resolve_thumbnail_source_path(entry, dest_root)

                    if file_path is None:
                        logger.warning(
                            "Thumbnail regen: source and dest both missing for item %d "
                            "-- skipping", item_id
                        )
                        submit_and_wait(mark_thumbnail_failed(item_id))
                        continue

                    data = generate_thumbnail_bytes(file_path)
                    if data:
                        with _regen_gen_lock:
                            if gen != _regen_generation:
                                break
                        thumbnail_cache.put(item_id, data)
                        time.sleep(0.02)
                        submit_and_wait(mark_thumbnail_ready(item_id))
                        logger.info("Thumbnail regen: item %d OK (%d bytes)", item_id, len(data))
                    else:
                        logger.warning("Thumbnail regen: generation returned None for item %d", item_id)
                        submit_and_wait(mark_thumbnail_failed(item_id))
                except Exception as exc:
                    logger.warning("Thumbnail regen: failed for item %d: %s", item_id, exc)
                    try:
                        submit_and_wait(mark_thumbnail_failed(item_id))
                    except Exception:
                        pass

    t = threading.Thread(
        target=_generate_all, args=(current_gen,), daemon=True, name="regen-thumbnails",
    )
    t.start()

    return {
        "message": "Thumbnail generation started in background",
        "total": len(items),
        "queued": len(items),
        "note": "Call GET /api/media/thumbnail-cache-stats to monitor progress.",
    }


# ---------------------------------------------------------------------------
# Clear Library
# ---------------------------------------------------------------------------
@router.post("/media/clear", response_model=ClearResponse)
async def clear_library(_: None = Depends(require_local_token)) -> ClearResponse:
    """Clear all library entries (media items, sessions, batches).

    Removes every row from media_items, transfer_batches, and
    transfer_sessions tables. Clears the in-memory thumbnail cache and
    Hop 1 cache directory contents. Does NOT touch the user's actual
    files at any transfer destination — this only clears app-managed
    records and caches.
    """
    global _regen_generation
    with _regen_gen_lock:
        _regen_generation += 1
    thumbnail_cache.clear()

    async with session_scope() as session:
        media_count = (await session.execute(
            select(func.count(MediaItem.id))
        )).scalar() or 0

        # Collect all session IDs before deleting them
        session_ids_res = await session.execute(select(TransferSession.id))
        session_ids = [row[0] for row in session_ids_res.all()]
        batch_count = (await session.execute(
            select(func.count(TransferBatch.id))
        )).scalar() or 0

        # Delete in correct cascade order
        await session.execute(delete(MediaItem))
        await session.execute(delete(TransferBatch))
        await session.execute(delete(TransferSession))
        try:
            await session.execute(text("DELETE FROM sqlite_sequence WHERE name='media_items'"))
            await session.execute(text("DELETE FROM sqlite_sequence WHERE name='transfer_batches'"))
            await session.execute(text("DELETE FROM sqlite_sequence WHERE name='transfer_sessions'"))
        except Exception as exc:
            logger.debug("Failed to reset sqlite_sequence: %s", exc)
        await session.commit()

    # Clean up in-memory state for deleted sessions
    for sid in session_ids:
        _cleanup_session_state(sid)

    cache_files_removed = _clear_cache_dir()

    logger.info(
        "Cleared library: %d media items, %d sessions, %d cache files removed",
        media_count, len(session_ids), cache_files_removed,
    )

    return ClearResponse(
        message=f"Cleared {media_count} library item(s) and {len(session_ids)} session(s)",
        sessions_cleared=len(session_ids),
        batches_cleared=batch_count,
        media_items_cleared=media_count,
        thumbnails_removed=0,
        cache_files_removed=cache_files_removed,
    )


# ---------------------------------------------------------------------------
# Batch queries
# ---------------------------------------------------------------------------
@router.get("/sessions/{session_id}/batches", response_model=BatchList)
async def list_batches(session_id: int) -> BatchList:
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")

        result = await session.execute(
            select(TransferBatch)
            .where(TransferBatch.session_id == session_id)
            .order_by(TransferBatch.batch_number)
        )
        batches = [_batch_to_info(b) for b in result.scalars().all()]

    return BatchList(batches=batches, total=len(batches))


# ---------------------------------------------------------------------------
# Session Progress (polling-based authoritative source)
# ---------------------------------------------------------------------------
@router.get("/sessions/{session_id}/progress", response_model=SessionProgressResponse)
async def get_session_progress(
    session_id: int,
    _: None = Depends(per_session_rate_limit()),
) -> SessionProgressResponse:
    """Return a complete snapshot of transfer progress for the live UI.

    This is the authoritative data source for the Transfer Monitor and Media
    Preview. The WebSocket channel (if connected) provides faster granular
    updates on top of this baseline, but polling this endpoint is the
    **only** requirement for correctness.
    """
    try:
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts is None:
                raise HTTPException(status_code=404, detail="Session not found")

            active_batch = None
            current_item_id = None
            current_file_name = ""
            current_hop = ""
            hop1_progress = 0
            hop2_progress = 0
            batch_completed = 0
            batch_total = 0

            # Find the active (in-progress) batch
            result = await session.execute(
                select(TransferBatch)
                .where(
                    TransferBatch.session_id == session_id,
                    TransferBatch.status.in_([
                        BatchStatus.PROCESSING.value,
                        BatchStatus.LOADING.value,
                        BatchStatus.ARCHIVED.value,
                    ]),
                )
                .order_by(TransferBatch.batch_number)
                .limit(1)
            )
            active_batch = result.scalars().first()

            if active_batch:
                batch_total = active_batch.total_items
                batch_completed = active_batch.completed_items

                # Get items in the active batch for hop progress calculation
                items_result = await session.execute(
                    select(MediaItem)
                    .where(MediaItem.batch_id == active_batch.id)
                    .order_by(MediaItem.id)
                )
                batch_items = list(items_result.scalars().all())

                # Find current item: the most recently updated non-terminal item
                active_items = [
                    item for item in batch_items
                    if item.final_status not in (
                        HopStatus.COMPLETED.value, HopStatus.FAILED.value,
                        HopStatus.SKIPPED.value,
                    )
                ]
                active_items.sort(key=lambda i: i.updated_at, reverse=True)

                if active_items:
                    current = active_items[0]
                    current_item_id = current.id
                    current_file_name = current.file_name
                    if current.hop1_status == HopStatus.COMPLETED.value:
                        current_hop = "hop2"
                    elif current.hop1_status in (
                        HopStatus.TRANSFERRING.value,
                        HopStatus.HASHING.value,
                        HopStatus.SCANNING.value,
                        HopStatus.SCANNED.value,
                        HopStatus.HASHED.value,
                        HopStatus.PENDING.value,
                        HopStatus.FAILED.value,
                    ):
                        current_hop = "hop1"
                    else:
                        current_hop = "hop1"

                # Compute hop progress percentages
                total = len(batch_items) if batch_items else 1
                hop1_done = sum(
                    1 for item in batch_items
                    if item.hop1_status in (
                        HopStatus.COMPLETED.value, HopStatus.SKIPPED.value,
                    )
                )
                hop2_done = sum(
                    1 for item in batch_items
                    if item.hop2_status in (
                        HopStatus.COMPLETED.value, HopStatus.SKIPPED.value,
                    )
                )
                hop1_progress = round((hop1_done / total) * 100)
                hop2_progress = round((hop2_done / total) * 100)

            # Recent items with thumbnails (for the Media Preview panel).
            # Use a generous limit (200) so that when items arrive progressively
            # the frontend sees them arrive and can accumulate them, rather than
            # having earlier items silently pushed out of a tight 12-item window.
            recent_result = await session.execute(
                select(MediaItem)
                .where(
                    MediaItem.session_id == session_id,
                    MediaItem.thumbnail_path.isnot(None),
                )
                .order_by(MediaItem.updated_at.desc())
                .limit(200)
            )
            recent_items = [
                RecentItemProgress(
                    item_id=item.id,
                    file_name=item.file_name,
                    hop1_status=item.hop1_status,
                    hop2_status=item.hop2_status,
                    thumbnail_url=f"/api/media/{item.id}/thumbnail?t={int(item.updated_at.timestamp())}" if item.thumbnail_path else None,
                    updated_at=item.updated_at,
                )
                for item in recent_result.scalars().all()
            ]

        total_files = ts.total_files or ts.total_items
        progress_pct = round((ts.imported_files / total_files) * 100, 1) if total_files > 0 else 0.0

        # --- Server-side elapsed / ETA / speed computation ---
        now = datetime.now(UTC)
        elapsed_seconds = 0
        eta_seconds: int | None = None
        speed = 0.0

        if ts.started_at:
            active_pause_ms = 0
            if ts.paused_at:
                paused = ts.paused_at.replace(tzinfo=UTC) if ts.paused_at.tzinfo is None else ts.paused_at
                active_pause_ms = (now - paused).total_seconds() * 1000
            started = ts.started_at.replace(tzinfo=UTC) if ts.started_at.tzinfo is None else ts.started_at
            elapsed_ms = max(
                0,
                (now - started).total_seconds() * 1000
                - ts.total_paused_ms
                - active_pause_ms,
            )
            elapsed_seconds = int(elapsed_ms / 1000)

            # Compute rolling speed from speed_samples
            # Blend a short window (last 2 samples, most reactive) with a
            # medium window (last 5 samples, more stable) for responsiveness
            # without jitter.
            if ts.speed_samples:
                try:
                    all_samples = json.loads(ts.speed_samples)
                    if len(all_samples) >= 2:
                        short = all_samples[-2:]
                        dt_short = short[-1]["ts"] - short[0]["ts"]
                        dc_short = short[-1]["count"] - short[0]["count"]
                        speed_short = dc_short / dt_short if dt_short > 0 else 0.0

                        medium = all_samples[-5:]
                        dt_med = medium[-1]["ts"] - medium[0]["ts"]
                        dc_med = medium[-1]["count"] - medium[0]["count"]
                        speed_med = dc_med / dt_med if dt_med > 0 else 0.0

                        speed = (
                            0.4 * speed_short + 0.6 * speed_med
                            if speed_short > 0 and speed_med > 0
                            else speed_short or speed_med
                        )
                    elif len(all_samples) == 1 and elapsed_seconds > 0 and ts.imported_files > 0:
                        speed = ts.imported_files / elapsed_seconds
                except (json.JSONDecodeError, KeyError, IndexError):
                    speed = 0.0

            # Fallback: if still zero but we have files and elapsed time, use naive average
            if speed == 0.0 and elapsed_seconds > 0 and ts.imported_files > 0:
                speed = ts.imported_files / elapsed_seconds

            remaining = total_files - ts.imported_files
            if speed > 0.01:
                eta_seconds = int(remaining / speed)

        return SessionProgressResponse(
            session_id=session_id,
            status=ts.status,
            total_items=ts.total_items,
            completed_items=ts.completed_items,
            failed_items=ts.failed_items,
            total_files=total_files,
            cached_files=ts.cached_files,
            imported_files=ts.imported_files,
            failed_files=ts.failed_files,
            current_batch=ts.current_batch,
            total_batches=ts.total_batches,
            progress_percent=progress_pct,
            current_item_id=current_item_id,
            current_file_name=current_file_name,
            current_hop=current_hop,
            active_batch_id=active_batch.id if active_batch else None,
            active_batch_number=active_batch.batch_number if active_batch else 0,
            active_batch_status=active_batch.status if active_batch else "",
            active_batch_total=batch_total,
            active_batch_completed=batch_completed,
            active_batch_hop1_progress=hop1_progress,
            active_batch_hop2_progress=hop2_progress,
            recent_items=recent_items,
            started_at=ts.started_at,
            completed_at=ts.completed_at,
            elapsed_seconds=elapsed_seconds,
            eta_seconds=eta_seconds,
            speed_files_per_sec=round(speed, 2),
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.getLogger(__name__).error(f"get_session_progress error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Crash Recovery
# ---------------------------------------------------------------------------
@router.post("/recovery", response_model=SessionActionResponse)
async def trigger_recovery(_: None = Depends(require_local_token)) -> SessionActionResponse:
    stats = await recover_interrupted_batches(cache_dir=CACHE_DIR)
    return SessionActionResponse(
        session_id=0,
        status="recovered",
        message=f"Recovered {stats['loading_recovered']} LOADING, "
                f"{stats['archived_recovered']} ARCHIVED batches",
    )


# ---------------------------------------------------------------------------
# Directory Size Metrics
# ---------------------------------------------------------------------------
def _measure_directory(dir_path: str) -> dict:
    """Synchronous directory traversal — total size, file count, folder count."""
    total_bytes = 0
    file_count = 0
    folder_count = 0
    p = Path(dir_path)
    if not p.exists():
        return {"total_bytes": 0, "file_count": 0, "folder_count": 0, "exists": False}
    for entry in p.rglob("*"):
        if entry.is_file():
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                pass  # skip inaccessible files
            file_count += 1
        elif entry.is_dir():
            folder_count += 1
    return {"total_bytes": total_bytes, "file_count": file_count, "folder_count": folder_count, "exists": True}


def _format_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    return f"{size_bytes / (1024 ** 3):.2f} GB"


@router.post("/utils/dir-size", response_model=DirSizeResponse)
async def get_dir_size(req: DirSizeRequest) -> DirSizeResponse:
    try:
        result = await asyncio.to_thread(_measure_directory, req.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot measure directory: {exc}")

    if not result["exists"]:
        raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")

    return DirSizeResponse(
        path=req.path,
        total_bytes=result["total_bytes"],
        file_count=result["file_count"],
        folder_count=result["folder_count"],
        readable=_format_size(result["total_bytes"]),
    )


@router.post("/utils/disk-space", response_model=DiskSpaceResponse)
async def get_disk_space(req: DiskSpaceRequest) -> DiskSpaceResponse:
    """Return total / used / free bytes for the drive hosting the given path."""
    p = req.path.strip()
    if not p:
        raise HTTPException(status_code=400, detail="Path must not be empty")
    non_local_prefixes = ("file://", "smb://", "\\\\", "ios://", "wpd://", "afc://")
    if any(p.startswith(prefix) for prefix in non_local_prefixes):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot query disk space for non-local path: {p}",
        )
    try:
        usage = shutil.disk_usage(p)
    except NotADirectoryError:
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {p}")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Path not found: {p}")
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {p}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot query disk space: {exc}")
    return DiskSpaceResponse(
        path=req.path,
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
    )


# ---------------------------------------------------------------------------
# Folder Metadata (lightweight size + count for dashboard cards)
# ---------------------------------------------------------------------------
def _measure_folder_metadata(dir_path: str) -> dict:
    """Synchronous directory traversal returning size in GB and file count."""
    total_bytes = 0
    file_count = 0
    p = Path(dir_path)
    if not p.exists():
        return {"size_gb": 0.0, "file_count": 0}
    for entry in p.rglob("*"):
        if entry.is_file():
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                pass
            file_count += 1
    return {"size_gb": round(total_bytes / (1024 ** 3), 2), "file_count": file_count}


@router.post("/utils/folder-metadata", response_model=FolderMetadataResponse)
async def get_folder_metadata(req: FolderMetadataRequest) -> FolderMetadataResponse:
    """Return aggregate file size (GB) and file count for a directory."""
    try:
        result = await asyncio.to_thread(_measure_folder_metadata, req.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot measure folder: {exc}")
    return FolderMetadataResponse(
        path=req.path,
        size_gb=result["size_gb"],
        file_count=result["file_count"],
    )


# ---------------------------------------------------------------------------
# Preflight Disk Capacity Validation
# ---------------------------------------------------------------------------
def _scan_source_volume(source_path: str) -> dict:
    """Walk source directory with os.scandir and return aggregate size + file count."""
    total_bytes = 0
    file_count = 0
    try:
        with os.scandir(source_path) as scanner:
            for entry in scanner:
                if entry.is_file(follow_symlinks=False):
                    try:
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                    file_count += 1
                elif entry.is_dir(follow_symlinks=False):
                    for sub_file in _scan_dir_tree(entry.path):
                        total_bytes += sub_file[0]
                        file_count += sub_file[1]
    except FileNotFoundError:
        return {"source_size_bytes": 0, "file_count": 0, "exists": False}
    except PermissionError:
        return {"source_size_bytes": 0, "file_count": 0, "exists": False}
    return {"source_size_bytes": total_bytes, "file_count": file_count, "exists": True}


def _scan_dir_tree(dir_path: str) -> list[tuple[int, int]]:
    """Recursive scandir walker yielding (byte_size, file_count) per subdirectory."""
    results: list[tuple[int, int]] = []
    try:
        with os.scandir(dir_path) as scanner:
            for entry in scanner:
                if entry.is_file(follow_symlinks=False):
                    try:
                        sz = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        sz = 0
                    results.append((sz, 1))
                elif entry.is_dir(follow_symlinks=False):
                    results.extend(_scan_dir_tree(entry.path))
    except (FileNotFoundError, PermissionError):
        pass
    return results


def _get_dest_free_space(dest_path: str) -> int:
    """Return free bytes on the drive hosting dest_path."""
    usage = shutil.disk_usage(dest_path)
    return usage.free


def _preflight_validate_sync(source_path: str, dest_path: str) -> dict:
    """Synchronous pre-flight: source volume scan + destination free space check."""
    source = _scan_source_volume(source_path)
    dest_free = _get_dest_free_space(dest_path)

    source_size = source["source_size_bytes"]
    is_sufficient = dest_free >= source_size

    return {
        "source_size_bytes": source_size,
        "dest_free_bytes": dest_free,
        "is_sufficient": is_sufficient,
        "file_count": source["file_count"],
    }


@router.post("/utils/validate-path", response_model=PathValidateResponse)
async def validate_path(req: PathValidateRequest) -> PathValidateResponse:
    """Check whether a single path exists, is a directory, and is readable."""
    p = Path(req.path)
    return PathValidateResponse(
        path=req.path,
        exists=p.exists(),
        is_dir=p.is_dir(),
        readable=os.access(p, os.R_OK) if p.exists() else False,
    )


@router.post("/utils/preflight-validate", response_model=PreflightValidateResponse)
async def preflight_validate(req: PreflightValidateRequest) -> PreflightValidateResponse:
    """Pre-flight disk capacity check: compare source volume against destination free space."""
    # Resolve source: prefer source_ref, fall back to source_path string
    source_ref = req.source_ref
    if source_ref is None and req.source_path:
        source_ref = legacy_string_to_source_ref(req.source_path)
    if source_ref is None:
        raise HTTPException(status_code=400, detail="Either source_ref or source_path must be provided")

    # For local sources, use filesystem scanning
    if isinstance(source_ref, SourceRefLocal):
        source_path_str = source_ref.path
        if not Path(source_path_str).exists():
            raise HTTPException(status_code=404, detail=f"Source path not found: {source_path_str}")
        if not Path(req.dest_path).exists():
            raise HTTPException(status_code=404, detail=f"Destination path not found: {req.dest_path}")

        try:
            result = await asyncio.to_thread(_preflight_validate_sync, source_path_str, req.dest_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Preflight validation failed: {exc}")
    else:
        # Device source: estimate size from device path (best-effort)
        reader = DeviceSourceReader(
            device_id=source_ref.device_id,
            device_path=source_ref.device_path,
        )
        try:
            total_bytes = 0
            file_count = 0
            async for entry in reader.walk(source_ref.device_path):
                if not entry["is_dir"]:
                    total_bytes += entry["size"]
                    file_count += 1
        except Exception:
            # Device may have disconnected — return zero with a note
            total_bytes = 0
            file_count = 0

        if not Path(req.dest_path).exists():
            raise HTTPException(status_code=404, detail=f"Destination path not found: {req.dest_path}")
        dest_free = shutil.disk_usage(req.dest_path).free
        result = {
            "source_size_bytes": total_bytes,
            "dest_free_bytes": dest_free,
            "is_sufficient": dest_free >= total_bytes,
            "file_count": file_count,
        }

    # --- Logging warnings ---
    src_human = _format_size(result["source_size_bytes"])
    free_human = _format_size(result["dest_free_bytes"])

    if not result["is_sufficient"]:
        logger.warning(
            "PREFLIGHT BLOCKED: destination free space (%s) is LESS than source volume (%s, %d files) "
            "at dest=%s",
            free_human, src_human, result["file_count"], req.dest_path,
        )
    else:
        margin = result["dest_free_bytes"] - result["source_size_bytes"]
        if result["source_size_bytes"] > 0 and margin < result["source_size_bytes"] * 0.1:
            logger.warning(
                "PREFLIGHT WARNING: destination free space (%s) is dangerously close to source volume "
                "(%s, %d files) — only %s headroom at dest=%s",
                free_human, src_human, result["file_count"], _format_size(margin), req.dest_path,
            )
        else:
            logger.info(
                "PREFLIGHT OK: source=%s (%d files), dest_free=%s at dest=%s",
                src_human, result["file_count"], free_human, req.dest_path,
            )

    return PreflightValidateResponse(
        source_size_bytes=result["source_size_bytes"],
        dest_free_bytes=result["dest_free_bytes"],
        is_sufficient=result["is_sufficient"],
        file_count=result["file_count"],
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
async def ws_transfer(websocket: WebSocket, session_id: int) -> None:
    """Accept a WebSocket connection for live transfer progress.

    Validates the session exists before accepting. Logs connection attempts
    so that 403-style rejections have a clear audit trail.
    """
    logger.info("!!! ws_transfer ENTERED for session %d — request reached handler", session_id)
    # Validate session exists before accepting
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            logger.warning("WS rejected: session %d not found", session_id)
            await websocket.close(code=4004, reason="Session not found")
            return
        logger.info(
            "WS connection request: session=%d status=%s source=%s",
            session_id, ts.status, ts.source_root,
        )

    await ws_manager.connect(websocket, session_id)
    # Send an immediate connected event so the client knows the WS is live
    try:
        await websocket.send_json({
            "event": "connected",
            "data": {"session_id": session_id, "status": ts.status},
            "timestamp": datetime.now(UTC).isoformat(),
        })
    except Exception:
        pass
    try:
        while True:
            data = await websocket.receive_json()
            # Handle pong responses
            if data.get("event") == "pong":
                ws_manager.signal_pong(websocket, session_id)
                continue
            # Handle client-initiated events if needed
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, session_id)
    except Exception:
        ws_manager.disconnect(websocket, session_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _session_to_info(ts: TransferSession) -> SessionInfo:
    return SessionInfo(
        id=ts.id,
        session_name=ts.session_name,
        source_root=ts.source_root,
        dest_root=ts.dest_root,
        transfer_mode=ts.transfer_mode,
        status=ts.status,
        total_items=ts.total_items,
        completed_items=ts.completed_items,
        failed_items=ts.failed_items,
        only_new_mode=ts.only_new_mode,
        folder_layout=ts.folder_layout,
        total_bytes_volume=ts.total_bytes_volume,
        session_report_path=ts.session_report_path,
        created_at=ts.created_at,
        updated_at=ts.updated_at,
        started_at=ts.started_at,
        completed_at=ts.completed_at,
    )


# ---------------------------------------------------------------------------
# Report serving
# ---------------------------------------------------------------------------
@router.get("/sessions/{session_id}/report")
async def get_session_report(session_id: int, fmt: str = "html"):
    """Serve the session report file (HTML or JSON)."""
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")

    if not ts.session_report_path:
        raise HTTPException(status_code=404, detail="No report available for this session")

    report_path = Path(ts.session_report_path)

    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report file not found on disk")

    if fmt == "json":
        json_path = report_path.with_suffix(".json")
        if not json_path.exists():
            raise HTTPException(status_code=404, detail="JSON report not found")
        return FileResponse(
            str(json_path),
            media_type="application/json",
            filename=f"session-{session_id}-report.json",
        )

    # Default: HTML
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="HTML report not found")
    return FileResponse(
        str(report_path),
        media_type="text/html",
        filename=f"session-{session_id}-report.html",
    )


@router.post("/admin/backfill-metadata")
async def backfill_metadata(_: None = Depends(require_local_token)):
    """
    One-time migration: backfill ``original_capture_time`` for all existing
    records where it is NULL.

    Reads metadata from files already on the destination HDD (or falls back
    to the Hop 1 cache) to determine the original capture date.

    Runs in a background thread; returns immediately with counts.
    """
    async with session_scope() as session:
        q = select(MediaItem).where(MediaItem.original_capture_time.is_(None))
        result = await session.execute(q)
        items = list(result.scalars().all())

        item_data = []
        for item in items:
            dest_root = None
            if item.session is not None:
                dest_root = item.session.dest_root
            item_data.append({
                "id": item.id,
                "dest_root": Path(dest_root) if dest_root else None,
                "file_name": item.file_name,
                "source_path": item.source_path,
            })

    if not items:
        return {"message": "No items need backfill", "total": 0}

    updated = 0
    failed = 0
    failed_ids: list[int] = []

    def _backfill_all() -> None:
        nonlocal updated, failed
        loop = asyncio.new_event_loop()
        try:
            for entry in item_data:
                item_id = entry["id"]
                dest_root = entry["dest_root"]
                source_path = entry["source_path"]
                # Try destination first, then Hop 1 cache, then source
                candidates = []
                if dest_root:
                    candidates.append(dest_root / entry["file_name"])
                candidates.append(Path(source_path))

                file_path = None
                for c in candidates:
                    if c.is_file():
                        file_path = c
                        break

                if file_path is None:
                    logger.warning("Backfill: no file found for item %d", item_id)
                    failed_ids.append(item_id)
                    failed += 1
                    continue

                try:
                    capture_dt = extract_capture_datetime(file_path)

                    async def _update(iid: int, dt: datetime) -> None:
                        async with session_scope() as session:
                            db_item = await session.get(MediaItem, iid)
                            if db_item is not None:
                                db_item.original_capture_time = dt
                                db_item.touch()

                    loop.run_until_complete(_update(item_id, capture_dt))
                    updated += 1
                    logger.info("Backfill: item %d <- %s", item_id, capture_dt.isoformat())
                except Exception as exc:
                    logger.warning("Backfill failed for item %d: %s", item_id, exc)
                    failed_ids.append(item_id)
                    failed += 1
        finally:
            loop.close()

    t = threading.Thread(target=_backfill_all, daemon=True, name="backfill-metadata")
    t.start()

    return {
        "message": "Metadata backfill started",
        "total": len(items),
        "updated": updated,
        "failed": failed,
        "failed_ids": failed_ids,
    }


def _media_to_info(mi: MediaItem) -> MediaItemInfo:
    return MediaItemInfo(
        id=mi.id,
        source_path=mi.source_path,
        file_name=mi.file_name,
        file_size=mi.file_size,
        extension=mi.extension,
        mime_type=mi.mime_type,
        hop1_status=mi.hop1_status,
        hop2_status=mi.hop2_status,
        final_status=mi.final_status,
        live_photo_group=mi.live_photo_group,
        thumbnail_url=f"/api/media/{mi.id}/thumbnail?t={int(mi.updated_at.timestamp())}",
        thumbnail_status=mi.thumbnail_status,
        date_taken=mi.date_taken,
        date_source=mi.date_source,
        error_message=mi.error_message,
        created_at=mi.created_at,
        updated_at=mi.updated_at,
    )



def _batch_to_info(b: TransferBatch) -> BatchInfo:
    return BatchInfo(
        id=b.id,
        session_id=b.session_id,
        batch_number=b.batch_number,
        status=b.status,
        total_items=b.total_items,
        completed_items=b.completed_items,
        failed_items=b.failed_items,
        created_at=b.created_at,
        updated_at=b.updated_at,
    )
