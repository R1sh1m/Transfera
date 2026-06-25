"""
Transfera v2 — Importer (Hop 2: PC Cache -> Organised Destination)
Copies cached files to the destination archive, verifies against cache_hash,
and handles Move Mode (unlink source after verified commit).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time as time_module
from asyncio import Event
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import aiofiles

from backend.config import BATCH_SIZE, PARTIAL_SUFFIX
from backend.database.manager import session_scope
from backend.database.models import (
    BatchStatus,
    HopStatus,
    MediaItem,
    TransferBatch,
    TransferSession,
)
from backend.engines.batch_manager import get_batch_items, mark_batch_status
from backend.engines.cache_manager import get_cache_path
from backend.engines.capture_time import extract_capture_datetime
from backend.engines.organizer import resolve_archive_path
from backend.engines.thumbnail_cache import thumbnail_cache
from backend.engines.thumbnail_ops import mark_thumbnail_ready
from backend.engines.thumbnailer import generate_thumbnail_bytes
from backend.utils.hashing import hash_file

logger = logging.getLogger(__name__)

MAX_IMMEDIATE_RETRIES = 2
COPY_CONCURRENCY = 4  # safe for HDD; increase to 8 for NVMe source

# BLAKE3 import with fallback
_BLAKE3_AVAILABLE = False
try:
    import blake3 as _blake3

    _BLAKE3_AVAILABLE = True
except ImportError:
    pass

ProgressCallback = Optional[Callable[[int, int, str], None]]
FileProgressCallback = Optional[Callable[[int, int, str, int], Awaitable[None]]]


# ---------------------------------------------------------------------------
# Path organisation
# ---------------------------------------------------------------------------
def compute_archive_path(
    dest_root: Path,
    item: MediaItem,
    *,
    layout: str = "year/month",
) -> Path:
    """
    Compute the organised destination path for a media item.

    Delegates to ``resolve_archive_path`` in the organizer module, supporting
    configurable layouts: ``"year/month/day"``, ``"year/month"`` (default),
    and ``"flat"``.
    Falls back to ``dest_root / _unsorted / filename`` if no timestamp is
    available.
    """
    return resolve_archive_path(dest_root, item, layout=layout)


# ---------------------------------------------------------------------------
# Streaming copy + hash (cache -> destination)
# ---------------------------------------------------------------------------
async def _copy_cache_to_dest(
    src: Path,
    dst: Path,
    *,
    chunk_size: int = BATCH_SIZE * 1024,
) -> str:
    """
    Copy *src* to *dst* through a ``.partial`` intermediate while computing
    a BLAKE3 (or SHA-256) hash of the *cached* data.

    Returns the hex digest.
    """
    partial = dst.with_suffix(dst.suffix + PARTIAL_SUFFIX)
    partial.unlink(missing_ok=True)

    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()

    async with aiofiles.open(src, "rb") as src_fh, aiofiles.open(partial, "wb") as dst_fh:
        while True:
            chunk = await src_fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            await dst_fh.write(chunk)

    computed = hasher.hexdigest()
    partial.rename(dst)
    return computed


# ---------------------------------------------------------------------------
# Single-item import
# ---------------------------------------------------------------------------
async def _import_single_item(
    item: MediaItem,
    *,
    dest_root: Path,
    cache_dir: Path,
    move_mode: bool,
    on_progress: ProgressCallback,
    file_index: int,
    file_total: int,
    folder_layout: str = "year/month",
) -> bool:
    """
    Import a single cached item to the destination archive.

    1. Locate the cached file (Hop 1 output).
    2. Extract original capture time from the cached file.
    3. Compute the archive path via the organiser.
    4. Copy cache -> .partial -> rename on hash match against cache_hash.
    5. Restore destination file mtime to match capture time.
    6. In Move Mode, unlink the source path ONLY after successful DB commit.
    """
    # 1. Resolve cached file path
    cache_file = _find_cached_file(cache_dir, item)
    if cache_file is None or not cache_file.is_file():
        logger.warning("Cache file missing for item %d: %s", item.id, item.file_name)
        await _mark_item_hop2(item, HopStatus.FAILED, "Cache file missing")
        return False

    # 2. Extract original capture time from the cached file (pre-copy)
    # For iOS HEIC/MOV files, try ExifTool first for maximum metadata fidelity
    capture_dt = None
    source_created_ts: float | None = None
    ext = cache_file.suffix.lower()

    # Step A: Try ExifTool on cache file for EMBEDDED metadata.
    # Embedded EXIF/metadata is preserved through file copy, so ExifTool
    # results from the cache file are valid for files that have EXIF.
    if ext in {'.heic', '.jpg', '.jpeg', '.png', '.mov', '.mp4', '.m4v', '.3gp'}:
        try:
            from backend.engines.metadata_extractor import extract_metadata_batch
            meta_results = extract_metadata_batch([cache_file])
            meta = meta_results.get(str(cache_file.resolve()))
            if meta and meta.date_taken:
                capture_dt = meta.date_taken
            elif meta and meta.date_created:
                capture_dt = meta.date_created
        except Exception as exc:
            logger.debug("ExifTool batch extraction failed for %s: %s", cache_file.name, exc)

    # Step B: For local source files (not iOS/WPD), read timestamps directly
    # from the SOURCE file. The source file still has its original mtime/ctime.
    # This is the authoritative fallback for files without embedded EXIF
    # (e.g. PNG screenshots, documents) where ExifTool returns no useful date.
    is_local_source = (
        not item.source_path.startswith("ios://")
        and not item.source_path.startswith("wpd://")
        and not item.source_path.startswith("afc://")
    )
    if is_local_source:
        src = Path(item.source_path)
        if src.is_file():
            try:
                src_stat = src.stat()
                # Use source mtime if ExifTool found nothing, OR if date_source
                # indicates the scanner already used file_modified (no EXIF).
                if capture_dt is None or item.date_source in (None, "file_modified"):
                    capture_dt = datetime.fromtimestamp(src_stat.st_mtime, tz=UTC)
                # Always capture the source's ctime for Windows creation time restoration.
                # st_ctime on Windows is the file creation time (unlike Linux where
                # it's the inode change time).
                if sys.platform == "win32":
                    source_created_ts = src_stat.st_ctime
            except OSError as exc:
                logger.debug(
                    "Could not stat source file %s for timestamp recovery: %s",
                    item.source_path, exc,
                )

    # Step C: Last resort — extract from cache file's embedded metadata only
    # (NOT from cache file's filesystem timestamps, which are always wrong).
    if capture_dt is None:
        capture_dt = extract_capture_datetime(cache_file)

    # 2b. For iOS sources with no prior date_taken, update the item's date_taken NOW
    #     so compute_archive_path uses the real EXIF capture date for folder placement.
    if capture_dt is not None and item.date_taken is None:
        from backend.engines.date_resolver import is_date_sane
        if is_date_sane(capture_dt):
            async with session_scope() as session:
                db_item = await session.get(MediaItem, item.id)
                if db_item is not None and db_item.date_taken is None:
                    db_item.date_taken = capture_dt
                    db_item.date_source = "exif"
                    db_item.touch()
            item.date_taken = capture_dt

    # 3. Compute destination (now uses the correct capture date for iOS files)
    dst = compute_archive_path(dest_root, item, layout=folder_layout)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Skip if destination already matches cache_hash
    if dst.is_file() and item.source_hash:
        if verify_file_hash(dst, item.source_hash):
            logger.debug("Destination already verified: %s", dst.name)
            await _mark_item_hop2(item, HopStatus.COMPLETED, capture_dt=capture_dt)
            await cleanup_cache_file(cache_dir, item)
            if move_mode:
                await _unlink_source(item)
            # Restore mtime on verified destination
            _restore_mtime(dst, capture_dt, source_created_ts=source_created_ts)
            # Generate thumbnail for verified items too
            try:
                data = await asyncio.to_thread(generate_thumbnail_bytes, dst)
                if data:
                    thumbnail_cache.put(item.id, data)
                    await mark_thumbnail_ready(item.id)
            except Exception as exc:
                logger.warning("Thumbnail generation skipped (verified dest) for item %d: %s", item.id, exc)
            return True
        else:
            logger.info("Destination hash mismatch for %s — re-importing", dst.name)
            dst.unlink(missing_ok=True)

    # 4-5. Copy cache -> destination with hash verification (with retry on mismatch)
    max_attempts = MAX_IMMEDIATE_RETRIES + 1
    for attempt in range(1, max_attempts + 1):
        try:
            computed_hash = await _copy_cache_to_dest(cache_file, dst)
        except Exception as exc:
            logger.error("Import failed for item %d: %s", item.id, exc)
            _cleanup_partial(dst)
            await _mark_item_hop2(item, HopStatus.FAILED, str(exc))
            return False

        # Verify against cache_hash (source_hash computed during Hop 1)
        if item.source_hash and computed_hash != item.source_hash.lower():
            if attempt < max_attempts:
                logger.warning(
                    "Hash mismatch for %s on attempt %d/%d — retrying",
                    dst.name, attempt, max_attempts,
                )
            else:
                logger.error(
                    "Import hash mismatch for %s after %d attempts: "
                    "expected %s, got %s",
                    dst.name, max_attempts, item.source_hash, computed_hash,
                )
                dst.unlink(missing_ok=True)
                await _mark_item_hop2(item, HopStatus.FAILED, "Import hash mismatch after all retries")
                return False

            dst.unlink(missing_ok=True)
            _cleanup_partial(dst)
            await asyncio.sleep(0.5 * attempt)
            continue

        # Post-copy verification: re-read destination from disk
        if item.source_hash:
            try:
                dest_hash = hash_file(dst)
            except Exception as exc:
                if attempt < max_attempts:
                    logger.warning(
                        "Post-copy read failed for %s on attempt %d/%d — retrying",
                        dst.name, attempt, max_attempts,
                    )
                else:
                    logger.error(
                        "Post-copy verification read failed for %s after %d attempts: %s",
                        dst.name, max_attempts, exc,
                    )
                    dst.unlink(missing_ok=True)
                    await _mark_item_hop2(item, HopStatus.FAILED, f"Post-copy verification read error after all retries: {exc}")
                    return False

                dst.unlink(missing_ok=True)
                _cleanup_partial(dst)
                await asyncio.sleep(0.5 * attempt)
                continue

            if dest_hash != item.source_hash.lower():
                if attempt < max_attempts:
                    logger.warning(
                        "Post-copy hash mismatch for %s on attempt %d/%d — retrying",
                        dst.name, attempt, max_attempts,
                    )
                else:
                    logger.error(
                        "POST-COPY VERIFICATION FAILED for %s after %d attempts: "
                        "destination hash %s does not match expected %s "
                        "(copy hash was %s)",
                        dst.name, max_attempts, dest_hash, item.source_hash, computed_hash,
                    )
                    dst.unlink(missing_ok=True)
                    await _mark_item_hop2(
                        item, HopStatus.FAILED,
                        f"Post-copy verification failed after all retries: "
                        f"destination hash {dest_hash[:16]}… "
                        f"does not match expected {item.source_hash[:16]}…",
                    )
                    return False

                dst.unlink(missing_ok=True)
                _cleanup_partial(dst)
                await asyncio.sleep(0.5 * attempt)
                continue

        # All checks passed
        break

    # 6. Success — restore mtime, mark completed, clean up cache, optionally unlink source
    _restore_mtime(dst, capture_dt, source_created_ts=source_created_ts)
    await _mark_item_hop2(item, HopStatus.COMPLETED, capture_dt=capture_dt)
    await cleanup_cache_file(cache_dir, item)
    if move_mode:
        await _unlink_source(item)

    # Generate thumbnail asynchronously (best-effort, non-blocking)
    try:
        data = await asyncio.to_thread(generate_thumbnail_bytes, dst)
        if data:
            thumbnail_cache.put(item.id, data)
            await mark_thumbnail_ready(item.id)
    except Exception as exc:
        logger.warning("Thumbnail generation skipped for item %d: %s", item.id, exc)

    if on_progress is not None:
        on_progress(file_index + 1, file_total, item.source_path)
    return True


def _restore_mtime(
    dst: Path,
    capture_dt: datetime | None,
    *,
    source_created_ts: float | None = None,
) -> None:
    """Restore timestamps on the destination file.

    capture_dt: the file's original modification/capture time.
    source_created_ts: the source file's st_ctime (Windows creation time),
        as a Unix timestamp float. When provided, this is used for the
        Windows file creation time instead of capture_dt, preserving the
        correct Created/Modified distinction.
    """
    try:
        if capture_dt is None:
            return

        now = datetime.now(UTC)
        if (now - capture_dt).total_seconds() < 60:
            logger.debug(
                "Skipping mtime restore for %s: capture_dt looks like transfer time (%s)",
                dst.name, capture_dt,
            )
            return

        ts = capture_dt.timestamp()
        os.utime(dst, (ts, ts))

        if sys.platform == "win32":
            if source_created_ts is not None:
                # Use the source file's original creation time
                created_dt = datetime.fromtimestamp(source_created_ts, tz=UTC)
                _set_windows_file_creation_time(dst, created_dt)
            else:
                _set_windows_file_creation_time(dst, capture_dt)

    except Exception as exc:
        logger.warning("Failed to restore timestamps on %s: %s", dst, exc)


def _set_windows_file_creation_time(path: Path, capture_dt: datetime) -> None:
    """Set the Windows file creation time (ctime) using Win32 SetFileTime API.

    Windows FILETIME is a 64-bit integer counting 100-nanosecond intervals
    since January 1, 1601. Python's datetime epoch starts at January 1, 1970.
    """
    try:
        import ctypes
        import ctypes.wintypes

        EPOCH_DIFF = 116444736000000000

        ts = capture_dt.timestamp()
        filetime_value = int(ts * 10_000_000) + EPOCH_DIFF

        filetime = ctypes.wintypes.FILETIME(
            filetime_value & 0xFFFFFFFF,
            (filetime_value >> 32) & 0xFFFFFFFF,
        )

        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

        handle = ctypes.windll.kernel32.CreateFileW(
            str(path),
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )

        INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
        if handle == INVALID_HANDLE_VALUE:
            err = ctypes.windll.kernel32.GetLastError()
            logger.debug("CreateFileW failed for %s: error %d", path.name, err)
            return

        try:
            success = ctypes.windll.kernel32.SetFileTime(
                handle,
                ctypes.byref(filetime),
                None,
                None,
            )
            if not success:
                err = ctypes.windll.kernel32.GetLastError()
                logger.debug("SetFileTime failed for %s: error %d", path.name, err)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    except Exception as exc:
        logger.debug("Could not set Windows creation time for %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Batch import
# ---------------------------------------------------------------------------
async def import_batch(
    batch_id: int,
    *,
    dest_root: Path,
    cache_dir: Path,
    move_mode: bool = False,
    on_progress: ProgressCallback = None,
    on_file_progress: FileProgressCallback = None,
    cancel_event: Event | None = None,
    session_id: int | None = None,
) -> int:
    """
    Process one batch through Hop 2 (cache -> organised destination).

    ``on_file_progress(processed, total, file_name)`` is called after each
    file completes so the caller can emit real-time WS progress events.

    Returns the number of successfully imported items.
    """
    items = await get_batch_items(batch_id)
    if not items:
        logger.warning("Batch %d has no items — skipping", batch_id)
        return 0

    await mark_batch_status(batch_id, BatchStatus.ARCHIVED)

    folder_layout: str = "year/month"
    is_local = True
    if session_id is not None:
        async with session_scope() as db_session:
            ts_obj = await db_session.get(TransferSession, session_id)
            if ts_obj is not None:
                folder_layout = ts_obj.folder_layout
                is_local = not (
                    ts_obj.source_root.startswith("ios://")
                    or ts_obj.source_root.startswith("wpd://")
                )

    # For local sources, use parallel copy with batched DB writes.
    # iOS/WPD sources and move mode stay sequential (AFC driver constraint).
    use_parallel = is_local and not move_mode and COPY_CONCURRENCY > 1

    imported = 0
    total = len(items)
    imported_delta = 0
    failed_delta = 0
    completed_ids: list[int] = []

    SPEED_SAMPLE_MIN_INTERVAL = 2.0
    SPEED_SAMPLE_MAX_FILES = 5
    last_speed_sample_time: float = 0.0

    if use_parallel:
        chunk_size = COPY_CONCURRENCY * 2
        sem = asyncio.Semaphore(COPY_CONCURRENCY)

        async def _import_one(item: MediaItem, idx: int) -> tuple[bool, int]:
            async with sem:
                try:
                    success = await _import_single_item(
                        item,
                        dest_root=dest_root,
                        cache_dir=cache_dir,
                        move_mode=move_mode,
                        on_progress=on_progress,
                        file_index=idx,
                        file_total=total,
                        folder_layout=folder_layout,
                    )
                    return success, item.id
                except Exception as exc:
                    logger.error("Import failed for item %d (%s): %s", item.id, item.source_path, exc)
                    await _mark_item_hop2(item, HopStatus.FAILED, str(exc))
                    return False, item.id

        for chunk_start in range(0, total, chunk_size):
            if cancel_event is not None and cancel_event.is_set():
                logger.info(
                    "Batch %d interrupted at item %d/%d",
                    batch_id, chunk_start, total,
                )
                break

            chunk = items[chunk_start:chunk_start + chunk_size]
            tasks = [_import_one(item, chunk_start + i) for i, item in enumerate(chunk)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, tuple):
                    success, item_id = result
                    if success:
                        imported += 1
                        imported_delta += 1
                        completed_ids.append(item_id)
                    else:
                        failed_delta += 1

            # Flush counter deltas to DB after each chunk
            if session_id is not None and (imported_delta > 0 or failed_delta > 0):
                now = time_module.time()
                async with session_scope() as db_session:
                    ts_obj = await db_session.get(TransferSession, session_id)
                    if ts_obj is not None:
                        ts_obj.imported_files += imported_delta
                        ts_obj.failed_files += failed_delta
                        _maybe_sample_speed(ts_obj, now)
                        ts_obj.touch()
                imported_delta = 0
                failed_delta = 0

            # Bulk progress update per chunk
            if on_file_progress is not None:
                processed = min(chunk_start + len(chunk), total)
                await on_file_progress(processed, total, "", 0)
    else:
        for idx, item in enumerate(items):
            if cancel_event is not None and cancel_event.is_set():
                logger.info(
                    "Batch %d interrupted (pause or cancel) at item %d/%d",
                    batch_id, idx + 1, total,
                )
                break

            success = False
            try:
                success = await _import_single_item(
                    item,
                    dest_root=dest_root,
                    cache_dir=cache_dir,
                    move_mode=move_mode,
                    on_progress=on_progress,
                    file_index=idx,
                    file_total=total,
                    folder_layout=folder_layout,
                )
                if success:
                    imported += 1
            except Exception as exc:
                logger.error("Import failed for item %d (%s): %s", item.id, item.source_path, exc)
                await _mark_item_hop2(item, HopStatus.FAILED, str(exc))

            if session_id is not None:
                if success:
                    imported_delta += 1
                    completed_ids.append(item.id)
                else:
                    failed_delta += 1

                # Flush counter deltas every 5 files or on last file
                if (idx + 1) % 5 == 0 or idx == total - 1:
                    if imported_delta > 0 or failed_delta > 0:
                        now = time_module.time()
                        async with session_scope() as db_session:
                            ts_obj = await db_session.get(TransferSession, session_id)
                            if ts_obj is not None:
                                ts_obj.imported_files += imported_delta
                                ts_obj.failed_files += failed_delta
                                _maybe_sample_speed(ts_obj, now)
                                ts_obj.touch()
                        imported_delta = 0
                        failed_delta = 0

            if on_file_progress is not None:
                await on_file_progress(idx + 1, total, item.file_name, item.id)

    # Bulk-update MediaItem statuses for all completed items
    if completed_ids:
        from sqlalchemy import update
        async with session_scope() as db_session:
            await db_session.execute(
                update(MediaItem)
                .where(MediaItem.id.in_(completed_ids))
                .values(
                    hop2_status=HopStatus.COMPLETED.value,
                    final_status=HopStatus.COMPLETED.value,
                )
            )

    async with session_scope() as session:
        db_batch = await session.get(TransferBatch, batch_id)
        if db_batch is not None:
            db_batch.completed_items = imported
            db_batch.failed_items = total - imported
            if imported == 0:
                db_batch.status = BatchStatus.FAILED.value
            elif imported < total:
                db_batch.status = BatchStatus.PARTIAL.value
            else:
                db_batch.status = BatchStatus.COMPLETED.value
            db_batch.touch()

    logger.info("Batch %d imported: %d/%d succeeded", batch_id, imported, total)
    return imported



# ---------------------------------------------------------------------------
# Move Mode: unlink source after verified commit
# ---------------------------------------------------------------------------
async def _unlink_source(item: MediaItem) -> None:
    """
    Remove the original source file. Called ONLY after Hop 2 verify is
    committed to the DB. Uses ``Path.unlink(missing_ok=True)`` for safety.
    Supports both local filesystem and iOS device sources.
    """
    from backend.ios_device import is_ios_source

    if is_ios_source(item.source_path):
        from backend.ios_device import _get_afc_service, parse_ios_source
        try:
            serial, afc_path = parse_ios_source(item.source_path)
            afc, lockdown = await _get_afc_service(serial)
            try:
                await asyncio.to_thread(afc.rm, afc_path)
                logger.info("iOS source unlinked (move mode): %s", item.source_path)
            finally:
                afc.close()
                lockdown.close()
        except Exception as exc:
            logger.warning("Failed to unlink iOS source %s: %s", item.source_path, exc)
    else:
        src = Path(item.source_path).resolve()
        try:
            src.unlink(missing_ok=True)
            logger.debug("Source unlinked (move mode): %s", src)
        except OSError as exc:
            logger.warning("Failed to unlink source %s: %s", src, exc)



async def cleanup_cache_file(
    cache_dir: Path,
    item: MediaItem,
) -> None:
    """
    Remove the Hop 1 cache file after Hop 2 has confirmed the destination
    copy.  This prevents an unbounded disk-space leak from accumulated cache
    files (every transferred file leaves a permanent copy in the cache
    directory unless explicitly cleaned up here).

    The cache path is computed using the same deterministic scheme as
    ``cache_manager._cache_path_for`` / ``_find_cached_file`` so it always
    matches the file created during Hop 1.

    Safety contract:
    - Call ONLY after the DB commit in ``_mark_item_hop2(COMPLETED)`` has
      succeeded — never before, and never in a failure path.
    - It is safe to call even if the cache file has already been removed
      (e.g. by a concurrent purge or a prior recovery run).
    """
    cache_file = get_cache_path(cache_dir, item.source_path, item.file_name)
    if not cache_file.exists():
        return

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            cache_file.unlink(missing_ok=True)
            if not cache_file.exists():
                logger.debug(
                    "Cache file removed (Hop 2 confirmed): %s", cache_file
                )
            return
        except OSError as exc:
            is_windows_lock = (
                sys.platform == "win32"
                and hasattr(exc, "winerror")
                and exc.winerror == 32  # ERROR_SHARING_VIOLATION
            )
            if is_windows_lock and attempt < max_attempts:
                delay = 0.1 * attempt  # 100ms, then 200ms
                logger.debug(
                    "Cache file locked (attempt %d/%d), retrying in %.0fms: %s",
                    attempt, max_attempts, delay * 1000, cache_file.name,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "Failed to remove cache file %s after %d attempt(s): %s",
                    cache_file, attempt, exc,
                )
                return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _maybe_sample_speed(ts_obj: TransferSession, now: float) -> None:
    """Sample transfer speed if enough time/files have passed."""
    import json as _json
    samples: list[dict] = []
    if ts_obj.speed_samples:
        try:
            samples = _json.loads(ts_obj.speed_samples)
        except _json.JSONDecodeError:
            samples = []
    samples.append({
        "ts": now,
        "count": ts_obj.imported_files,
    })
    if len(samples) > 20:
        samples = samples[-20:]
    ts_obj.speed_samples = _json.dumps(samples)


def _find_cached_file(
    cache_dir: Path,
    item: MediaItem,
) -> Path | None:
    """Locate the cached file for a given source path."""
    candidate = get_cache_path(cache_dir, item.source_path, item.file_name)
    return candidate if candidate.is_file() else None


def verify_file_hash(file_path: Path, expected: str) -> bool:
    """Synchronously verify a file's hash."""
    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(BATCH_SIZE * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == expected.lower()


def _cleanup_partial(path: Path) -> None:
    """Remove a .partial file if it exists."""
    partial = path.with_suffix(path.suffix + PARTIAL_SUFFIX)
    partial.unlink(missing_ok=True)


async def _mark_item_hop2(
    item: MediaItem,
    status: HopStatus,
    error: str | None = None,
    capture_dt: datetime | None = None,
) -> None:
    """Update a single item's hop2_status and final_status."""
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item.id)
        if db_item is not None:
            db_item.hop2_status = status.value
            db_item.final_status = status.value
            if error is not None:
                db_item.error_message = error
            if capture_dt is not None:
                db_item.original_capture_time = capture_dt
            db_item.touch()


