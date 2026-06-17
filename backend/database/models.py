"""
MediaVault v2 — SQLAlchemy ORM Models
Mapped-column API (SQLAlchemy 2.0 style).
Tables: media_items, transfer_sessions, transfer_batches.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    source_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mime_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    extension: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

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
    batch_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("transfer_batches.id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("transfer_sessions.id", ondelete="SET NULL"), nullable=True
    )

    # --- Error tracking ---
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Live Photo grouping ---
    live_photo_group: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # --- Relationships ---
    batch: Mapped[Optional["TransferBatch"]] = relationship(
        back_populates="items", lazy="selectin"
    )
    session: Mapped[Optional["TransferSession"]] = relationship(
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
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SessionStatus.CREATED.value
    )

    # --- Counters ---
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Error tracking ---
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Relationships ---
    batches: Mapped[List["TransferBatch"]] = relationship(
        back_populates="session", lazy="selectin", cascade="all, delete-orphan"
    )
    items: Mapped[List["MediaItem"]] = relationship(
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
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Relationships ---
    session: Mapped["TransferSession"] = relationship(
        back_populates="batches", lazy="selectin"
    )
    items: Mapped[List["MediaItem"]] = relationship(
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
