"""
Transfera v2 — Pydantic Schemas
Request / Response validation for all API endpoints.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


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
    source_path: str = Field(..., min_length=1, description="Directory or file to scan")
    session_name: Optional[str] = Field(None, max_length=255)
    dest_path: Optional[str] = Field(None, description="Destination root for archive")

    @field_validator("source_path")
    @classmethod
    def source_not_empty(cls, v: str) -> str:
        if not v.strip():
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
    source_root: str = Field(..., min_length=1)
    dest_root: str = Field(..., min_length=1)
    transfer_mode: str = Field(
        "copy",
        pattern="^(copy|move)$",
        description="'copy' for backup mode, 'move' for space-saver mode",
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
# Folder Metadata
# ---------------------------------------------------------------------------
class FolderMetadataRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Absolute directory path to analyze")


class FolderMetadataResponse(BaseModel):
    path: str
    size_gb: float
    file_count: int


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    detail: str
    code: str = "error"
