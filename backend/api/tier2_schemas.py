"""
Transfera v2 -- Tier 2 API Schemas
Pydantic models for WSL2/usbipd device access endpoints.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Tier2StatusResponse(BaseModel):
    wsl_installed: bool = False
    distro_name: Optional[str] = None
    distro_ready: bool = False
    usbipd_installed: bool = False
    usbipd_version: Optional[str] = None
    bridge_running: bool = False
    bridge_reachable: bool = False
    virtualization_available: bool = True
    restart_required: bool = False
    active_tier: str = "none"
    devices_on_tier2: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class Tier2StepPreview(BaseModel):
    step_id: str
    title: str
    description: str
    requires_restart: bool = False
    requires_elevation: bool = False
    elevation_description: Optional[str] = None
    restart_description: Optional[str] = None
    can_cancel: bool = True


class Tier2SetupPreviewResponse(BaseModel):
    steps: list[Tier2StepPreview]
    total_steps: int
    requires_restart: bool
    requires_elevation: bool


class Tier2StepRequest(BaseModel):
    step_id: str
    confirmed: bool = False


class Tier2StepResponse(BaseModel):
    step_id: str
    completed: bool
    restart_required: bool = False
    error: Optional[str] = None
    next_step: Optional[str] = None
    details: dict = Field(default_factory=dict)


class Tier2BindRequest(BaseModel):
    busid: str = Field(..., min_length=1)
    serial: Optional[str] = None


class Tier2BindPreviewResponse(BaseModel):
    busid: str
    device_name: str
    explanation: str
    requires_restart: bool = False
    requires_elevation: bool = True
    elevation_description: str


class Tier2BindExecuteRequest(BaseModel):
    busid: str = Field(..., min_length=1)
    confirmed: bool = False


class Tier2BindExecuteResponse(BaseModel):
    busid: str
    bound: bool = False
    attached: bool = False
    confirmed_in_wsl: bool = False
    error: Optional[str] = None


class Tier2USBDeviceInfo(BaseModel):
    busid: str
    vid_pid: str
    device_name: str
    state: str
    is_apple: bool = False


class Tier2USBDeviceListResponse(BaseModel):
    devices: list[Tier2USBDeviceInfo]


class Tier2ResumeNotification(BaseModel):
    steps_completed: list[str]
    current_step: str
    message: str


class Tier2RestartRequest(BaseModel):
    action: str = Field(..., pattern="^(now|later)$")


class Tier2CancelResponse(BaseModel):
    cancelled: bool
    message: str


class Tier2ResetResponse(BaseModel):
    reset: bool
    message: str
    bridge_terminated: bool = False
    prefer_tier2_reset: bool = False
    persisted_state_cleared: bool = False
    device_preferences_cleared: bool = False
