"""
Transfera v2 — Importer (Hop 2: PC Cache -> Organised Destination)
Copies cached files to the destination archive, verifies against cache_hash,
and handles Move Mode (unlink source after verified commit).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time as time_module
from asyncio import Event
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles

from backend.config import BATCH_SIZE, PARTIAL_SUFFIX
from backend.database.manager import increment_session_counter, session_scope
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
from backend.engines.thumbnailer import generate_thumbnail_bytes
from backend.utils.hashing import hash_file

logger = logging.getLogger(__name__)

MAX_IMMEDIATE_RETRIES = 2

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
    capture_dt = extract_capture_datetime(cache_file)

    # 3. Compute destination
    dst = compute_archive_path(dest_root, item, layout=folder_layout)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Skip if destination already matches cache_hash
    if dst.is_file() and item.source_hash:
        if _verify_file_hash(dst, item.source_hash):
            logger.debug("Destination already verified: %s", dst.name)
            await _mark_item_hop2(item, HopStatus.COMPLETED, capture_dt=capture_dt)
            await _cleanup_cache_file(cache_dir, item)
            if move_mode:
                await _unlink_source(item)
            # Restore mtime on verified destination
            _restore_mtime(dst, capture_dt)
            # Generate thumbnail for verified items too
            try:
                data = generate_thumbnail_bytes(dst)
                if data:
                    thumbnail_cache.put(item.id, data)
                    await _mark_thumbnail_ready(item.id)
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
    _restore_mtime(dst, capture_dt)
    await _mark_item_hop2(item, HopStatus.COMPLETED, capture_dt=capture_dt)
    await _cleanup_cache_file(cache_dir, item)
    if move_mode:
        await _unlink_source(item)

    # Generate thumbnail asynchronously (best-effort, non-blocking)
    try:
        data = generate_thumbnail_bytes(dst)
        if data:
            thumbnail_cache.put(item.id, data)
            await _mark_thumbnail_ready(item.id)
    except Exception as exc:
        logger.warning("Thumbnail generation skipped for item %d: %s", item.id, exc)

    if on_progress is not None:
        on_progress(file_index + 1, file_total, item.source_path)
    return True


def _restore_mtime(dst: Path, capture_dt: datetime) -> None:
    """Set the destination file's mtime/atime to match the capture time."""
    try:
        ts = capture_dt.timestamp()
        os.utime(dst, (ts, ts))
    except Exception as exc:
        logger.warning("Failed to restore mtime on %s: %s", dst, exc)


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
    if session_id is not None:
        async with session_scope() as db_session:
            ts_obj = await db_session.get(TransferSession, session_id)
            if ts_obj is not None:
                folder_layout = ts_obj.folder_layout

    imported = 0
    total = len(items)

    for idx, item in enumerate(items):
        if cancel_event is not None and cancel_event.is_set():
            logger.info("Batch %d cancelled at item %d/%d", batch_id, idx + 1, total)
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
                await increment_session_counter(session_id, "imported_files", 1)
                # Update speed samples
                async with session_scope() as db_session:
                    ts_obj = await db_session.get(TransferSession, session_id)
                    if ts_obj is not None:
                        samples = []
                        if ts_obj.speed_samples:
                            try:
                                samples = json.loads(ts_obj.speed_samples)
                            except json.JSONDecodeError:
                                samples = []
                        samples.append({
                            "ts": time_module.time(),
                            "count": ts_obj.imported_files,
                        })
                        if len(samples) > 20:
                            samples = samples[-20:]
                        ts_obj.speed_samples = json.dumps(samples)
                        ts_obj.touch()
            else:
                await increment_session_counter(session_id, "failed_files", 1)

        if on_file_progress is not None:
            await on_file_progress(idx + 1, total, item.file_name, item.id)

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
    """
    src = Path(item.source_path).resolve()
    try:
        src.unlink(missing_ok=True)
        logger.debug("Source unlinked (move mode): %s", src)
    except OSError as exc:
        logger.warning("Failed to unlink source %s: %s", src, exc)


async def _cleanup_cache_file(
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
    try:
        cache_file.unlink(missing_ok=True)
        if not cache_file.exists():
            logger.debug("Cache file removed (Hop 2 confirmed): %s", cache_file)
    except OSError as exc:
        logger.warning("Failed to remove cache file %s: %s", cache_file, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_cached_file(
    cache_dir: Path,
    item: MediaItem,
) -> Path | None:
    """Locate the cached file for a given source path."""
    candidate = get_cache_path(cache_dir, item.source_path, item.file_name)
    return candidate if candidate.is_file() else None


def _verify_file_hash(file_path: Path, expected: str) -> bool:
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


async def _set_item_thumbnail(item_id: int, thumbnail_path: str) -> None:
    """Update a single item's thumbnail_path in the database."""
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
        if db_item is not None:
            db_item.thumbnail_path = thumbnail_path
            db_item.touch()


async def _mark_thumbnail_ready(item_id: int) -> None:
    """Set thumbnail_path sentinel so frontend knows the thumbnail is in cache."""
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
        if db_item is not None:
            db_item.thumbnail_path = "memory"
            db_item.thumbnail_status = "ready"
            db_item.touch()


async def _mark_thumbnail_failed(item_id: int) -> None:
    """Mark a media item's thumbnail as failed so the frontend stops retrying."""
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
        if db_item is not None:
            db_item.thumbnail_status = "failed"
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
