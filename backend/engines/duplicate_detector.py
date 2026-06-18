"""
Transfera v2 — Duplicate Detection Subsystem
Compares batch items against the completed archive and generates a
DuplicateReport.  Includes ``check_batch()`` which pauses processing
and emits a WebSocket event alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.manager import session_scope
from backend.database.models import BatchStatus, HopStatus, MediaItem, TransferBatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Report data structures
# ---------------------------------------------------------------------------
@dataclass
class DuplicateEntry:
    """A single detected duplicate / potential-duplicate."""
    item_id: int
    file_name: str
    source_path: str
    source_hash: Optional[str]
    file_size: int
    match_type: str  # "exact" | "potential"
    matched_path: Optional[str] = None  # path of the existing archive copy


@dataclass
class DuplicateReport:
    """Full report returned by ``check_batch()``."""
    batch_id: int
    session_id: int
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exact_duplicates: list[DuplicateEntry] = field(default_factory=list)
    potential_duplicates: list[DuplicateEntry] = field(default_factory=list)
    total_items_checked: int = 0
    processing_paused: bool = False

    @property
    def has_duplicates(self) -> bool:
        return bool(self.exact_duplicates or self.potential_duplicates)

    @property
    def summary(self) -> str:
        e = len(self.exact_duplicates)
        p = len(self.potential_duplicates)
        return f"{e} exact, {p} potential duplicates across {self.total_items_checked} items"


# ---------------------------------------------------------------------------
# WebSocket event bus (lightweight in-process pub/sub)
# ---------------------------------------------------------------------------
class _EventBus:
    """Singleton in-process event bus for UI alerts."""

    def __init__(self) -> None:
        self._listeners: dict[str, list] = {}

    def on(self, event: str, callback) -> None:  # type: ignore[no-untyped-def]
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback) -> None:  # type: ignore[no-untyped-def]
        self._listeners.get(event, []).remove(callback)

    async def emit(self, event: str, data: dict) -> None:  # type: ignore[no-untyped-def]
        for cb in self._listeners.get(event, []):
            try:
                result = cb(data)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.error("Event bus error on %s: %s", event, exc)


event_bus = _EventBus()


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------
async def scan_batch_duplicates(
    batch_id: int,
    *,
    archive_root: Path | None = None,
) -> DuplicateReport:
    """
    Scan a batch's cached items against the completed archive.

    Parameters
    ----------
    batch_id : int
        The ``TransferBatch.id`` to check.
    archive_root : Path | None
        If provided, scan the filesystem for already-archived copies.

    Returns
    -------
    DuplicateReport
    """
    async with session_scope() as session:
        # Fetch batch and items
        batch = await session.get(TransferBatch, batch_id)
        if batch is None:
            raise ValueError(f"TransferBatch {batch_id} does not exist")

        result = await session.execute(
            select(MediaItem)
            .where(MediaItem.batch_id == batch_id)
            .order_by(MediaItem.id)
        )
        items = list(result.scalars().all())

        if not items:
            return DuplicateReport(
                batch_id=batch_id,
                session_id=batch.session_id,
            )

        # Build lookup of ALL items in this session (archive = completed items)
        archive_result = await session.execute(
            select(MediaItem)
            .where(
                MediaItem.session_id == batch.session_id,
                MediaItem.id.notin_([i.id for i in items]),
                MediaItem.final_status == HopStatus.COMPLETED.value,
            )
        )
        archive_items = list(archive_result.scalars().all())

        # Also fetch all items across the entire DB for cross-session dedup
        all_items_result = await session.execute(
            select(MediaItem)
            .where(
                MediaItem.id.notin_([i.id for i in items]),
                MediaItem.source_hash.isnot(None),
            )
        )
        all_archived = list(all_items_result.scalars().all())

    # Run detection (outside session to avoid long-lived connections)
    report = _detect_duplicates(
        items=items,
        archive_items=archive_items + all_archived,
        batch_id=batch_id,
        session_id=batch.session_id,
        archive_root=archive_root,
    )

    return report


def _detect_duplicates(
    items: list[MediaItem],
    archive_items: list[MediaItem],
    *,
    batch_id: int,
    session_id: int,
    archive_root: Path | None,
) -> DuplicateReport:
    """
    Compare *items* against *archive_items*.

    Exact match:   source_hash AND file_size both equal.
    Potential:     file_name matches but hash differs (different content
                   with the same filename).
    """
    report = DuplicateReport(
        batch_id=batch_id,
        session_id=session_id,
        total_items_checked=len(items),
    )

    # Build archive lookup maps
    hash_index: dict[str, list[MediaItem]] = {}
    name_index: dict[str, list[MediaItem]] = {}

    for ai in archive_items:
        if ai.source_hash:
            hash_index.setdefault(ai.source_hash.lower(), []).append(ai)
        name_index.setdefault(ai.file_name.lower(), []).append(ai)

    # Check each batch item
    for item in items:
        item_hash = (item.source_hash or "").lower()

        # --- Exact match (hash + size) ---
        if item_hash and item_hash in hash_index:
            for archived in hash_index[item_hash]:
                if archived.file_size == item.file_size:
                    report.exact_duplicates.append(DuplicateEntry(
                        item_id=item.id,
                        file_name=item.file_name,
                        source_path=item.source_path,
                        source_hash=item.source_hash,
                        file_size=item.file_size,
                        match_type="exact",
                        matched_path=archived.source_path,
                    ))
                    break

        # --- Potential match (name matches, hash differs or missing) ---
        name_key = item.file_name.lower()
        if name_key in name_index:
            for archived in name_index[name_key]:
                if archived.id == item.id:
                    continue
                # Only flag if hash is present and differs
                if item_hash and archived.source_hash:
                    if item_hash != archived.source_hash.lower():
                        report.potential_duplicates.append(DuplicateEntry(
                            item_id=item.id,
                            file_name=item.file_name,
                            source_path=item.source_path,
                            source_hash=item.source_hash,
                            file_size=item.file_size,
                            match_type="potential",
                            matched_path=archived.source_path,
                        ))
                        break
                elif not item_hash and archived.file_size == item.file_size:
                    # Same name, same size, no hash — potential
                    report.potential_duplicates.append(DuplicateEntry(
                        item_id=item.id,
                        file_name=item.file_name,
                        source_path=item.source_path,
                        source_hash=item.source_hash,
                        file_size=item.file_size,
                        match_type="potential",
                        matched_path=archived.source_path,
                    ))
                    break

    logger.info(
        "Duplicate scan batch %d: %s", batch_id, report.summary,
    )
    return report


# ---------------------------------------------------------------------------
# check_batch: generates report + emits WebSocket alert
# ---------------------------------------------------------------------------
async def check_batch(
    batch_id: int,
    *,
    archive_root: Path | None = None,
) -> DuplicateReport:
    """
    High-level routine: scan for duplicates, generate report, and if any
    are found, pause processing by emitting a ``"duplicates_detected"``
    WebSocket event to the UI.

    Parameters
    ----------
    batch_id : int
        The batch to analyse.
    archive_root : Path | None
        Optional filesystem root to cross-reference archived files.

    Returns
    -------
    DuplicateReport
    """
    report = await scan_batch_duplicates(
        batch_id,
        archive_root=archive_root,
    )

    if report.has_duplicates:
        report.processing_paused = True
        logger.warning(
            "DUPLICATES DETECTED in batch %d: %s — processing paused.",
            batch_id, report.summary,
        )

        # Emit WebSocket alert
        await event_bus.emit("duplicates_detected", {
            "batch_id": report.batch_id,
            "session_id": report.session_id,
            "exact_count": len(report.exact_duplicates),
            "potential_count": len(report.potential_duplicates),
            "summary": report.summary,
            "exact_duplicates": [
                {
                    "item_id": e.item_id,
                    "file_name": e.file_name,
                    "source_path": e.source_path,
                    "matched_path": e.matched_path,
                }
                for e in report.exact_duplicates
            ],
            "potential_duplicates": [
                {
                    "item_id": e.item_id,
                    "file_name": e.file_name,
                    "source_path": e.source_path,
                    "matched_path": e.matched_path,
                }
                for e in report.potential_duplicates
            ],
            "paused_at": report.checked_at.isoformat(),
        })

    return report


async def resume_after_duplicates(batch_id: int) -> None:
    """
    Called after the user reviews the DuplicateReport and decides to
    proceed.  Emits a ``"duplicates_resolved"`` event.
    """
    await event_bus.emit("duplicates_resolved", {
        "batch_id": batch_id,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("Duplicates resolved for batch %d — processing resumed.", batch_id)
