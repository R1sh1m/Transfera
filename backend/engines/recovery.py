"""
Transfera v2 — Crash Recovery
Handles interrupted batches on startup:
  - LOADING (Hop 1 interrupted): delete partials, reset items to PENDING.
  - ARCHIVED (Hop 2 interrupted): verify destination, mark VERIFIED or retry.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import select, update

from backend.config import BATCH_SIZE, CACHE_DIR, PARTIAL_SUFFIX
from backend.database.manager import session_scope
from backend.database.models import (
    BatchStatus,
    HopStatus,
    MediaItem,
    TransferBatch,
    TransferSession,
)
from backend.engines.cache_manager import _cache_path_for, _partial_path

logger = logging.getLogger(__name__)

# BLAKE3 import with fallback
_BLAKE3_AVAILABLE = False
try:
    import blake3 as _blake3

    _BLAKE3_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Public recovery entry point
# ---------------------------------------------------------------------------
async def recover_interrupted_batches(
    *,
    cache_dir: Path = CACHE_DIR,
) -> dict[str, int]:
    """
    Scan all sessions for batches stuck in LOADING or ARCHIVED and recover.

    Returns a summary dict: ``{"loading_recovered": N, "archived_recovered": M}``
    """
    stats = {"loading_recovered": 0, "archived_recovered": 0}

    async with session_scope() as session:
        # Find all stuck batches
        result = await session.execute(
            select(TransferBatch).where(
                TransferBatch.status.in_([
                    BatchStatus.LOADING.value,
                    BatchStatus.ARCHIVED.value,
                ])
            )
        )
        stuck_batches = list(result.scalars().all())

    if not stuck_batches:
        logger.info("No interrupted batches found — clean startup.")
        return stats

    for batch in stuck_batches:
        if batch.status == BatchStatus.LOADING.value:
            await _recover_loading_batch(batch, cache_dir=cache_dir)
            stats["loading_recovered"] += 1
        elif batch.status == BatchStatus.ARCHIVED.value:
            await _recover_archived_batch(batch, cache_dir=cache_dir)
            stats["archived_recovered"] += 1

    logger.info(
        "Crash recovery complete: %d LOADING, %d ARCHIVED batches handled.",
        stats["loading_recovered"],
        stats["archived_recovered"],
    )
    return stats


# ---------------------------------------------------------------------------
# LOADING recovery: delete partials, reset to PENDING
# ---------------------------------------------------------------------------
async def _recover_loading_batch(
    batch: TransferBatch,
    *,
    cache_dir: Path,
) -> None:
    """
    A batch stuck in LOADING means Hop 1 was interrupted mid-copy.

    Action:
    1. Delete all .partial files for items in this batch.
    2. Reset hop1_status back to PENDING for unfinished items.
    3. Mark the batch as PENDING for re-processing.
    """
    logger.info("Recovering LOADING batch %d (session %d)", batch.id, batch.session_id)

    async with session_scope() as session:
        # Get all items in this batch
        result = await session.execute(
            select(MediaItem).where(MediaItem.batch_id == batch.id)
        )
        items = list(result.scalars().all())

        for item in items:
            # Delete .partial cache file
            src = Path(item.source_path).resolve()
            partial = _partial_path(_cache_path_for(cache_dir, src))
            partial.unlink(missing_ok=True)

            # Reset hop1_status if not completed
            if item.hop1_status != HopStatus.COMPLETED.value:
                item.hop1_status = HopStatus.PENDING.value
                item.error_message = None
                item.touch()

        # Reset batch status
        db_batch = await session.get(TransferBatch, batch.id)
        if db_batch is not None:
            db_batch.status = BatchStatus.PENDING.value
            db_batch.error_message = None
            db_batch.touch()

    logger.info("LOADING batch %d recovered: %d items reset to PENDING", batch.id, len(items))


# ---------------------------------------------------------------------------
# ARCHIVED recovery: verify destination, mark or retry
# ---------------------------------------------------------------------------
async def _recover_archived_batch(
    batch: TransferBatch,
    *,
    cache_dir: Path,
) -> None:
    """
    A batch stuck in ARCHIVED means Hop 2 was interrupted mid-import.

    Action:
    1. For each item, check if the destination file exists and matches cache_hash.
    2. If match: mark item as COMPLETED (verified).
    3. If no match: delete partials, reset item to PENDING for retry.
    4. Reset batch to PENDING for re-processing.
    """
    logger.info("Recovering ARCHIVED batch %d (session %d)", batch.id, batch.session_id)

    async with session_scope() as session:
        # Get session to find dest_root
        ts = await session.get(TransferSession, batch.session_id)
        dest_root = Path(ts.dest_root) if ts else Path(".")

        result = await session.execute(
            select(MediaItem).where(MediaItem.batch_id == batch.id)
        )
        items = list(result.scalars().all())

        for item in items:
            # Check destination
            dst = dest_root / item.file_name
            partial = dst.with_suffix(dst.suffix + PARTIAL_SUFFIX)

            # Clean up any .partial files
            partial.unlink(missing_ok=True)

            if dst.is_file() and item.source_hash:
                if _verify_hash(dst, item.source_hash):
                    # Destination verified
                    item.hop2_status = HopStatus.COMPLETED.value
                    item.final_status = HopStatus.COMPLETED.value
                    item.error_message = None
                    item.touch()
                    continue

            # No match or hash mismatch — reset for retry
            if dst.is_file():
                dst.unlink(missing_ok=True)

            item.hop2_status = HopStatus.PENDING.value
            item.final_status = HopStatus.PENDING.value
            item.error_message = None
            item.touch()

        # Reset batch
        db_batch = await session.get(TransferBatch, batch.id)
        if db_batch is not None:
            db_batch.status = BatchStatus.PENDING.value
            db_batch.error_message = None
            db_batch.completed_items = sum(
                1 for i in items if i.hop2_status == HopStatus.COMPLETED.value
            )
            db_batch.touch()

    logger.info("ARCHIVED batch %d recovered: items verified or reset", batch.id)


# ---------------------------------------------------------------------------
# Hash verification helper
# ---------------------------------------------------------------------------
def _verify_hash(file_path: Path, expected: str) -> bool:
    """Synchronously verify a file's hash against an expected digest."""
    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(BATCH_SIZE * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == expected.lower()
