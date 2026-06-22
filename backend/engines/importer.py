"""
Transfera v2 — Importer (Hop 2: PC Cache -> Organised Destination)
Copies cached files to the destination archive, verifies against cache_hash,
and handles Move Mode (unlink source after verified commit).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from asyncio import Event
from typing import Awaitable, Callable, Optional

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
from backend.engines.organizer import format_month_folder
from backend.engines.thumbnailer import generate_thumbnail_bytes
from backend.engines.thumbnail_cache import thumbnail_cache
from backend.utils.hashing import hash_file

logger = logging.getLogger(__name__)

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
) -> Path:
    """
    Compute the organised destination path for a media item.

    Layout: ``dest_root / YYYY / MM / filename``
    Falls back to ``dest_root / _unsorted / filename`` if no timestamp is
    available.
    """
    ext = item.extension or ""
    name = item.file_name

    # Attempt to derive date from created_at or source_path
    dt = _derive_date(item)
    if dt is not None:
        return dest_root / str(dt.year) / format_month_folder(dt) / name
    return dest_root / "_unsorted" / name


def _derive_date(item: MediaItem) -> datetime | None:
    """Return the resolved date for the item, falling back to None (unsorted)."""
    return item.date_taken


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
) -> bool:
    """
    Import a single cached item to the destination archive.

    1. Locate the cached file (Hop 1 output).
    2. Compute the archive path via the organiser.
    3. Copy cache -> .partial -> rename on hash match against cache_hash.
    4. In Move Mode, unlink the source path ONLY after successful DB commit.
    """
    # 1. Resolve cached file path
    src = Path(item.source_path).resolve()
    cache_file = _find_cached_file(cache_dir, src, item)
    if cache_file is None or not cache_file.is_file():
        logger.warning("Cache file missing for item %d: %s", item.id, src.name)
        await _mark_item_hop2(item, HopStatus.FAILED, "Cache file missing")
        return False

    # 2. Compute destination
    dst = compute_archive_path(dest_root, item)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Skip if destination already matches cache_hash
    if dst.is_file() and item.source_hash:
        if _verify_file_hash(dst, item.source_hash):
            logger.debug("Destination already verified: %s", dst.name)
            await _mark_item_hop2(item, HopStatus.COMPLETED)
            await _cleanup_cache_file(cache_dir, item)
            if move_mode:
                await _unlink_source(item)
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

    # 3. Copy cache -> destination with hash verification
    try:
        computed_hash = await _copy_cache_to_dest(cache_file, dst)
    except Exception as exc:
        logger.error("Import failed for item %d: %s", item.id, exc)
        _cleanup_partial(dst)
        await _mark_item_hop2(item, HopStatus.FAILED, str(exc))
        return False

    # Verify against cache_hash (source_hash computed during Hop 1)
    if item.source_hash and computed_hash != item.source_hash.lower():
        logger.warning(
            "Import hash mismatch for %s: expected %s, got %s",
            dst.name, item.source_hash, computed_hash,
        )
        dst.unlink(missing_ok=True)
        await _mark_item_hop2(item, HopStatus.FAILED, "Import hash mismatch")
        return False

    # 4. Post-copy verification: re-read destination from disk and compare
    if item.source_hash:
        try:
            dest_hash = hash_file(dst)
        except Exception as exc:
            logger.error("Post-copy verification read failed for %s: %s", dst.name, exc)
            dst.unlink(missing_ok=True)
            await _mark_item_hop2(item, HopStatus.FAILED, f"Post-copy verification read error: {exc}")
            return False

        if dest_hash != item.source_hash.lower():
            logger.error(
                "POST-COPY VERIFICATION FAILED for %s: "
                "destination hash %s does not match expected %s "
                "(copy hash was %s)",
                dst.name, dest_hash, item.source_hash, computed_hash,
            )
            dst.unlink(missing_ok=True)
            # In move mode, do NOT delete the source — it's the only intact copy
            await _mark_item_hop2(
                item, HopStatus.FAILED,
                f"Post-copy verification failed: destination hash {dest_hash[:16]}… "
                f"does not match expected {item.source_hash[:16]}…",
            )
            return False

    # 5. Success — mark completed, clean up cache, optionally unlink source
    await _mark_item_hop2(item, HopStatus.COMPLETED)
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
        on_progress(file_index + 1, file_total, str(src))
    return True


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

    imported = 0
    total = len(items)

    for idx, item in enumerate(items):
        if cancel_event is not None and cancel_event.is_set():
            logger.info("Batch %d cancelled at item %d/%d", batch_id, idx + 1, total)
            break

        try:
            success = await _import_single_item(
                item,
                dest_root=dest_root,
                cache_dir=cache_dir,
                move_mode=move_mode,
                on_progress=on_progress,
                file_index=idx,
                file_total=total,
            )
            if success:
                imported += 1
        except Exception as exc:
            logger.error("Import failed for item %d (%s): %s", item.id, item.source_path, exc)
            await _mark_item_hop2(item, HopStatus.FAILED, str(exc))

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
    src = Path(item.source_path).resolve()
    prefix = hashlib.md5(str(src).encode()).hexdigest()[:2]
    cache_file = cache_dir / prefix / item.file_name
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
    source_path: Path,
    item: MediaItem,
) -> Path | None:
    """Locate the cached file for a given source path."""
    prefix = hashlib.md5(str(source_path).encode()).hexdigest()[:2]
    candidate = cache_dir / prefix / item.file_name
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
) -> None:
    """Update a single item's hop2_status and final_status."""
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item.id)
        if db_item is not None:
            db_item.hop2_status = status.value
            db_item.final_status = status.value
            if error is not None:
                db_item.error_message = error
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
        src = Path(item.source_path).resolve()
        prefix = hashlib.md5(str(src).encode()).hexdigest()[:2]
        cache_file = cache_dir / prefix / item.file_name

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
