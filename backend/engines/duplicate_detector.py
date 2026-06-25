"""
Transfera v2 — Duplicate Detection Subsystem
Compares batch items against the completed archive and generates a
DuplicateReport.  Includes ``check_batch()`` which pauses processing
and emits a WebSocket event alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, or_, select

from backend.database.manager import session_scope
from backend.database.models import HopStatus, MediaItem, TransferBatch

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
    source_hash: str | None
    file_size: int
    match_type: str  # "exact" | "potential"
    matched_path: str | None = None  # path of the existing archive copy
    matched_item_id: int | None = None  # ID of the matched library item
    matched_file_size: int | None = None  # size of the matched item
    matched_date_taken: str | None = None  # ISO datetime of the matched item
    matched_thumbnail_url: str | None = None  # thumbnail URL for the matched item


@dataclass
class DuplicateReport:
    """Full report returned by ``check_batch()``."""
    batch_id: int
    session_id: int
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
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

        # Extract batch hashes and filenames for indexed, filtered queries
        batch_hashes = {i.source_hash for i in items if i.source_hash}
        batch_names = {i.file_name.lower() for i in items if i.file_name}

        if not batch_hashes and not batch_names:
            archive_items = []
            all_archived = []
        else:
            # Build query conditions using matching hashes or names
            conditions = []
            if batch_hashes:
                conditions.append(MediaItem.source_hash.in_(batch_hashes))
            if batch_names:
                conditions.append(func.lower(MediaItem.file_name).in_(batch_names))

            # Build lookup of matching items in this session (archive = completed items)
            archive_result = await session.execute(
                select(MediaItem)
                .where(
                    MediaItem.session_id == batch.session_id,
                    MediaItem.id.notin_([i.id for i in items]),
                    MediaItem.final_status == HopStatus.COMPLETED.value,
                    or_(*conditions),
                )
            )
            archive_items = list(archive_result.scalars().all())

            # Also fetch matching items across the entire DB for cross-session dedup
            all_items_result = await session.execute(
                select(MediaItem)
                .where(
                    MediaItem.id.notin_([i.id for i in items]),
                    MediaItem.source_hash.isnot(None),
                    or_(*conditions),
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

    def _make_matched_url(mi: MediaItem) -> str | None:
        if mi.thumbnail_path:
            return f"/api/media/{mi.id}/thumbnail?t={int(mi.updated_at.timestamp())}"
        return None

    def _make_matched_date(mi: MediaItem) -> str | None:
        if mi.date_taken:
            return mi.date_taken.isoformat()
        return None

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
                        matched_item_id=archived.id,
                        matched_file_size=archived.file_size,
                        matched_date_taken=_make_matched_date(archived),
                        matched_thumbnail_url=_make_matched_url(archived),
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
                            matched_item_id=archived.id,
                            matched_file_size=archived.file_size,
                            matched_date_taken=_make_matched_date(archived),
                            matched_thumbnail_url=_make_matched_url(archived),
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
                        matched_item_id=archived.id,
                        matched_file_size=archived.file_size,
                        matched_date_taken=_make_matched_date(archived),
                        matched_thumbnail_url=_make_matched_url(archived),
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

        # Emit WebSocket alert with full matched item details
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

    return report


async def resume_after_duplicates(batch_id: int) -> None:
    """
    Called after the user reviews the DuplicateReport and decides to
    proceed.  Emits a ``"duplicates_resolved"`` event.
    """
    await event_bus.emit("duplicates_resolved", {
        "batch_id": batch_id,
        "resolved_at": datetime.now(UTC).isoformat(),
    })
    logger.info("Duplicates resolved for batch %d — processing resumed.", batch_id)


# ---------------------------------------------------------------------------
# Pre-scan (hash-free, filename + size match)
# ---------------------------------------------------------------------------

async def prescan_against_library(
    candidates: list[dict],
) -> dict:
    """
    Fast pre-scan: compare candidate source files against the existing
    library by (file_name, file_size) BEFORE any hashing/caching happens.

    Parameters
    ----------
    candidates : list[dict]
        Each dict has at minimum: "abs_path", "filename", "size_bytes" —
        matching the shape of items from GET /api/device/preview.

    Returns
    -------
    dict with shape:
      {
        "checked": int,
        "likely_duplicate_count": int,
        "likely_duplicate_paths": list[str],
      }
    """
    if not candidates:
        return {"checked": 0, "likely_duplicate_count": 0, "likely_duplicate_paths": []}


    from backend.database.manager import session_scope
    from backend.database.models import HopStatus

    async with session_scope() as session:
        result = await session.execute(
            select(MediaItem.file_name, MediaItem.file_size).where(
                MediaItem.final_status == HopStatus.COMPLETED.value
            )
        )
        rows = result.fetchall()

    # Build O(1) lookup set: (lowercase_name, size_bytes) -> True
    library_set: set[tuple[str, int]] = {
        (row[0].lower(), row[1]) for row in rows if row[0] and row[1] is not None
    }

    likely_duplicate_paths: list[str] = []
    for c in candidates:
        name = (c.get("filename") or "").lower()
        size = c.get("size_bytes") or 0
        if (name, size) in library_set:
            path = c.get("abs_path") or ""
            if path:
                likely_duplicate_paths.append(path)

    return {
        "checked": len(candidates),
        "likely_duplicate_count": len(likely_duplicate_paths),
        "likely_duplicate_paths": likely_duplicate_paths,
    }
