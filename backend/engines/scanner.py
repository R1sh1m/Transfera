"""
Transfera v2 — Strict Oldest-First Media Scanner
Walks directories, detects Live Photo pairs, enforces chronological insertion order.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import (
    ALL_MEDIA_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from backend.database.manager import session_scope
from backend.database.models import HopStatus, MediaItem
from backend.engines.metadata_extractor import FileMetadata, extract_metadata

logger = logging.getLogger(__name__)

# Type alias for the progress callback
ProgressCallback = Optional[Callable[[int, int, str], None]]


# ---------------------------------------------------------------------------
# Live Photo detection helpers
# ---------------------------------------------------------------------------
def _normalise_stem(stem: str) -> str:
    """Lower-case, strip whitespace — for case-insensitive grouping."""
    return stem.strip().lower()


def _detect_live_photo_groups(
    files: list[Path],
) -> dict[str, str]:
    """
    Group files by (parent_dir, normalised stem). If a group contains at
    least one image extension AND one video extension, every MEDIA file in
    that group receives the same UUID string as its ``live_photo_group`` id.

    Returns a mapping ``{ resolved_path_str: live_photo_group_uuid }``.
    """
    # Only consider image and video files for Live Photo pairing
    media_exts = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in files:
        if p.suffix.lower() not in media_exts:
            continue
        key = (str(p.parent), _normalise_stem(p.stem))
        groups[key].append(p)

    result: dict[str, str] = {}
    for key, members in groups.items():
        exts = {m.suffix.lower() for m in members}
        has_image = bool(exts & IMAGE_EXTENSIONS)
        has_video = bool(exts & VIDEO_EXTENSIONS)
        if has_image and has_video:
            group_id = str(uuid.uuid4())
            for m in members:
                result[str(m.resolve())] = group_id
    return result


# ---------------------------------------------------------------------------
# Chronological sort key
# ---------------------------------------------------------------------------
def _sort_key(meta: FileMetadata) -> datetime:
    """
    Return the best available timestamp for chronological ordering.

    Priority: date_taken (EXIF) → date_created → date_modified.
    Falls back to the Unix epoch if no timestamp is available.
    """
    EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return meta.date_taken or meta.date_created or meta.date_modified or EPOCH


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
async def scan(
    source_path: str | Path,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
) -> list[int]:
    """
    Walk *source_path*, extract metadata, detect Live Photo pairs, sort
    chronologically (oldest first), and insert/update ``media_items`` rows.

    Parameters
    ----------
    source_path : str | Path
        Root directory (or single file) to scan.
    session_id : int | None
        Optional session FK to attach to newly created rows.
    on_progress : callback | None
        ``callback(processed, total, current_file)`` invoked per file.

    Returns
    -------
    list[int]
        Ordered list of ``media_items.id`` values (oldest → newest).
    """
    source = Path(source_path).resolve()

    # 1. Collect all media files
    if source.is_file():
        if source.suffix.lower() in ALL_MEDIA_EXTENSIONS:
            media_files = [source]
        else:
            logger.info("Skipping non-media file: %s", source)
            media_files = []
    elif source.is_dir():
        media_files = sorted(
            p for p in source.rglob("*")
            if p.is_file() and p.suffix.lower() in ALL_MEDIA_EXTENSIONS
        )
    else:
        logger.error("Source path does not exist: %s", source)
        return []

    total = len(media_files)
    logger.info("Found %d media files under %s", total, source)

    if total == 0:
        return []

    # 2. Detect Live Photo groups
    lp_groups = _detect_live_photo_groups(media_files)

    # 3. Extract metadata for every file
    entries: list[tuple[Path, FileMetadata, str | None]] = []
    for idx, fpath in enumerate(media_files):
        try:
            meta = extract_metadata(fpath)
        except Exception as exc:
            logger.warning("Failed to extract metadata for %s: %s", fpath, exc)
            stat = fpath.stat()
            from backend.engines.metadata_extractor import _ts_to_datetime
            meta = FileMetadata(
                file_path=str(fpath.resolve()),
                file_name=fpath.name,
                file_size=stat.st_size,
                extension=fpath.suffix.lower(),
                date_created=_ts_to_datetime(stat.st_ctime),
                date_modified=_ts_to_datetime(stat.st_mtime),
            )
        lp_id = lp_groups.get(str(fpath.resolve()))
        entries.append((fpath, meta, lp_id))
        if on_progress is not None:
            on_progress(idx + 1, total, str(fpath))

    # 4. Sort chronologically (oldest first)
    entries.sort(key=lambda e: _sort_key(e[1]))
    logger.info("Entries sorted chronologically (oldest -> newest).")

    # 5. Insert / upsert into database
    inserted_ids: list[int] = []

    async with session_scope() as session:
        for fpath, meta, lp_group_id in entries:
            row_id = await _upsert_media_item(
                session,
                meta=meta,
                live_photo_group=lp_group_id,
                session_id=session_id,
            )
            inserted_ids.append(row_id)

    logger.info("Scan complete — %d items persisted.", len(inserted_ids))
    return inserted_ids


# ---------------------------------------------------------------------------
# Upsert logic (dedup by source_path)
# ---------------------------------------------------------------------------
async def _upsert_media_item(
    session: AsyncSession,
    *,
    meta: FileMetadata,
    live_photo_group: str | None,
    session_id: int | None,
) -> int:
    """
    If ``source_path`` already exists with a non-FAILED status, return its
    existing id. Otherwise insert a new row and return the new id.
    """
    result = await session.execute(
        select(MediaItem.id, MediaItem.final_status).where(
            MediaItem.source_path == meta.file_path
        )
    )
    existing = result.first()

    if existing is not None:
        existing_id, status = existing
        if status != HopStatus.FAILED.value:
            logger.debug("Reusing existing row id=%d for %s", existing_id, meta.file_path)
            return existing_id

    item = MediaItem(
        source_path=meta.file_path,
        file_name=meta.file_name,
        file_size=meta.file_size,
        extension=meta.extension,
        mime_type=meta.mime_type,
        hop1_status=HopStatus.SCANNED.value,
        hop2_status=HopStatus.PENDING.value,
        final_status=HopStatus.PENDING.value,
        session_id=session_id,
        live_photo_group=live_photo_group,
    )
    session.add(item)
    await session.flush()

    return item.id  # type: ignore[return-value]
