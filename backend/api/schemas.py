"""
Transfera v2 — Pydantic Schemas
Request / Response validation for all API endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.api.source_types import (
    SourceRef,
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


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
class ScanRequest(BaseModel):
    source_path: str = Field("", description="Directory or file to scan (legacy string path)")
    source_ref: SourceRef | None = Field(None, description="Typed source reference (preferred)")
    session_name: str | None = Field(None, max_length=255)
    dest_path: str | None = Field(None, description="Destination root for archive")

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
    source_ref: SourceRef | None = Field(None, description="Typed source reference (preferred)")
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
    selected_files: list[str] | None = Field(
        None,
        description="If provided, only these source file paths are transferred (subset of source dir)",
    )
    folder_layout: Literal["year/month/day", "year/month", "flat"] = Field(
        "year/month",
        description="Destination folder structure: year/month/day, year/month, or flat (all files in root)",
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
    folder_layout: str = "year/month"
    total_bytes_volume: int | None = None
    session_report_path: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


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
    extension: str | None = None
    mime_type: str | None = None
    hop1_status: str
    hop2_status: str
    final_status: str
    live_photo_group: str | None = None
    thumbnail_url: str | None = None
    thumbnail_status: str = "pending"
    date_taken: datetime | None = None
    date_source: str | None = None
    error_message: str | None = None
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
    source_hash: str | None = None
    file_size: int
    match_type: str
    matched_path: str | None = None
    matched_item_id: int | None = None
    matched_file_size: int | None = None
    matched_date_taken: str | None = None
    matched_thumbnail_url: str | None = None


class DuplicateReportResponse(BaseModel):
    batch_id: int
    session_id: int
    checked_at: datetime
    exact_duplicates: list[DuplicateEntrySchema]
    potential_duplicates: list[DuplicateEntrySchema]
    total_items_checked: int
    processing_paused: bool
    summary: str


class PrescanCandidate(BaseModel):
    abs_path: str
    filename: str
    size_bytes: int


class PrescanRequest(BaseModel):
    candidates: list[PrescanCandidate]


class PrescanResponse(BaseModel):
    checked: int = 0
    likely_duplicate_count: int = 0
    likely_duplicate_paths: list[str] = []


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
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
    source_ref: SourceRef | None = Field(None, description="Typed source reference (preferred)")
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
    error_detail: str | None = Field(
        None,
        description="Human-readable detail when status is 'error'",
    )
    active_tier: str | None = Field(
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
    device_name: str | None = None
    last_successful_cutoff: datetime | None = None
    last_import_session_id: int | None = None
    updated_at: datetime | None = None


class DeviceImportStateListResponse(BaseModel):
    devices: list[DeviceImportStateResponse]


# ---------------------------------------------------------------------------
# iOS Driver Installer
# ---------------------------------------------------------------------------
class InstallerStatusResponse(BaseModel):
    winget_available: bool
    winget_version: str | None = None
    driver_status: str


class PackageVerificationResponse(BaseModel):
    success: bool
    package_id: str | None = None
    package_name: str | None = None
    version: str | None = None
    error: str | None = None


class InstallDriverResponse(BaseModel):
    success: bool
    exit_code: int | None = None
    error: str | None = None
    message: str = ""


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

    # Cumulative progress counters (never reset between batches)
    total_files: int = 0
    cached_files: int = 0
    imported_files: int = 0
    failed_files: int = 0
    current_batch: int = 0
    total_batches: int = 0
    progress_percent: float = 0.0

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

    # --- Timing & speed (server-computed) ---
    elapsed_seconds: int = 0
    eta_seconds: int | None = None
    speed_files_per_sec: float = 0.0


# ---------------------------------------------------------------------------
# Clear / Purge
# ---------------------------------------------------------------------------
class ClearSessionsRequest(BaseModel):
    older_than_days: int | None = Field(
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
