"""
Transfera v2 -- Tier 2 API Routes
Endpoints for WSL2/usbipd iPhone access setup and management.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.api.tier2_schemas import (
    Tier2BindExecuteRequest,
    Tier2BindExecuteResponse,
    Tier2BindPreviewResponse,
    Tier2BindRequest,
    Tier2CancelResponse,
    Tier2ResetResponse,
    Tier2ResumeNotification,
    Tier2RestartRequest,
    Tier2SetupPreviewResponse,
    Tier2StepPreview,
    Tier2StepRequest,
    Tier2StepResponse,
    Tier2StatusResponse,
    Tier2USBDeviceInfo,
    Tier2USBDeviceListResponse,
)
from backend.api.auth import require_local_token
from backend.tier2_manager import get_device_manager, DeviceAccessTier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tier2")


@router.get("/status", response_model=Tier2StatusResponse)
async def get_tier2_status() -> Tier2StatusResponse:
    """Get comprehensive Tier 2 setup status."""
    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()

    if orchestrator is None:
        try:
            from backend.wsl_orchestrator import WSLOrchestrator
            orchestrator = WSLOrchestrator()
        except Exception:
            return Tier2StatusResponse(error="WSL orchestrator not available")

    wsl_status = await orchestrator.check_feasibility()
    usbipd_status = await orchestrator.verify_usbipd_installed()
    bridge_status = await orchestrator.get_bridge_status()

    # Get devices on Tier 2
    devices_on_tier2 = []
    if bridge_status.reachable:
        devices_on_tier2 = [d.get("serial", "") for d in bridge_status.devices if d.get("serial")]

    return Tier2StatusResponse(
        wsl_installed=wsl_status.wsl_installed,
        distro_name=wsl_status.distro_name,
        distro_ready=wsl_status.distro_ready,
        usbipd_installed=usbipd_status.installed,
        usbipd_version=usbipd_status.version,
        bridge_running=bridge_status.running,
        bridge_reachable=bridge_status.reachable,
        virtualization_available=wsl_status.virtualization_available,
        restart_required=wsl_status.restart_required,
        active_tier=(await manager.get_active_tier()).value,
        devices_on_tier2=devices_on_tier2,
        error=wsl_status.error or usbipd_status.error or bridge_status.error,
    )


@router.get("/preview", response_model=Tier2SetupPreviewResponse)
async def get_setup_preview() -> Tier2SetupPreviewResponse:
    """
    Preview all setup steps before the user commits.
    Shows exactly what will happen, including restart/elevation requirements.
    """
    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()

    if orchestrator is None:
        try:
            from backend.wsl_orchestrator import WSLOrchestrator
            orchestrator = WSLOrchestrator()
        except Exception:
            raise HTTPException(status_code=503, detail="WSL orchestrator not available")

    wsl_status = await orchestrator.check_feasibility()
    usbipd_status = await orchestrator.verify_usbipd_installed()

    steps: list[Tier2StepPreview] = []
    needs_restart = False
    needs_elevation = False

    if not wsl_status.wsl_installed:
        needs_restart = True
        steps.append(Tier2StepPreview(
            step_id="enable_wsl",
            title="Enable WSL2 (Windows feature)",
            description="Turns on Windows Subsystem for Linux. One-time restart may be required.",
            requires_restart=True,
            restart_description="Windows needs to restart to finish enabling WSL2. Save any unsaved work before restarting.",
        ))

    if wsl_status.wsl_installed and not wsl_status.distro_name:
        steps.append(Tier2StepPreview(
            step_id="install_distro",
            title="Install Ubuntu (WSL distribution)",
            description="Downloads and registers the Ubuntu WSL distribution for running Linux-side tools.",
        ))

    if not usbipd_status.installed:
        needs_elevation = True
        steps.append(Tier2StepPreview(
            step_id="install_usbipd",
            title="Install usbipd-win (USB sharing tool)",
            description="Installs an open-source USB device sharing tool referenced in Microsoft's official WSL documentation.",
            requires_elevation=True,
            elevation_description="Windows will ask for admin permission (UAC). Click 'Yes' to allow installation.",
        ))

    steps.append(Tier2StepPreview(
        step_id="provision_linux",
        title="Set up Linux-side tools",
        description="Installs USB/IP tools, pymobiledevice3, and the connection bridge inside Ubuntu.",
    ))

    steps.append(Tier2StepPreview(
        step_id="start_bridge",
        title="Start connection bridge",
        description="Launches the device access service that connects to your iPhone.",
    ))

    return Tier2SetupPreviewResponse(
        steps=steps,
        total_steps=len(steps),
        requires_restart=needs_restart,
        requires_elevation=needs_elevation,
    )


@router.post("/setup", response_model=Tier2StepResponse)
async def execute_setup_step(req: Tier2StepRequest) -> Tier2StepResponse:
    """
    Execute a single setup step. The frontend drives the flow step-by-step,
    showing notification gates between each step.
    """
    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()

    if orchestrator is None:
        try:
            from backend.wsl_orchestrator import WSLOrchestrator
            orchestrator = WSLOrchestrator()
        except Exception:
            raise HTTPException(status_code=503, detail="WSL orchestrator not available")

    step_id = req.step_id

    if step_id == "enable_wsl":
        result = await orchestrator.install_wsl()
    elif step_id == "install_distro":
        result = await orchestrator.install_distro()
    elif step_id == "provision_linux":
        result = await orchestrator.provision_linux()
    elif step_id == "start_bridge":
        bridge_status = await orchestrator.start_bridge()
        from backend.wsl_orchestrator import Tier2StepResult
        result = Tier2StepResult(
            step_id="start_bridge",
            completed=bridge_status.reachable,
            error=bridge_status.error,
            details={"devices": bridge_status.devices},
        )
    elif step_id == "verify_restart":
        result = await orchestrator.verify_wsl_after_restart()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown step: {step_id}")

    return Tier2StepResponse(
        step_id=result.step_id,
        completed=result.completed,
        restart_required=result.restart_required,
        error=result.error,
        next_step=result.next_step,
        details=result.details,
    )


@router.get("/usb-devices", response_model=Tier2USBDeviceListResponse)
async def list_usb_devices() -> Tier2USBDeviceListResponse:
    """List USB devices with their usbipd bind/attach state."""
    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()
    if orchestrator is None:
        return Tier2USBDeviceListResponse(devices=[])

    devices = await orchestrator.list_usb_devices()
    return Tier2USBDeviceListResponse(
        devices=[
            Tier2USBDeviceInfo(
                busid=d.busid, vid_pid=d.vid_pid,
                device_name=d.device_name, state=d.state, is_apple=d.is_apple,
            )
            for d in devices
        ]
    )


@router.post("/devices/bind-preview", response_model=Tier2BindPreviewResponse)
async def preview_device_bind(req: Tier2BindRequest) -> Tier2BindPreviewResponse:
    """
    Preview what bind does BEFORE triggering it.
    The frontend shows this explanation first, then asks for confirmation.
    """
    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="WSL orchestrator not available")

    devices = await orchestrator.list_usb_devices()
    target = next((d for d in devices if d.busid == req.busid), None)
    device_name = target.device_name if target else "Unknown device"

    return Tier2BindPreviewResponse(
        busid=req.busid,
        device_name=device_name,
        explanation=(
            f"Sharing '{device_name}' with WSL requires admin permission "
            f"one time per device. This installs a USB driver that lets "
            f"the device be accessed from Linux instead of Windows. "
            f"After this, the device will only be available in the "
            f"alternative connection (not through Apple software) until "
            f"you unplug and replug it."
        ),
        requires_restart=False,
        requires_elevation=True,
        elevation_description=(
            f"Share USB device {req.busid} with WSL. "
            f"Windows will ask for admin permission (UAC). "
            f"This only needs to happen once per device."
        ),
    )


@router.post("/devices/bind-execute", response_model=Tier2BindExecuteResponse)
async def execute_device_bind(req: Tier2BindExecuteRequest) -> Tier2BindExecuteResponse:
    """
    Actually bind + attach a device after user confirmation.
    Returns the command for Electron to elevate for bind,
    then runs attach and confirmation.
    """
    if not req.confirmed:
        raise HTTPException(status_code=400, detail="User confirmation required")

    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="WSL orchestrator not available")

    result = await orchestrator.handle_apple_device_event(req.busid)
    return Tier2BindExecuteResponse(
        busid=result.busid,
        bound=result.bound,
        attached=result.attached,
        confirmed_in_wsl=result.confirmed_in_wsl,
        error=result.error,
    )


@router.post("/devices/bind-elevated")
async def bind_device_elevated(req: Tier2BindRequest) -> dict:
    """
    Return the elevated bind command for Electron to execute.
    Does NOT run the bind itself.
    """
    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="WSL orchestrator not available")

    cmd = orchestrator.get_bind_command(req.busid)
    return cmd


@router.get("/resume")
async def check_resume() -> Tier2ResumeNotification | None:
    """Check if Tier 2 setup needs to resume after a restart."""
    from backend.wsl_orchestrator import Tier2PersistedState
    state = Tier2PersistedState.load()
    if state is None or not state.pending_step:
        return None

    return Tier2ResumeNotification(
        steps_completed=state.steps_completed,
        current_step=state.pending_step,
        message="Finishing device setup from before the restart.",
    )


@router.post("/cancel", response_model=Tier2CancelResponse)
async def cancel_setup(_: None = Depends(require_local_token)) -> Tier2CancelResponse:
    """Cancel any in-progress Tier 2 setup and clean up state.

    Terminates any running bridge process before deleting persisted state,
    so a cancelled setup doesn't leave an orphaned bridge running inside WSL.
    """
    from backend.wsl_orchestrator import Tier2PersistedState
    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()
    if orchestrator is not None:
        await orchestrator.cleanup_orphaned_bridge()
    state = Tier2PersistedState.load()
    if state:
        state.delete()
    return Tier2CancelResponse(cancelled=True, message="Tier 2 setup cancelled")


@router.post("/reset", response_model=Tier2ResetResponse)
async def reset_setup(_: None = Depends(require_local_token)) -> Tier2ResetResponse:
    """Fully reset all Tier 2 / WSL device setup state.

    This is the nuclear option for when setup gets into a stuck or
    repeatedly-failing state.  It guarantees a clean teardown of EVERYTHING
    the setup flow can leave behind, while NEVER touching the user's
    installed WSL distro or usbipd-win — those are system-level installs
    that took real time to provision.

    Specifically:
      - Terminates any running or orphaned bridge process inside WSL.
      - Resets prefer_tier2 back to its default (False).
      - Clears per-device tier preference mappings.
      - Clears any cached/incomplete setup progress state.
    """
    from backend.wsl_orchestrator import Tier2PersistedState, STATE_DIR

    manager = get_device_manager()
    orchestrator = manager.get_orchestrator()

    # 1. Terminate bridge process
    bridge_terminated = False
    if orchestrator is not None:
        try:
            await orchestrator.cleanup_orphaned_bridge()
            bridge_terminated = True
        except Exception as exc:
            logger.warning("Bridge termination during reset: %s", exc)

    # 2. Reset prefer_tier2 back to default (False)
    prefer_tier2_reset = False
    try:
        manager._backend.prefer_tier2 = False
        prefer_tier2_reset = True
        logger.info("Device setup reset: prefer_tier2 set to False")
    except Exception as exc:
        logger.warning("Failed to reset prefer_tier2: %s", exc)

    # 3. Delete persisted setup-progress state
    persisted_state_cleared = False
    try:
        state = Tier2PersistedState.load()
        if state:
            state.delete()
        # Also delete the file directly in case save was ever called
        tier2_file = STATE_DIR / "tier2_state.json"
        if tier2_file.exists():
            tier2_file.unlink()
        persisted_state_cleared = True
    except Exception as exc:
        logger.warning("Failed to clear persisted state: %s", exc)

    # 4. Clear per-device tier preference mappings
    device_preferences_cleared = False
    try:
        manager._backend.reset_device_tier_preferences()
        device_preferences_cleared = True
    except Exception as exc:
        logger.warning("Failed to clear device preferences: %s", exc)

    logger.info("Device setup reset complete — bridge=%s prefer_tier2=%s state=%s device_prefs=%s",
                bridge_terminated, prefer_tier2_reset, persisted_state_cleared,
                device_preferences_cleared)

    return Tier2ResetResponse(
        reset=True,
        message="Device setup has been fully reset. You can start the setup flow again from the beginning.",
        bridge_terminated=bridge_terminated,
        prefer_tier2_reset=prefer_tier2_reset,
        persisted_state_cleared=persisted_state_cleared,
        device_preferences_cleared=device_preferences_cleared,
    )