# ---------------------------------------------------------------------------
# One-time remediation: purge all Hop 1 cache files for confirmed items
# ---------------------------------------------------------------------------
async def purge_hop1_cache_for_completed_items(
    cache_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    """
    One-time remediation pass: scan all MediaItem records whose
    ``final_status`` is COMPLETED and remove their corresponding Hop 1
    cache file.  This cleans up accumulated cache files from prior
    transfers that were never cleaned up (the disk-space leak this module's
    ``_cleanup_cache_file`` now prevents going forward).

    When *dry_run* is ``True``, only count and log what *would* be removed
    without actually deleting anything.

    Returns the number of cache files removed (or that would be removed).
    """
    from sqlalchemy import select

    removed = 0
    async with session_scope() as session:
        result = await session.execute(
            select(MediaItem).where(MediaItem.final_status == HopStatus.COMPLETED.value)
        )
        items = list(result.scalars().all())

    logger.info(
        "Purge scan: %d completed items found in database",
        len(items),
    )

    for item in items:
        cache_file = get_cache_path(cache_dir, item.source_path, item.file_name)

        if not cache_file.is_file():
            continue

        if dry_run:
            logger.debug("Would remove cache file: %s", cache_file)
            removed += 1
            continue

        try:
            cache_file.unlink()
            removed += 1
            logger.debug("Purged cache file: %s", cache_file)
        except OSError as exc:
            logger.warning("Failed to purge cache file %s: %s", cache_file, exc)

    action = "Would remove" if dry_run else "Removed"
    logger.info("%s %d cache files for completed items", action, removed)
    return removed
