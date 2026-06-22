"""
Transfera v2 — Pydantic Schemas
Request / Response validation for all API endpoints.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from backend.api.source_types import (
    SourceRef,
    SourceRefDevice,
    SourceRefLocal,
    legacy_string_to_source_ref,
    source_ref_to_legacy_string,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class SessionStatusEnum(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HopStatusEnum(str, Enum):
    PENDING = "pending"
    SCANNING = "scanning"
    SCANNED = "scanned"
    HASHING = "hashing"
    HASHED = "hashed"
    TRANSFERRING = "transferring"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class BatchStatusEnum(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    LOADING = "loading"
    ARCHIVED = "archived"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "2.0.0"
    port: int
    database: str = "connected"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class ConfigResponse(BaseModel):
    port: int
    host: str
    batch_size: int
    max_retry: int
    cache_dir: str
    db_dir: str
    image_extensions: list[str]
    video_extensions: list[str]
    audio_extensions: list[str]
    document_extensions: list[str]
    local_secret_token: str = ""


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
class ScanRequest(BaseModel):
    source_path: str = Field("", description="Directory or file to scan (legacy string path)")
    source_ref: Optional[SourceRef] = Field(None, description="Typed source reference (preferred)")
    session_name: Optional[str] = Field(None, max_length=255)
    dest_path: Optional[str] = Field(None, description="Destination root for archive")

    @field_validator("source_path")
    @classmethod
    def source_not_empty(cls, v: str) -> str:
        if v and not v.strip():
            raise ValueError("source_path must not be blank")
        return v.strip()


class ScanResponse(BaseModel):
    session_id: int
    status: str
    message: str


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class SessionCreate(BaseModel):
    session_name: str = Field(..., min_length=1, max_length=255)
    source_root: str = Field("", description="Legacy source path string (for backward compat)")
    source_ref: Optional[SourceRef] = Field(None, description="Typed source reference (preferred)")
    dest_root: str = Field(..., min_length=1)
    transfer_mode: str = Field(
        "copy",
        pattern="^(copy|move)$",
        description="'copy' for backup mode, 'move' for space-saver mode",
    )
    only_new_since_last_import: bool = Field(
        False,
        description="If True and source is a device, skip files at or before the last import cutoff",
    )


class SessionInfo(BaseModel):
    id: int
    session_name: str
    source_root: str
    dest_root: str
    transfer_mode: str = "copy"
    status: str
    total_items: int
    completed_items: int
    failed_items: int
    only_new_mode: bool = False
    total_bytes_volume: Optional[int] = None
    session_report_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class SessionList(BaseModel):
    sessions: list[SessionInfo]
    total: int


class SessionActionResponse(BaseModel):
    session_id: int
    status: str
    message: str


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------
class BatchInfo(BaseModel):
    id: int
    session_id: int
    batch_number: int
    status: str
    total_items: int
    completed_items: int
    failed_items: int
    created_at: datetime
    updated_at: datetime


class BatchList(BaseModel):
    batches: list[BatchInfo]
    total: int


# ---------------------------------------------------------------------------
# Media Item
# ---------------------------------------------------------------------------
class MediaItemInfo(BaseModel):
    id: int
    source_path: str
    file_name: str
    file_size: int
    extension: Optional[str] = None
    mime_type: Optional[str] = None
    hop1_status: str
    hop2_status: str
    final_status: str
    live_photo_group: Optional[str] = None
    thumbnail_url: Optional[str] = None
    date_taken: Optional[datetime] = None
    date_source: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime



class MediaList(BaseModel):
    items: list[MediaItemInfo]
    total: int
    page: int
    page_size: int
    pages: int


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------
class DuplicateEntrySchema(BaseModel):
    item_id: int
    file_name: str
    source_path: str
    source_hash: Optional[str] = None
    file_size: int
    match_type: str
    matched_path: Optional[str] = None
    matched_item_id: Optional[int] = None
    matched_file_size: Optional[int] = None
    matched_date_taken: Optional[str] = None
    matched_thumbnail_url: Optional[str] = None


class DuplicateReportResponse(BaseModel):
    batch_id: int
    session_id: int
    checked_at: datetime
    exact_duplicates: list[DuplicateEntrySchema]
    potential_duplicates: list[DuplicateEntrySchema]
    total_items_checked: int
    processing_paused: bool
    summary: str


class DuplicateCheckRequest(BaseModel):
    batch_id: int = Field(..., gt=0)


class DuplicateResolutionItem(BaseModel):
    item_id: int
    action: Literal["skip", "overwrite", "keep_both"]


class DuplicateResolveRequest(BaseModel):
    batch_id: int = Field(..., gt=0)
    resolutions: list[DuplicateResolutionItem]


# ---------------------------------------------------------------------------
# WebSocket Events (15 system-wide events)
# ---------------------------------------------------------------------------
class WSEventType(str, Enum):
    # Scan events
    SCAN_PROGRESS = "scan_progress"
    SCAN_COMPLETE = "scan_complete"
    # Batch events
    BATCH_CREATED = "batch_created"
    BATCH_PROCESSING = "batch_processing"
    BATCH_COMPLETE = "batch_complete"
    # Hop 1 events
    HOP1_PROGRESS = "hop1_progress"
    HOP1_COMPLETE = "hop1_complete"
    # Hop 2 events
    HOP2_PROGRESS = "hop2_progress"
    HOP2_COMPLETE = "hop2_complete"
    # Duplicate events
    DUPLICATES_DETECTED = "duplicates_detected"
    DUPLICATES_RESOLVED = "duplicates_resolved"
    # Session events
    SESSION_STARTED = "session_started"
    SESSION_PAUSED = "session_paused"
    SESSION_COMPLETE = "session_complete"
    # System events
    ERROR = "error"
    PONG = "pong"


class WSEvent(BaseModel):
    event: str
    data: dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.utcnow())


# ---------------------------------------------------------------------------
# Directory Size
# ---------------------------------------------------------------------------
class DirSizeRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Directory path to measure")


class DirSizeResponse(BaseModel):
    path: str
    total_bytes: int
    file_count: int
    folder_count: int
    readable: str  # human-readable size string (e.g. "12.4 GB")


# ---------------------------------------------------------------------------
# Disk Space (drive-level free space)
# ---------------------------------------------------------------------------
class DiskSpaceRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Path on the drive to query")


class DiskSpaceResponse(BaseModel):
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int


# ---------------------------------------------------------------------------
# Folder Metadata
# ---------------------------------------------------------------------------
class FolderMetadataRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Absolute directory path to analyze")


class FolderMetadataResponse(BaseModel):
    path: str
    size_gb: float
    file_count: int


# ---------------------------------------------------------------------------
# Preflight Disk Validation
# ---------------------------------------------------------------------------
class PreflightValidateRequest(BaseModel):
    source_path: str = Field("", description="Source directory to measure (legacy)")
    source_ref: Optional[SourceRef] = Field(None, description="Typed source reference (preferred)")
    dest_path: str = Field(..., min_length=1, description="Destination drive/directory to check free space")

    @field_validator("source_path", "dest_path")
    @classmethod
    def path_not_empty(cls, v: str) -> str:
        if v and not v.strip():
            raise ValueError("path must not be blank")
        return v.strip()


class PreflightValidateResponse(BaseModel):
    source_size_bytes: int
    dest_free_bytes: int
    is_sufficient: bool
    file_count: int


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------
class PathValidateRequest(BaseModel):
    path: str = Field(..., min_length=1)

class PathValidateResponse(BaseModel):
    path: str
    exists: bool
    is_dir: bool
    readable: bool


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    detail: str
    code: str = "error"


# ---------------------------------------------------------------------------
# iOS Device
# ---------------------------------------------------------------------------
class IOSDeviceInfo(BaseModel):
    serial: str
    name: str
    model: str
    ios_version: str
    connection_type: str
    status: str
    error_detail: Optional[str] = Field(
        None,
        description="Human-readable detail when status is 'error'",
    )
    active_tier: Optional[str] = Field(
        None,
        description="Which tier is serving this device: 'tier1' (Apple driver) or 'tier2' (WSL bridge)",
    )


class IOSDeviceListResponse(BaseModel):
    available: bool
    driver_status: str = "unknown"  # "ready" | "no_driver" | "no_pymobiledevice3"
    prefer_tier2: bool = Field(
        False,
        description="Global preference: when True, Tier 2 (open-source bridge) is tried first",
    )
    devices: list[IOSDeviceInfo]


class IOSBrowseRequest(BaseModel):
    serial: str = Field(..., min_length=1)
    path: str = Field("/", description="Directory path on the device")


class IOSDeviceInfoRequest(BaseModel):
    serial: str = Field(..., min_length=1)
    path: str = Field("/", description="File or directory path on the device")


class IOSDeviceFileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float


class IOSBrowseResponse(BaseModel):
    serial: str
    path: str
    entries: list[IOSDeviceFileEntry]


# ---------------------------------------------------------------------------
# Device Import State (incremental import tracking)
# ---------------------------------------------------------------------------
class DeviceImportStateResponse(BaseModel):
    device_id: str
    device_name: Optional[str] = None
    last_successful_cutoff: Optional[datetime] = None
    last_import_session_id: Optional[int] = None
    updated_at: datetime


class DeviceImportStateListResponse(BaseModel):
    devices: list[DeviceImportStateResponse]


# ---------------------------------------------------------------------------
# iOS Driver Installer
# ---------------------------------------------------------------------------
class InstallerStatusResponse(BaseModel):
    winget_available: bool
    winget_version: Optional[str] = None
    driver_status: str


class PackageVerificationResponse(BaseModel):
    success: bool
    package_id: Optional[str] = None
    package_name: Optional[str] = None
    version: Optional[str] = None
    error: Optional[str] = None


class InstallDriverRequest(BaseModel):
    pass  # No parameters needed — the command is always the same


class InstallDriverResponse(BaseModel):
    executable: str
    args: list[str]
    message: str


# ---------------------------------------------------------------------------
# Device Backend Preference
# ---------------------------------------------------------------------------
class DevicePreferenceResponse(BaseModel):
    prefer_tier2: bool = Field(
        description="Global preference: when True, Tier 2 is tried first",
    )


class DevicePreferenceRequest(BaseModel):
    prefer_tier2: bool = Field(
        description="Set to True to prefer the open-source bridge over Apple's driver",
    )


# ---------------------------------------------------------------------------
# Session Progress (polling-based live data)
# ---------------------------------------------------------------------------
class RecentItemProgress(BaseModel):
    item_id: int
    file_name: str
    hop1_status: str
    hop2_status: str
    thumbnail_url: str | None = None
    updated_at: datetime


class SessionProgressResponse(BaseModel):
    session_id: int
    status: str
    total_items: int
    completed_items: int
    failed_items: int

    current_item_id: int | None = None
    current_file_name: str = ""
    current_hop: str = ""

    active_batch_id: int | None = None
    active_batch_number: int = 0
    active_batch_status: str = ""
    active_batch_total: int = 0
    active_batch_completed: int = 0
    active_batch_hop1_progress: int = 0
    active_batch_hop2_progress: int = 0

    recent_items: list[RecentItemProgress] = []

    started_at: datetime | None = None
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Clear / Purge
# ---------------------------------------------------------------------------
class ClearSessionsRequest(BaseModel):
    older_than_days: Optional[int] = Field(
        None,
        ge=1,
        description="If set, only clear sessions created more than N days ago. "
                    "If omitted, all sessions are cleared.",
    )


class ClearResponse(BaseModel):
    message: str
    sessions_cleared: int = 0
    batches_cleared: int = 0
    media_items_cleared: int = 0
    thumbnails_removed: int = 0
    cache_files_removed: int = 0
