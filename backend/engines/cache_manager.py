"""
Transfera v2 — Cache Manager (Hop 1: Source -> PC Local Cache)
Stream-by-stream copy with simultaneous BLAKE3 hash computation.
Writes .partial first; renames on verified hash match.
Supports both local filesystem and iOS device sources.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from asyncio import Event
from typing import Awaitable, Callable, Optional

import aiofiles

from backend.config import BATCH_SIZE, CACHE_DIR, PARTIAL_SUFFIX
from backend.database.manager import session_scope
from backend.database.models import BatchStatus, HopStatus, MediaItem, TransferBatch
from backend.engines.batch_manager import get_batch_items, mark_batch_status
from backend.ios_device import is_ios_source, parse_ios_source

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
# Streaming copy + hash
# ---------------------------------------------------------------------------
async def _copy_and_hash(
    src,
    dst: Path,
    *,
    chunk_size: int = BATCH_SIZE * 1024,
    on_progress: ProgressCallback = None,
    file_index: int = 0,
    file_total: int = 0,
) -> str:
    """
    Copy *src* to *dst* reading in ``chunk_size`` buffers while computing
    a BLAKE3 (or SHA-256 fallback) hash of the *source* data.

    The hash is computed from the read buffer, not re-read from disk,
    so the copy and hash happen in a single pass.

    Parameters
    ----------
    src : Path | Any
        Source file path or an async reader with ``read(n)`` and a ``size`` property.

    Returns the hex digest of the source file.
    """
    if isinstance(src, Path):
        file_size = src.stat().st_size
        src_context = aiofiles.open(src, "rb")
    else:
        # iOS device file via AFCFileReader
        file_size = src.size
        src_context = _afc_reader_context(src)

    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()

    bytes_read = 0
    async with src_context as src_fh, aiofiles.open(dst, "wb") as dst_fh:
        while True:
            chunk = await src_fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            await dst_fh.write(chunk)
            bytes_read += len(chunk)
            if on_progress is not None:
                on_progress(bytes_read, file_size, str(src))

    return hasher.hexdigest()


class _afc_reader_context:
    """Async context manager wrapper for a reader with ``open()``/``close()``."""
    def __init__(self, reader):
        self._reader = reader

    async def __aenter__(self):
        await self._reader.open()
        return self._reader

    async def __aexit__(self, *args):
        await self._reader.close()


# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------
def _cache_path_for(cache_dir: Path, source_path: Path) -> Path:
    """Deterministic cache path: cache_dir / <source_hash_prefix> / filename."""
    # Use first 2 chars of the source path hash as a sharding prefix
    prefix = hashlib.md5(str(source_path).encode()).hexdigest()[:2]
    return cache_dir / prefix / source_path.name


def _partial_path(clean: Path) -> Path:
    """Return the .partial variant of a cache path."""
    return clean.with_suffix(clean.suffix + PARTIAL_SUFFIX)


# ---------------------------------------------------------------------------
# Main hop-1 entry point
# ---------------------------------------------------------------------------
async def cache_batch(
    batch_id: int,
    *,
    cache_dir: Path = CACHE_DIR,
    on_progress: ProgressCallback = None,
    on_file_progress: FileProgressCallback = None,
    cancel_event: Event | None = None,
) -> int:
    """
    Process one batch through Hop 1 (source -> local cache).

    For each ``MediaItem`` in the batch:
    1. If a clean cached file exists whose hash matches ``source_hash``, skip.
    2. Otherwise stream-copy source -> ``.partial`` while computing the hash.
    3. On hash match, atomically rename ``.partial`` -> clean path.
    4. On mismatch, delete the ``.partial`` and mark the item FAILED.

    ``on_file_progress(processed, total, file_name)`` is called after each
    file completes so the caller can emit real-time WS progress events.

    Returns the number of successfully cached items.
    """
    items = await get_batch_items(batch_id)
    if not items:
        logger.warning("Batch %d has no items — skipping", batch_id)
        return 0

    await mark_batch_status(batch_id, BatchStatus.LOADING)

    cached_count = 0
    total = len(items)

    for idx, item in enumerate(items):
        if cancel_event is not None and cancel_event.is_set():
            logger.info("Batch %d cancelled at item %d/%d", batch_id, idx + 1, total)
            break

        try:
            success = await _cache_single_item(
                item,
                cache_dir=cache_dir,
                on_progress=on_progress,
                file_index=idx,
                file_total=total,
            )
            if success:
                cached_count += 1
        except Exception as exc:
            logger.error("Cache failed for item %d (%s): %s", item.id, item.source_path, exc)
            await _mark_item_hop1(item, HopStatus.FAILED, str(exc))

        if on_file_progress is not None:
            await on_file_progress(idx + 1, total, item.file_name, item.id)

    async with session_scope() as session:
        db_batch = await session.get(TransferBatch, batch_id)
        if db_batch is not None:
            db_batch.completed_items = cached_count
            db_batch.failed_items = total - cached_count
            if cached_count == 0:
                db_batch.status = BatchStatus.FAILED.value
            elif cached_count < total:
                db_batch.status = BatchStatus.PARTIAL.value
            else:
                db_batch.status = BatchStatus.COMPLETED.value
            db_batch.touch()

    logger.info("Batch %d cached: %d/%d succeeded", batch_id, cached_count, total)
    return cached_count



# ---------------------------------------------------------------------------
# Single-item cache logic
# ---------------------------------------------------------------------------
async def _cache_single_item(
    item: MediaItem,
    *,
    cache_dir: Path,
    on_progress: ProgressCallback,
    file_index: int,
    file_total: int,
) -> bool:
    """
    Cache a single media item. Returns True on success.

    - Skips if clean cache already matches source_hash.
    - Writes .partial, verifies hash, renames on match.
    - Deletes .partial on mismatch or crash residue.
    - Supports both local files and iOS device sources.
    """
    source_path = item.source_path
    is_ios = is_ios_source(source_path)

    if is_ios:
        serial, afc_path = parse_ios_source(source_path)
        src_filename = afc_path.rsplit("/", 1)[-1] if "/" in afc_path else afc_path
        cache_path_prefix = hashlib.md5(source_path.encode()).hexdigest()[:2]
        dst = cache_dir / cache_path_prefix / src_filename
        partial = _partial_path(dst)
        partial.unlink(missing_ok=True)

        # Skip if clean file already matches source_hash
        if dst.is_file() and item.source_hash:
            if _verify_cached_hash(dst, item.source_hash):
                logger.debug("Cache hit (hash match): %s", dst.name)
                await _mark_item_hop1(item, HopStatus.COMPLETED)
                return True
            else:
                logger.info("Cache hash mismatch for %s — re-caching", dst.name)
                dst.unlink(missing_ok=True)

        # Stream from iOS device + simultaneous hash
        dst.parent.mkdir(parents=True, exist_ok=True)
        from backend.device_backend import get_device_backend_manager
        backend_mgr = get_device_backend_manager()
        file_reader = backend_mgr.create_file_reader(serial, afc_path)
        try:
            computed_hash = await _copy_and_hash(
                file_reader, partial,
                on_progress=on_progress,
                file_index=file_index,
                file_total=file_total,
            )
        except Exception as exc:
            logger.error("iOS device read failed for %s: %s", source_path, exc)
            partial.unlink(missing_ok=True)
            await _mark_item_hop1(item, HopStatus.FAILED, f"iOS device read failed: {exc}")
            return False
    else:
        # Local file path
        src = Path(source_path).resolve()
        if not src.is_file():
            logger.warning("Source missing: %s — skipping item %d", src, item.id)
            await _mark_item_hop1(item, HopStatus.FAILED, f"Source missing: {src}")
            return False

        dst = _cache_path_for(cache_dir, src)
        partial = _partial_path(dst)
        partial.unlink(missing_ok=True)

        # Skip if clean file already matches source_hash
        if dst.is_file() and item.source_hash:
            if _verify_cached_hash(dst, item.source_hash):
                logger.debug("Cache hit (hash match): %s", dst.name)
                await _mark_item_hop1(item, HopStatus.COMPLETED)
                return True
            else:
                logger.info("Cache hash mismatch for %s — re-caching", dst.name)
                dst.unlink(missing_ok=True)

        # Stream-copy + simultaneous hash
        dst.parent.mkdir(parents=True, exist_ok=True)
        computed_hash = await _copy_and_hash(
            src, partial,
            on_progress=on_progress,
            file_index=file_index,
            file_total=file_total,
        )

    # --- Verify hash against recorded source_hash ---
    if item.source_hash and computed_hash != item.source_hash.lower():
        logger.warning(
            "Hash mismatch for %s: expected %s, got %s",
            source_path, item.source_hash, computed_hash,
        )
        partial.unlink(missing_ok=True)
        await _mark_item_hop1(item, HopStatus.FAILED, "Source hash mismatch")
        return False

    # --- Hash match (or no prior hash) — commit ---
    import os
    os.replace(str(partial), str(dst))

    # Store the computed hash for downstream verification
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item.id)
        if db_item is not None:
            db_item.source_hash = computed_hash
            db_item.touch()

    await _mark_item_hop1(item, HopStatus.COMPLETED)

    # Schedule thumbnail generation from the cached copy in a background
    # thread so it doesn't slow down the Hop 1 copy loop.  This is the
    # earliest realistic point for device-sourced items (the file was
    # remote until now).
    _schedule_hop1_thumbnail(item.id, dst)

    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _schedule_hop1_thumbnail(item_id: int, cached_path: Path) -> None:
    """Fire-and-forget thumbnail generation from a freshly-cached file.

    Runs in a daemon thread so it never blocks the Hop 1 copy loop.
    Stores result in the in-memory LRU cache (no disk write).
    """
    import threading
    from backend.engines.thumbnailer import generate_thumbnail_bytes
    from backend.engines.thumbnail_cache import thumbnail_cache

    def _generate() -> None:
        try:
            data = generate_thumbnail_bytes(cached_path)
            if data:
                thumbnail_cache.put(item_id, data)
                import asyncio
                import time as _time
                _time.sleep(0.05)
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_mark_thumbnail_ready(item_id))
                finally:
                    loop.close()
        except Exception as exc:
            logger.warning("Hop-1 thumbnail failed for item %d: %s", item_id, exc)

    t = threading.Thread(target=_generate, daemon=True, name=f"thumb-h1-{item_id}")
    t.start()


async def _mark_thumbnail_ready(item_id: int) -> None:
    """Set thumbnail_path sentinel so frontend knows the thumbnail is in cache."""
    from backend.database.manager import session_scope
    from backend.database.models import MediaItem
    async with session_scope() as session:
        item = await session.get(MediaItem, item_id)
        if item is not None:
            item.thumbnail_path = "memory"  # sentinel: available in cache
            item.touch()


def _verify_cached_hash(file_path: Path, expected: str) -> bool:
    """Synchronously verify a cached file's hash against an expected digest."""
    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(BATCH_SIZE * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == expected.lower()


async def _mark_item_hop1(
    item: MediaItem,
    status: HopStatus,
    error: str | None = None,
) -> None:
    """Update a single item's hop1_status."""
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item.id)
        if db_item is not None:
            db_item.hop1_status = status.value
            if error is not None:
                db_item.error_message = error
            db_item.touch()
