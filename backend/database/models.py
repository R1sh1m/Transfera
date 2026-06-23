"""
Transfera v2 — SQLAlchemy ORM Models
Mapped-column API (SQLAlchemy 2.0 style).
Tables: media_items, transfer_sessions, transfer_batches, device_import_states.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------
class HopStatus(str, enum.Enum):
    PENDING = "pending"
    SCANNING = "scanning"
    SCANNED = "scanned"
    HASHING = "hashing"
    HASHED = "hashed"
    TRANSFERRING = "transferring"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SessionStatus(str, enum.Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    LOADING = "loading"        # Hop 1 in progress (source -> cache)
    ARCHIVED = "archived"      # Hop 2 in progress (cache -> destination)
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# media_items
# ---------------------------------------------------------------------------
class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(String(4096), nullable=False, unique=True)
    source_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extension: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- Hop state machine ---
    hop1_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=HopStatus.PENDING.value
    )
    hop2_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=HopStatus.PENDING.value
    )
    final_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=HopStatus.PENDING.value
    )

    # --- Foreign keys ---
    batch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transfer_batches.id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transfer_sessions.id", ondelete="SET NULL"), nullable=True
    )

    # --- Error tracking ---
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Thumbnail ---
    thumbnail_path: Mapped[str | None] = mapped_column(
        String(4096), nullable=True
    )
    thumbnail_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # 'pending' | 'ready' | 'failed'

    # --- Date resolution ---
    date_taken: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    date_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # "exif", "file_modified", or None (unsorted)

    # --- Live Photo grouping ---
    live_photo_group: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # --- Original capture time (extracted pre-copy for sort order) ---
    original_capture_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # --- Relationships ---
    batch: Mapped[TransferBatch | None] = relationship(
        back_populates="items", lazy="selectin"
    )
    session: Mapped[TransferSession | None] = relationship(
        back_populates="items", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_media_items_hop1_status", "hop1_status"),
        Index("ix_media_items_hop2_status", "hop2_status"),
        Index("ix_media_items_final_status", "final_status"),
        Index("ix_media_items_batch_id", "batch_id"),
        Index("ix_media_items_session_id", "session_id"),
        Index("ix_media_items_source_hash", "source_hash"),
        Index("ix_media_items_source_path", "source_path"),
        Index("ix_media_items_filename_size", "file_name", "file_size"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("hop1_status", HopStatus.PENDING.value)
        kwargs.setdefault("hop2_status", HopStatus.PENDING.value)
        kwargs.setdefault("final_status", HopStatus.PENDING.value)
        kwargs.setdefault("file_size", 0)
        kwargs.setdefault("retry_count", 0)
        now = _utcnow()
        kwargs.setdefault("created_at", now)
        kwargs.setdefault("updated_at", now)
        super().__init__(**kwargs)

    def touch(self) -> None:
        """Manually bump updated_at (SQLite has no auto-update hook)."""
        self.updated_at = _utcnow()

    def __repr__(self) -> str:
        return f"<MediaItem id={self.id} file={self.file_name!r}>"


# ---------------------------------------------------------------------------
# transfer_sessions
# ---------------------------------------------------------------------------
class TransferSession(Base):
    __tablename__ = "transfer_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_root: Mapped[str] = mapped_column(String(4096), nullable=False)
    dest_root: Mapped[str] = mapped_column(String(4096), nullable=False)
    transfer_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="copy"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SessionStatus.CREATED.value
    )

    # --- Counters ---
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Cumulative progress counters (never reset between batches) ---
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_batch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Volume tracking ---
    total_bytes_volume: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, default=None
    )

    # --- Report path ---
    session_report_path: Mapped[str | None] = mapped_column(
        String(4096), nullable=True, default=None
    )

    # --- Error tracking ---
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Folder layout ---
    folder_layout: Mapped[str] = mapped_column(
        String(32), nullable=False, default="year/month"
    )

    # --- Incremental import ---
    only_new_mode: Mapped[bool] = mapped_column(nullable=False, default=False)

    # --- Duplicate resolution persistence ---
    resolved_batch_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    duplicate_resolutions_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )

    # --- Pause / resume timing ---
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    total_paused_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # --- Speed tracking ---
    speed_samples: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Relationships ---
    batches: Mapped[list[TransferBatch]] = relationship(
        back_populates="session", lazy="selectin", cascade="all, delete-orphan"
    )
    items: Mapped[list[MediaItem]] = relationship(
        back_populates="session", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_transfer_sessions_status", "status"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("status", SessionStatus.CREATED.value)
        kwargs.setdefault("total_items", 0)
        kwargs.setdefault("completed_items", 0)
        kwargs.setdefault("failed_items", 0)
        kwargs.setdefault("total_files", 0)
        kwargs.setdefault("cached_files", 0)
        kwargs.setdefault("imported_files", 0)
        kwargs.setdefault("failed_files", 0)
        kwargs.setdefault("current_batch", 0)
        kwargs.setdefault("total_batches", 0)
        kwargs.setdefault("folder_layout", "year/month")
        kwargs.setdefault("total_paused_ms", 0)
        now = _utcnow()
        kwargs.setdefault("created_at", now)
        kwargs.setdefault("updated_at", now)
        super().__init__(**kwargs)

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def __repr__(self) -> str:
        return f"<TransferSession id={self.id} name={self.session_name!r}>"


# ---------------------------------------------------------------------------
# transfer_batches
# ---------------------------------------------------------------------------
class TransferBatch(Base):
    __tablename__ = "transfer_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transfer_sessions.id", ondelete="CASCADE"), nullable=False
    )
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BatchStatus.PENDING.value
    )

    # --- Counters ---
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Error tracking ---
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Relationships ---
    session: Mapped[TransferSession] = relationship(
        back_populates="batches", lazy="selectin"
    )
    items: Mapped[list[MediaItem]] = relationship(
        back_populates="batch", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_transfer_batches_session_id", "session_id"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("batch_number", 1)
        kwargs.setdefault("status", BatchStatus.PENDING.value)
        kwargs.setdefault("total_items", 0)
        kwargs.setdefault("completed_items", 0)
        kwargs.setdefault("failed_items", 0)
        now = _utcnow()
        kwargs.setdefault("created_at", now)
        kwargs.setdefault("updated_at", now)
        super().__init__(**kwargs)

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def __repr__(self) -> str:
        return f"<TransferBatch id={self.id} batch={self.batch_number}>"


# ---------------------------------------------------------------------------
# device_import_states
# ---------------------------------------------------------------------------
class DeviceImportState(Base):
    """
    Persistent per-device record tracking the last successful import cutoff.

    Keyed by a stable device identifier (e.g. UDID/serial), not the display
    name. The cutoff is the modified-time of the oldest item that was NOT
    successfully handled in the last session — or, if all items succeeded,
    the newest item's mtime. Files at or below this timestamp can be safely
    skipped on subsequent "only new" imports.
    """

    __tablename__ = "device_import_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_successful_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_import_session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transfer_sessions.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_device_import_states_device_id", "device_id"),
    )

    def __init__(self, **kwargs: object) -> None:
        now = _utcnow()
        kwargs.setdefault("updated_at", now)
        super().__init__(**kwargs)

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def __repr__(self) -> str:
        return f"<DeviceImportState device_id={self.device_id!r} cutoff={self.last_successful_cutoff}>"
