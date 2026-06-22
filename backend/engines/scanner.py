"""
Transfera v2 — Strict Oldest-First Media Scanner
Walks directories (local or iOS device via AFC), detects Live Photo pairs,
enforces chronological insertion order. Supports optional mtime-based cutoff
for incremental imports from device sources.

Supports both raw path strings (legacy) and SourceRef typed references.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import (
    ALL_MEDIA_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from backend.database.manager import session_scope
from backend.database.models import HopStatus, MediaItem
from backend.engines.date_resolver import resolve_item_date
from backend.engines.metadata_extractor import FileMetadata, extract_metadata, _ts_to_datetime
from backend.ios_device import (
    IOS_SOURCE_PREFIX,
    browse_device_directory,
    is_ios_source,
    is_wpd_device_id,
    parse_ios_source,
)
from backend.api.source_types import SourceRef, SourceRefDevice, SourceRefLocal

logger = logging.getLogger(__name__)

# Type alias for the progress callback
ProgressCallback = Optional[Callable[[int, int, str], None]]


# ---------------------------------------------------------------------------
# Live Photo detection helpers
# ---------------------------------------------------------------------------
def _normalise_stem(stem: str) -> str:
    """Lower-case, strip whitespace — for case-insensitive grouping."""
    return stem.strip().lower()


def _detect_live_photo_groups_from_entries(
    entries: list[FileMetadata],
) -> dict[str, str]:
    """
    Group files by (parent_dir, normalised stem). If a group contains at
    least one image extension AND one video extension, every MEDIA file in
    that group receives the same UUID string as its ``live_photo_group`` id.

    Works with both local Path-based and iOS device-based file entries.

    Returns a mapping ``{ source_path: live_photo_group_uuid }``.
    """
    media_exts = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    groups: dict[tuple[str, str], list[FileMetadata]] = defaultdict(list)

    for meta in entries:
        ext = (meta.extension or "").lower()
        if ext not in media_exts:
            continue
        # Extract parent directory from the file path
        parent = str(Path(meta.file_path).parent) if "/" in meta.file_path or "\\" in meta.file_path else ""
        key = (parent, _normalise_stem(Path(meta.file_path).stem))
        groups[key].append(meta)

    result: dict[str, str] = {}
    for key, members in groups.items():
        exts = {(m.extension or "").lower() for m in members}
        has_image = bool(exts & IMAGE_EXTENSIONS)
        has_video = bool(exts & VIDEO_EXTENSIONS)
        if has_image and has_video:
            group_id = str(uuid.uuid4())
            for m in members:
                result[m.file_path] = group_id
    return result


# ---------------------------------------------------------------------------
# Chronological sort key
# ---------------------------------------------------------------------------
def _sort_key(meta: FileMetadata) -> datetime:
    """
    Return the best available timestamp for chronological ordering.

    Uses the shared resolve_item_date() fallback chain with sanity checks.
    Falls back to the Unix epoch if no sane timestamp is available.
    """
    EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
    date, _source = resolve_item_date(
        date_taken=meta.date_taken,
        date_modified=meta.date_modified,
    )
    return date or EPOCH


# ---------------------------------------------------------------------------
# iOS device scanner
# ---------------------------------------------------------------------------
async def _scan_ios_device(
    serial: str,
    afc_path: str,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
) -> list[int]:
    """
    Walk an iOS device's DCIM directory via AFC, collecting media files.

    If *cutoff_datetime* is provided, files with mtime at or before the
    cutoff are skipped entirely — they don't enter metadata extraction,
    hashing, or the database. This is a performance optimization layered
    on top of hash-based duplicate detection, never a replacement.

    Parameters
    ----------
    serial : str
        Device UDID/serial number.
    afc_path : str
        Absolute path on the device (e.g. "/DCIM").
    session_id : int | None
        Optional session FK to attach to newly created rows.
    on_progress : callback | None
        ``callback(processed, total, current_file)`` invoked per file.
    cutoff_datetime : datetime | None
        If provided, files with mtime <= this datetime (already adjusted
        for safety overlap) are skipped.

    Returns
    -------
    list[int]
        Ordered list of ``media_items.id`` values (oldest -> newest).
    """
    # 1. Recursively collect all media files from the device
    logger.info(
        "Scanning iOS device %s at %s (cutoff=%s)",
        serial, afc_path,
        cutoff_datetime.isoformat() if cutoff_datetime else "none",
    )

    device_files = await _walk_ios_directory(serial, afc_path)
    logger.info("Found %d files on device %s under %s", len(device_files), serial, afc_path)

    # 2. Filter by media extension and apply cutoff
    entries: list[FileMetadata] = []
    skipped_by_cutoff = 0

    for fi in device_files:
        if fi.is_dir:
            continue

        # Check extension
        ext = Path(fi.name).suffix.lower()
        if ext not in ALL_MEDIA_EXTENSIONS:
            continue

        # Apply cutoff filter: skip files with mtime at or before the cutoff
        if cutoff_datetime is not None and fi.mtime > 0:
            file_mtime_dt = _ts_to_datetime(fi.mtime)
            if file_mtime_dt is not None and file_mtime_dt <= cutoff_datetime:
                skipped_by_cutoff += 1
                continue

        # Build the ios:// source path
        source_path = f"{IOS_SOURCE_PREFIX}{serial}{fi.path}"

        # Build FileMetadata from AFC stat info
        file_mtime_dt = _ts_to_datetime(fi.mtime) if fi.mtime > 0 else None

        meta = FileMetadata(
            file_path=source_path,
            file_name=fi.name,
            file_size=fi.size,
            extension=ext,
            date_created=file_mtime_dt,
            date_modified=file_mtime_dt,
        )
        entries.append(meta)

    if skipped_by_cutoff > 0:
        logger.info(
            "Skipped %d files at or before cutoff from device %s",
            skipped_by_cutoff, serial,
        )

    total = len(entries)
    if total == 0:
        logger.info("No media files to process from device %s after cutoff filtering", serial)
        return []

    logger.info("Processing %d media files from device %s", total, serial)

    # 3. Detect Live Photo groups
    lp_groups = _detect_live_photo_groups_from_entries(entries)

    # 4. Sort chronologically (oldest first)
    entries.sort(key=_sort_key)
    logger.info("Entries sorted chronologically (oldest -> newest).")

    # 5. Insert / upsert into database
    inserted_ids: list[int] = []

    async with session_scope() as session:
        for idx, meta in enumerate(entries):
            lp_id = lp_groups.get(meta.file_path)
            row_id = await _upsert_media_item(
                session,
                meta=meta,
                live_photo_group=lp_id,
                session_id=session_id,
            )
            inserted_ids.append(row_id)
            if on_progress is not None:
                on_progress(idx + 1, total, meta.file_path)

    logger.info("iOS scan complete — %d items persisted.", len(inserted_ids))
    return inserted_ids


async def _walk_ios_directory(
    serial: str, path: str
) -> list:
    """
    Recursively walk an iOS device directory via AFC.

    Returns a flat list of DeviceFileInfo for all files found.
    """
    from backend.ios_device import DeviceFileInfo

    all_files: list[DeviceFileInfo] = []
    try:
        entries = await browse_device_directory(serial, path)
    except Exception as exc:
        logger.warning("Failed to browse %s on device %s: %s", path, serial, exc)
        return all_files

    for entry in entries:
        if entry.is_dir:
            # Recurse into subdirectories (skip "." and "..")
            if entry.name in (".", ".."):
                continue
            sub_path = f"{path.rstrip('/')}/{entry.name}"
            sub_files = await _walk_ios_directory(serial, sub_path)
            all_files.extend(sub_files)
        else:
            all_files.append(entry)

    return all_files


# ---------------------------------------------------------------------------
# Local filesystem scanner (existing behavior)
# ---------------------------------------------------------------------------
async def _scan_local_files(
    source: Path,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
) -> list[int]:
    """
    Walk a local directory, extract metadata, detect Live Photo pairs,
    sort chronologically, and insert/update media_items rows.

    This is the original scan logic, refactored into its own function.
    """
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
    lp_groups = _detect_live_photo_groups_from_paths(media_files)

    # 3. Extract metadata for every file
    entries: list[tuple[Path, FileMetadata, str | None]] = []
    for idx, fpath in enumerate(media_files):
        try:
            meta = extract_metadata(fpath)
        except Exception as exc:
            logger.warning("Failed to extract metadata for %s: %s", fpath, exc)
            stat = fpath.stat()
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

    # 6. Pre-generate thumbnails for local files (off the transfer critical
    #    path).  The original source file is already on disk, so there's no
    #    reason to wait for any copy hop.  Run in a background thread so the
    #    scan itself isn't blocked.
    if inserted_ids:
        _schedule_local_thumbnails(inserted_ids, entries)

    logger.info("Scan complete — %d items persisted.", len(inserted_ids))
    return inserted_ids


def _schedule_local_thumbnails(
    item_ids: list[int],
    entries: list[tuple[Path, FileMetadata, str | None]],
) -> None:
    """Fire-and-forget thumbnail generation for local source files.

    Runs in a daemon thread so it never blocks the transfer pipeline.
    Stores thumbnails in the in-memory LRU cache (no disk writes).
    """
    import asyncio
    import threading
    from backend.engines.thumbnailer import generate_thumbnail_bytes
    from backend.engines.thumbnail_cache import thumbnail_cache

    id_to_path = {
        row_id: fpath
        for row_id, (fpath, _meta, _lp) in zip(item_ids, entries)
    }

    def _generate_all() -> None:
        import time as _time
        loop = asyncio.new_event_loop()
        try:
            for row_id, fpath in id_to_path.items():
                try:
                    data = generate_thumbnail_bytes(fpath)
                    if data:
                        thumbnail_cache.put(row_id, data)
                        _time.sleep(0.05)
                        loop.run_until_complete(_mark_thumb_ready(row_id))
                except Exception as exc:
                    logger.warning("Pre-scan thumbnail failed for item %d: %s", row_id, exc)
        finally:
            loop.close()

    async def _mark_thumb_ready(item_id: int) -> None:
        from backend.database.manager import session_scope
        from backend.database.models import MediaItem
        async with session_scope() as session:
            db_item = await session.get(MediaItem, item_id)
            if db_item is not None and db_item.thumbnail_path is None:
                db_item.thumbnail_path = "memory"
                db_item.touch()

    t = threading.Thread(target=_generate_all, daemon=True, name="pre-scan-thumbnails")
    t.start()


def _detect_live_photo_groups_from_paths(
    files: list[Path],
) -> dict[str, str]:
    """
    Group local Path files by (parent_dir, normalised stem).
    Returns a mapping ``{ resolved_path_str: live_photo_group_uuid }``.
    """
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
# Main scanner entry point
# ---------------------------------------------------------------------------
async def scan(
    source_path: str | Path,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
) -> list[int]:
    """
    Walk *source_path*, extract metadata, detect Live Photo pairs, sort
    chronologically (oldest first), and insert/update ``media_items`` rows.

    For iOS device sources (``ios://`` prefix), uses AFC-based DCIM browsing.
    For local filesystem sources, uses ``Path.rglob()``.

    Parameters
    ----------
    source_path : str | Path
        Root directory (single file, local path, or ``ios://`` device path)
        to scan.
    session_id : int | None
        Optional session FK to attach to newly created rows.
    on_progress : callback | None
        ``callback(processed, total, current_file)`` invoked per file.
    cutoff_datetime : datetime | None
        If provided (for device sources only), files with mtime at or before
        this datetime are skipped. This is a performance optimization and
        does NOT replace hash-based duplicate detection.

    Returns
    -------
    list[int]
        Ordered list of ``media_items.id`` values (oldest -> newest).
    """
    source_str = str(source_path)

    # --- iOS device source ---
    if is_ios_source(source_str):
        serial, afc_path = parse_ios_source(source_str)
        if is_wpd_device_id(serial):
            logger.error(
                "Scan aborted: ios:// source has a WPD device ID (%s) instead of an "
                "iOS UDID. The device must be selected from the iOS device list, not "
                "the Windows device browser.", serial[:40]
            )
            raise RuntimeError(
                "Device ID looks like a Windows device path, not an iPhone UDID. "
                "Please re-select your iPhone from the iOS device panel."
            )
        return await _scan_ios_device(
            serial,
            afc_path,
            session_id=session_id,
            on_progress=on_progress,
            cutoff_datetime=cutoff_datetime,
        )

    # --- Local filesystem source ---
    source = Path(source_str).resolve()
    return await _scan_local_files(
        source,
        session_id=session_id,
        on_progress=on_progress,
    )


async def scan_from_ref(
    source_ref: SourceRef,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
) -> list[int]:
    """
    Walk a typed SourceRef, extract metadata, detect Live Photo pairs, sort
    chronologically (oldest first), and insert/update ``media_items`` rows.

    This is the preferred entry point for new code. The dispatch to the
    correct backend (local filesystem or device AFC) happens here at a
    single boundary.

    Parameters
    ----------
    source_ref : SourceRef
        Discriminated union: SourceRefLocal or SourceRefDevice.
    session_id : int | None
        Optional session FK to attach to newly created rows.
    on_progress : callback | None
        ``callback(processed, total, current_file)`` invoked per file.
    cutoff_datetime : datetime | None
        If provided (for device sources only), files with mtime at or before
        this datetime are skipped.

    Returns
    -------
    list[int]
        Ordered list of ``media_items.id`` values (oldest -> newest).
    """
    if isinstance(source_ref, SourceRefLocal):
        source = Path(source_ref.path).resolve()
        return await _scan_local_files(
            source,
            session_id=session_id,
            on_progress=on_progress,
        )
    elif isinstance(source_ref, SourceRefDevice):
        if is_wpd_device_id(source_ref.device_id):
            logger.error(
                "Scan aborted: ios:// source has a WPD device ID (%s) instead of an "
                "iOS UDID. The device must be selected from the iOS device list, not "
                "the Windows device browser.", source_ref.device_id[:40]
            )
            raise RuntimeError(
                "Device ID looks like a Windows device path, not an iPhone UDID. "
                "Please re-select your iPhone from the iOS device panel."
            )
        return await _scan_ios_device(
            source_ref.device_id,
            source_ref.device_path,
            session_id=session_id,
            on_progress=on_progress,
            cutoff_datetime=cutoff_datetime,
        )
    else:
        raise ValueError(f"Unknown source ref type: {type(source_ref)}")


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

    Resolves the item's date using the shared fallback chain and stores it
    along with provenance (date_source).
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
            # Reuse existing row — but update session_id so the item is
            # associated with the *current* session, not a stale one from
            # an earlier scan that happened to share the same source_path.
            if session_id is not None:
                stmt = (
                    update(MediaItem)
                    .where(MediaItem.id == existing_id)
                    .values(session_id=session_id)
                )
                await session.execute(stmt)
            logger.debug("Reusing existing row id=%d for %s", existing_id, meta.file_path)
            return existing_id

        # Existing item with final_status=FAILED — reset it for retry
        # rather than trying to INSERT a duplicate (which would violate
        # the UNIQUE constraint on source_path).
        resolved_date, date_source = resolve_item_date(
            date_taken=meta.date_taken,
            date_modified=meta.date_modified,
        )
        stmt = (
            update(MediaItem)
            .where(MediaItem.id == existing_id)
            .values(
                hop1_status=HopStatus.SCANNED.value,
                hop2_status=HopStatus.PENDING.value,
                final_status=HopStatus.PENDING.value,
                session_id=session_id,
                error_message=None,
                retry_count=0,
                live_photo_group=live_photo_group,
                date_taken=resolved_date,
                date_source=date_source,
            )
        )
        await session.execute(stmt)
        logger.debug("Resetting failed row id=%d for retry: %s", existing_id, meta.file_path)
        return existing_id

    # Resolve date using shared fallback chain
    resolved_date, date_source = resolve_item_date(
        date_taken=meta.date_taken,
        date_modified=meta.date_modified,
    )

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
        date_taken=resolved_date,
        date_source=date_source,
    )
    session.add(item)
    await session.flush()

    return item.id  # type: ignore[return-value]
