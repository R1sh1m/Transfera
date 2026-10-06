"""
Tests for live session counter rollups (no hardware required).

/api/sessions historically showed completed_items=0 for the entire run
(counters were only assigned at finalize). _rollup_session_item_counters()
recomputes them per finished batch so polling UIs stay truthful mid-run.
"""

from __future__ import annotations

from backend.api.routes import _rollup_session_item_counters
from backend.database.models import HopStatus, MediaItem, TransferSession


async def _make_session(db_session, **overrides):
    ts = TransferSession(
        session_name="rollup-test",
        source_root="ios://TEST/DCIM",
        dest_root="D:\\transferred",
        total_items=4,
        **overrides,
    )
    db_session.add(ts)
    await db_session.commit()
    return ts.id


async def _add_item(db_session, sid, final_status, name="a.jpg"):
    mi = MediaItem(
        source_path=f"ios://TEST/DCIM/{name}",
        file_name=name,
        session_id=sid,
        final_status=final_status,
    )
    db_session.add(mi)
    await db_session.commit()
    return mi


async def test_rollup_counts_completed_and_failed(db_session):
    """Completed/failed recomputed from rows; pending is neither."""
    from backend.database.manager import session_scope

    sid = await _make_session(db_session)
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "a.jpg")
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "b.jpg")
    await _add_item(db_session, sid, HopStatus.FAILED.value, "c.jpg")
    await _add_item(db_session, sid, HopStatus.PENDING.value, "d.jpg")

    await _rollup_session_item_counters(sid)

    async with session_scope() as check:
        ts = await check.get(TransferSession, sid)
        assert ts.completed_items == 2
        assert ts.failed_items == 1


async def test_rollup_bumps_updated_at_and_is_idempotent(db_session):
    """updated_at advances; repeat runs don't double-count."""
    from backend.database.manager import session_scope

    sid = await _make_session(db_session)
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "a.jpg")

    async with session_scope() as before_scope:
        before = (await before_scope.get(TransferSession, sid)).updated_at

    await _rollup_session_item_counters(sid)
    await _rollup_session_item_counters(sid)

    async with session_scope() as check:
        ts = await check.get(TransferSession, sid)
        assert ts.completed_items == 1
        assert ts.failed_items == 0
        assert ts.updated_at >= before
