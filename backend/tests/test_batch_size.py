"""
Tests for per-session batch sizing (no hardware required).

create_batches() resolves: explicit arg > session.batch_size > BATCH_SIZE,
clamped to 10..500. Buffer-size usages of BATCH_SIZE elsewhere are untouched.
"""

from __future__ import annotations

from backend.database.models import MediaItem, TransferSession
from backend.engines.batch_manager import create_batches


async def _make_session_with_items(db_session, batch_size=None, n=60):
    ts = TransferSession(
        session_name="batch-size-test",
        source_root="C:\\src",
        dest_root="D:\\dst",
        total_items=n,
    )
    if batch_size is not None:
        ts.batch_size = batch_size
    db_session.add(ts)
    await db_session.commit()
    ids = []
    for i in range(n):
        mi = MediaItem(source_path=f"C:\\src\\f{i}.jpg", file_name=f"f{i}.jpg", session_id=ts.id)
        db_session.add(mi)
        await db_session.flush()
        ids.append(mi.id)
    await db_session.commit()
    return ts.id, ids


async def test_custom_batch_size_from_session(db_session):
    """Session batch_size=25 chunks 60 items into 25/25/10."""
    from backend.database.manager import session_scope

    sid, ids = await _make_session_with_items(db_session, batch_size=25)
    created = await create_batches(sid, ids)
    assert len(created) == 3
    async with session_scope() as check:
        from sqlalchemy import select

        from backend.database.models import TransferBatch

        rows = (
            (
                await check.execute(
                    select(TransferBatch).where(TransferBatch.session_id == sid).order_by(TransferBatch.batch_number)
                )
            )
            .scalars()
            .all()
        )
        assert [b.total_items for b in rows] == [25, 25, 10]


async def test_default_batch_size_without_override(db_session):
    """No override -> global default (100) preserved."""
    sid, ids = await _make_session_with_items(db_session, None, n=210)
    created = await create_batches(sid, ids)
    assert len(created) == 3


async def test_batch_size_clamped(db_session):
    """Absurd values clamp to 10..500, never 0/div-by-zero."""
    sid, ids = await _make_session_with_items(db_session, batch_size=5000, n=40)
    assert len(await create_batches(sid, ids)) == 1
    sid2, ids2 = await _make_session_with_items(db_session, batch_size=1, n=25)
    assert len(await create_batches(sid2, ids2)) == 3  # clamped to 10: 10/10/5
