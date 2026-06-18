"""
Transfera v2 — Cache Manager (Hop 1: Source -> PC Local Cache)
Stream-by-stream copy with simultaneous BLAKE3 hash computation.
Writes .partial first; renames on verified hash match.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Callable, Optional

import aiofiles

from backend.config import BATCH_SIZE, CACHE_DIR, PARTIAL_SUFFIX
from backend.database.manager import session_scope
from backend.database.models import BatchStatus, HopStatus, MediaItem, TransferBatch
from backend.engines.batch_manager import get_batch_items, mark_batch_status

logger = logging.getLogger(__name__)

# BLAKE3 import with fallback
_BLAKE3_AVAILABLE = False
try:
    import blake3 as _blake3

    _BLAKE3_AVAILABLE = True
except ImportError:
    pass

ProgressCallback = Optional[Callable[[int, int, str], None]]


# ---------------------------------------------------------------------------
# Streaming copy + hash
# ---------------------------------------------------------------------------
async def _copy_and_hash(
    src: Path,
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

    Returns the hex digest of the source file.
    """
    file_size = src.stat().st_size

    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()

    bytes_read = 0
    async with aiofiles.open(src, "rb") as src_fh, aiofiles.open(dst, "wb") as dst_fh:
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
) -> int:
    """
    Process one batch through Hop 1 (source -> local cache).

    For each ``MediaItem`` in the batch:
    1. If a clean cached file exists whose hash matches ``source_hash``, skip.
    2. Otherwise stream-copy source -> ``.partial`` while computing the hash.
    3. On hash match, atomically rename ``.partial`` -> clean path.
    4. On mismatch, delete the ``.partial`` and mark the item FAILED.

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

    await mark_batch_status(batch_id, BatchStatus.COMPLETED)
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
    """
    src = Path(item.source_path).resolve()
    if not src.is_file():
        logger.warning("Source missing: %s — skipping item %d", src, item.id)
        await _mark_item_hop1(item, HopStatus.FAILED, f"Source missing: {src}")
        return False

    dst = _cache_path_for(cache_dir, src)
    partial = _partial_path(dst)

    # --- Resume cleanup: delete stale .partial ---
    partial.unlink(missing_ok=True)

    # --- Skip if clean file already matches ---
    if dst.is_file() and item.source_hash:
        if _verify_cached_hash(dst, item.source_hash):
            logger.debug("Cache hit (hash match): %s", dst.name)
            await _mark_item_hop1(item, HopStatus.COMPLETED)
            return True
        else:
            logger.info("Cache hash mismatch for %s — re-caching", dst.name)
            dst.unlink(missing_ok=True)

    # --- Stream-copy + simultaneous hash ---
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
            src.name, item.source_hash, computed_hash,
        )
        partial.unlink(missing_ok=True)
        await _mark_item_hop1(item, HopStatus.FAILED, "Source hash mismatch")
        return False

    # --- Hash match (or no prior hash) — commit ---
    partial.rename(dst)

    # Store the computed hash for downstream verification
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item.id)
        if db_item is not None:
            db_item.source_hash = computed_hash
            db_item.touch()

    await _mark_item_hop1(item, HopStatus.COMPLETED)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
