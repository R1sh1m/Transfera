"""Regression tests for Transfera backend fixes (Issue A/B/C).

Issue A — session counter divergence: rows marked COMPLETED via resume
re-verify, ARCHIVED recovery, or duplicate-skip never bumped the cumulative
``ts.cached_files`` / ``ts.imported_files`` counters, so the detail view
showed 0/25 while the list (row-derived ``completed_items``) showed 25/25.
``_phase_finalize`` now reconciles the cumulatives upward to the verified
row count.

Issue B — doubled destination year: session creation normalizes a trailing
duplicate year (``.../2026/2026`` -> ``.../2026``).

Issue C — 409 on deleting completed sessions: terminal sessions are always
deletable; only genuinely running transfers block.
"""

from __future__ import annotations

import pytest

from backend.api import routes as api_routes
from backend.api.routes import (
    _is_session_delete_blocked,
    _normalize_dest_root,
    _phase_finalize,
)
from backend.database.manager import session_scope
from backend.database.models import HopStatus, MediaItem, SessionStatus, TransferSession


async def _make_session(db_session, **overrides):
    ts = TransferSession(
        session_name="counter-reconcile-test",
        source_root="C:\\src",
        dest_root="D:\\transferred",
        total_items=3,
        **overrides,
    )
    db_session.add(ts)
    await db_session.commit()
    return ts.id


async def _add_item(db_session, sid, final_status, name="a.jpg"):
    mi = MediaItem(
        source_path=f"C:\\src\\{name}",
        file_name=name,
        session_id=sid,
        final_status=final_status,
    )
    db_session.add(mi)
    await db_session.commit()
    return mi


async def test_finalize_reconciles_cumulatives_after_reverify_paths(db_session):
    """Rows COMPLETED without counter bumps still show consistent detail.

    Simulates resume re-verify / recovery-verify / duplicate-skip: all rows
    COMPLETED but cumulatives left at 0. After finalize, cached/imported
    must match completed_items.
    """
    sid = await _make_session(db_session, cached_files=0, imported_files=0)
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "a.jpg")
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "b.jpg")
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "c.jpg")

    await _phase_finalize(sid)

    async with session_scope() as check:
        ts = await check.get(TransferSession, sid)
        assert ts.completed_items == 3
        assert ts.cached_files == 3
        assert ts.imported_files == 3
        assert ts.status == SessionStatus.COMPLETED.value


async def test_finalize_never_counts_down_exact_counters(db_session):
    """Reconcile clamps upward only — exact higher totals are preserved."""
    sid = await _make_session(db_session, cached_files=5, imported_files=5)
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "a.jpg")
    await _add_item(db_session, sid, HopStatus.COMPLETED.value, "b.jpg")
    await _add_item(db_session, sid, HopStatus.FAILED.value, "c.jpg")

    await _phase_finalize(sid)

    async with session_scope() as check:
        ts = await check.get(TransferSession, sid)
        assert ts.completed_items == 2
        assert ts.cached_files == 5
        assert ts.imported_files == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("D:\\transferred\\2026\\2026", "D:\\transferred\\2026"),
        ("D:/transferred/2026/2026", "D:/transferred/2026"),
        ("D:\\transferred\\2026", "D:\\transferred\\2026"),
        ("D:\\transferred\\2026\\10 - October", "D:\\transferred\\2026\\10 - October"),
        ("D:\\transferred\\2025\\2026", "D:\\transferred\\2025\\2026"),
        ("D:\\transferred", "D:\\transferred"),
        ("", ""),
    ],
)
def test_normalize_dest_root_collapses_duplicate_year(raw, expected):
    assert _normalize_dest_root(raw) == expected


def test_terminal_session_delete_never_blocked_even_with_stale_task():
    """A completed session with a stale _active_tasks entry must not 409."""
    api_routes._active_tasks[424242] = object()  # type: ignore[assignment]
    try:
        assert _is_session_delete_blocked(SessionStatus.COMPLETED.value, 424242) is False
        # Stale bookkeeping is evicted as a side effect.
        assert 424242 not in api_routes._active_tasks
    finally:
        api_routes._active_tasks.pop(424242, None)


def test_running_session_delete_still_blocked():
    assert _is_session_delete_blocked(SessionStatus.RUNNING.value, 999999) is True
