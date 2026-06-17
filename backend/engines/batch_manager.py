"""
MediaVault v2 — Batch Manager
Chunks sorted media_items into strict 100-file TransferBatch rows.
"""

from __future__ import annotations

import logging
from math import ceil
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import BATCH_SIZE
from backend.database.manager import session_scope
from backend.database.models import BatchStatus, MediaItem, TransferBatch, TransferSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------
async def create_batches(
    session_id: int,
    item_ids: Sequence[int],
) -> list[int]:
    """
    Partition *item_ids* into ``BATCH_SIZE``-sized chunks, insert a
    ``TransferBatch`` row for each chunk, and assign the batch FK to
    every ``MediaItem`` in that chunk.

    Returns a list of created ``TransferBatch.id`` values (ordered).
    """
    if not item_ids:
        return []

    num_batches = ceil(len(item_ids) / BATCH_SIZE)
    created_ids: list[int] = []

    async with session_scope() as session:
        # Verify session exists
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise ValueError(f"TransferSession {session_id} does not exist")

        for batch_num in range(1, num_batches + 1):
            start = (batch_num - 1) * BATCH_SIZE
            end = start + BATCH_SIZE
            chunk = item_ids[start:end]

            batch = TransferBatch(
                session_id=session_id,
                batch_number=batch_num,
                status=BatchStatus.PENDING.value,
                total_items=len(chunk),
            )
            session.add(batch)
            await session.flush()

            # Assign batch_id to every MediaItem in this chunk
            await session.execute(
                update(MediaItem)
                .where(MediaItem.id.in_(chunk))
                .values(batch_id=batch.id)
            )

            created_ids.append(batch.id)
            logger.info(
                "Batch %d created: %d items (ids %d-%d)",
                batch_num, len(chunk), chunk[0], chunk[-1],
            )

    logger.info("Created %d batches for session %d", len(created_ids), session_id)
    return created_ids


# ---------------------------------------------------------------------------
# Batch queries
# ---------------------------------------------------------------------------
async def get_pending_batches(session_id: int) -> list[TransferBatch]:
    """Return all PENDING batches for a session, ordered by batch_number."""
    async with session_scope() as session:
        result = await session.execute(
            select(TransferBatch)
            .where(
                TransferBatch.session_id == session_id,
                TransferBatch.status.in_([
                    BatchStatus.PENDING.value,
                    BatchStatus.FAILED.value,
                ]),
            )
            .order_by(TransferBatch.batch_number)
        )
        return list(result.scalars().all())


async def get_batch_items(batch_id: int) -> list[MediaItem]:
    """Return all MediaItems assigned to a batch, ordered by id (chronological)."""
    async with session_scope() as session:
        result = await session.execute(
            select(MediaItem)
            .where(MediaItem.batch_id == batch_id)
            .order_by(MediaItem.id)
        )
        return list(result.scalars().all())


async def mark_batch_status(
    batch_id: int,
    status: BatchStatus,
    *,
    error_message: str | None = None,
) -> None:
    """Update a batch's status and optionally record an error."""
    from backend.database.models import _utcnow

    async with session_scope() as session:
        batch = await session.get(TransferBatch, batch_id)
        if batch is None:
            raise ValueError(f"TransferBatch {batch_id} does not exist")
        batch.status = status.value
        batch.touch()
        if error_message is not None:
            batch.error_message = error_message
        if status == BatchStatus.PROCESSING and batch.started_at is None:
            batch.started_at = _utcnow()
        if status in (BatchStatus.COMPLETED, BatchStatus.FAILED):
            batch.completed_at = _utcnow()


async def increment_batch_counters(
    batch_id: int,
    *,
    completed: int = 0,
    failed: int = 0,
) -> None:
    """Atomically increment a batch's completed / failed counters."""
    async with session_scope() as session:
        batch = await session.get(TransferBatch, batch_id)
        if batch is None:
            return
        batch.completed_items += completed
        batch.failed_items += failed
        batch.touch()


async def get_next_batch(session_id: int) -> TransferBatch | None:
    """Return the lowest-numbered PENDING batch, or None if all done."""
    async with session_scope() as session:
        result = await session.execute(
            select(TransferBatch)
            .where(
                TransferBatch.session_id == session_id,
                TransferBatch.status == BatchStatus.PENDING.value,
            )
            .order_by(TransferBatch.batch_number)
            .limit(1)
        )
        return result.scalars().first()
