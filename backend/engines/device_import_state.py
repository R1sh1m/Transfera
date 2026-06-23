"""
Transfera v2 — Device Import State Manager
Persistent per-device tracking of last import cutoff for incremental imports.

The cutoff is the mtime boundary below which all files are known to have been
handled. Files at or below this timestamp can be safely skipped during scan.
This is a performance optimization layered on top of hash-based duplicate
detection — never a replacement for it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from backend.database.manager import session_scope
from backend.database.models import (
    DeviceImportState,
    HopStatus,
    MediaItem,
    SessionStatus,
    TransferSession,
)

logger = logging.getLogger(__name__)

# Safety overlap to absorb clock/timestamp precision differences.
# The stored cutoff is reduced by this amount before comparing.
CUTOFF_SAFETY_OVERLAP = timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------
async def get_device_state(device_id: str) -> DeviceImportState | None:
    """Return the import state for a device, or None if no record exists."""
    async with session_scope() as session:
        result = await session.execute(
            select(DeviceImportState).where(
                DeviceImportState.device_id == device_id
            )
        )
        return result.scalar_one_or_none()


async def get_cutoff_datetime(device_id: str) -> datetime | None:
    """
    Return the effective cutoff datetime for a device (with safety overlap
    already subtracted), or None if no cutoff exists.

    This is the value to compare against file mtimes during scanning.
    """
    state = await get_device_state(device_id)
    if state is None or state.last_successful_cutoff is None:
        return None
    return state.last_successful_cutoff - CUTOFF_SAFETY_OVERLAP


async def list_all_device_states() -> list[DeviceImportState]:
    """Return all device import states, ordered by most recently updated."""
    async with session_scope() as session:
        result = await session.execute(
            select(DeviceImportState).order_by(
                DeviceImportState.updated_at.desc()
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------
async def upsert_device_state(
    device_id: str,
    device_name: str | None,
    cutoff: datetime,
    session_id: int,
) -> DeviceImportState:
    """
    Insert or update the import state for a device.

    Called after a session completes to advance the cutoff for future
    incremental imports.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(DeviceImportState).where(
                DeviceImportState.device_id == device_id
            )
        )
        state = result.scalar_one_or_none()

        if state is None:
            state = DeviceImportState(
                device_id=device_id,
                device_name=device_name,
                last_successful_cutoff=cutoff,
                last_import_session_id=session_id,
            )
            session.add(state)
            logger.info(
                "Created device import state for %s (cutoff=%s, session=%d)",
                device_id, cutoff.isoformat(), session_id,
            )
        else:
            state.last_successful_cutoff = cutoff
            state.last_import_session_id = session_id
            if device_name is not None:
                state.device_name = device_name
            state.touch()
            logger.info(
                "Updated device import state for %s (cutoff=%s, session=%d)",
                device_id, cutoff.isoformat(), session_id,
            )

        await session.flush()
        return state


async def clear_device_state(device_id: str) -> bool:
    """
    Delete the import state for a device.

    Returns True if a record was deleted, False if no record existed.
    """
    async with session_scope() as session:
        result = await session.execute(
            select(DeviceImportState).where(
                DeviceImportState.device_id == device_id
            )
        )
        state = result.scalar_one_or_none()
        if state is None:
            return False

        await session.delete(state)
        logger.info("Cleared device import state for %s", device_id)
        return True


# ---------------------------------------------------------------------------
# Cutoff computation (the correctness-critical piece)
# ---------------------------------------------------------------------------
async def compute_cutoff_from_session(
    session_id: int,
) -> datetime | None:
    """
    Compute the new cutoff datetime after a session reaches a final state.

    The cutoff is the mtime of the OLDEST item that did NOT successfully
    complete. If every item succeeded, the cutoff is the mtime of the
    NEWEST item.

    This ensures we never advance past a file that failed, was skipped,
    or wasn't verified — even if newer files in the same session succeeded.

    Returns None if the cutoff cannot be computed (no items, session still
    running, etc.).
    """
    async with session_scope() as session:
        # 1. Verify the session is in a final state
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            logger.warning("Session %d not found — cannot compute cutoff", session_id)
            return None

        final_states = {
            SessionStatus.COMPLETED.value,
            SessionStatus.COMPLETED_WITH_ERRORS.value,
        }
        if ts.status not in final_states:
            logger.info(
                "Session %d is in state '%s' — not a final state, skipping cutoff update",
                session_id, ts.status,
            )
            return None

        # 2. Fetch all items for this session, ordered by date_taken (oldest first)
        result = await session.execute(
            select(MediaItem)
            .where(MediaItem.session_id == session_id)
            .order_by(MediaItem.date_taken.asc().nullslast())
        )
        items = list(result.scalars().all())

        if not items:
            logger.info("Session %d has no items — cannot compute cutoff", session_id)
            return None

        # 3. Find the oldest item that did NOT succeed
        failed_item = None
        for item in items:
            if item.final_status != HopStatus.COMPLETED.value:
                failed_item = item
                break

        if failed_item is not None:
            # Cutoff = the mtime of the oldest failed item.
            # This ensures we never skip past a failed file.
            if failed_item.date_taken is not None:
                cutoff = failed_item.date_taken
                logger.info(
                    "Session %d cutoff set to oldest failed item's date: %s "
                    "(item %d: %s, status=%s)",
                    session_id, cutoff.isoformat(),
                    failed_item.id, failed_item.file_name, failed_item.final_status,
                )
            else:
                # Item has no resolved date — use the session's created_at as a
                # conservative fallback. This is imprecise but safe: it won't
                # cause any files to be skipped.
                cutoff = ts.created_at
                logger.warning(
                    "Session %d: oldest failed item %d has no date — using session "
                    "created_at as conservative cutoff: %s",
                    session_id, failed_item.id, cutoff.isoformat(),
                )
        else:
            # All items succeeded — cutoff = the newest item's mtime
            newest_item = items[-1]
            if newest_item.date_taken is not None:
                cutoff = newest_item.date_taken
                logger.info(
                    "Session %d: all items succeeded — cutoff set to newest item's date: %s "
                    "(item %d: %s)",
                    session_id, cutoff.isoformat(),
                    newest_item.id, newest_item.file_name,
                )
            else:
                # Very unlikely: newest item has no date. Use session completed_at.
                cutoff = ts.completed_at or ts.created_at
                logger.warning(
                    "Session %d: newest item %d has no date — using session "
                    "completed_at as cutoff: %s",
                    session_id, newest_item.id, cutoff.isoformat(),
                )

        return cutoff
