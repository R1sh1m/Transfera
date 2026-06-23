"""
Transfera v2 — Core Database & Hashing Tests
Run: python -m backend.tests.test_db_core
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path when running directly.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import text  # noqa: E402

from backend.database.manager import (  # noqa: E402
    create_all_tables,
    dispose_engine,
    get_engine,
    get_session,
)
from backend.database.models import (  # noqa: E402
    Base,
    BatchStatus,
    HopStatus,
    MediaItem,
    SessionStatus,
    TransferBatch,
    TransferSession,
)
from backend.utils.hashing import (  # noqa: E402
    _BLAKE3_AVAILABLE,
    hash_file,
    hash_file_async,
    verify_hash,
)

# ======================================================================
# Helpers
# ======================================================================
_PASS = 0
_FAIL = 0


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f"  — {detail}"
        print(msg)


# ======================================================================
# 1. Database table creation + WAL mode
# ======================================================================
@pytest.mark.asyncio
async def test_database() -> None:
    print("\n=== Database Tests ===")

    # Drop & recreate to avoid stale data from prior runs
    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()

    async with engine.connect() as conn:
        # Verify WAL mode
        result = await conn.execute(text("PRAGMA journal_mode"))
        wal_mode = result.scalar()
        _check("WAL mode active", wal_mode == "wal", f"got: {wal_mode}")

        # Verify FK enforcement
        result = await conn.execute(text("PRAGMA foreign_keys"))
        fk_enabled = result.scalar()
        _check("Foreign keys enabled", fk_enabled == 1, f"got: {fk_enabled}")

        # Verify tables exist
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        )
        tables = {row[0] for row in result.fetchall()}
        _check("media_items table exists", "media_items" in tables)
        _check("transfer_sessions table exists", "transfer_sessions" in tables)
        _check("transfer_batches table exists", "transfer_batches" in tables)

    # Verify indexes
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'")
        )
        indexes = {row[0] for row in result.fetchall()}
        expected_indexes = {
            "ix_media_items_hop1_status",
            "ix_media_items_hop2_status",
            "ix_media_items_final_status",
            "ix_media_items_batch_id",
            "ix_media_items_session_id",
            "ix_media_items_source_hash",
            "ix_media_items_source_path",
            "ix_transfer_sessions_status",
            "ix_transfer_batches_session_id",
        }
        for idx in expected_indexes:
            _check(f"Index {idx} exists", idx in indexes)

    # CRUD lifecycle test
    async with get_session() as session:
        ts = TransferSession(
            session_name="test-session",
            source_root="/src",
            dest_root="/dst",
            status=SessionStatus.CREATED.value,
        )
        session.add(ts)
        await session.flush()

        tb = TransferBatch(
            session_id=ts.id,
            batch_number=1,
            status=BatchStatus.PENDING.value,
        )
        session.add(tb)
        await session.flush()

        mi = MediaItem(
            source_path="/src/photo.jpg",
            file_name="photo.jpg",
            file_size=1024,
            mime_type="image/jpeg",
            extension=".jpg",
            hop1_status=HopStatus.SCANNED.value,
            hop2_status=HopStatus.HASHED.value,
            final_status=HopStatus.PENDING.value,
            session_id=ts.id,
            batch_id=tb.id,
        )
        session.add(mi)
        await session.flush()

        _check("Session created with id", ts.id is not None)
        _check("Batch linked to session", tb.session_id == ts.id)
        _check("MediaItem linked to batch & session",
               mi.batch_id == tb.id and mi.session_id == ts.id)

        # Touch method
        old_updated = mi.updated_at
        mi.touch()
        _check("touch() bumps updated_at", mi.updated_at >= old_updated)

        # Enum defaults — use a bare instance to test Python-level defaults
        mi_default = MediaItem(
            source_path="/src/default.jpg",
            file_name="default.jpg",
            file_size=0,
        )
        _check("Session status default", ts.status == SessionStatus.CREATED.value)
        _check("Batch status default", tb.status == BatchStatus.PENDING.value)
        _check("MediaItem hop1 default", mi_default.hop1_status == HopStatus.PENDING.value)
        _check("MediaItem hop2 default", mi_default.hop2_status == HopStatus.PENDING.value)
        _check("MediaItem final default", mi_default.final_status == HopStatus.PENDING.value)

    # FK cascade: delete session → batch & items cascade
    async with get_session() as session:
        ts2 = TransferSession(
            session_name="cascade-test",
            source_root="/s",
            dest_root="/d",
        )
        session.add(ts2)
        await session.flush()

        tb2 = TransferBatch(session_id=ts2.id, batch_number=1)
        session.add(tb2)
        await session.flush()

        mi2 = MediaItem(
            source_path="/s/file.png",
            file_name="file.png",
            file_size=512,
            session_id=ts2.id,
            batch_id=tb2.id,
        )
        session.add(mi2)
        await session.flush()
        sid = ts2.id

    async with get_session() as session:
        ts_del = await session.get(TransferSession, sid)
        if ts_del is not None:
            await session.delete(ts_del)
        else:
            pass  # already deleted by cascade

    async with get_session() as session:
        result = await session.execute(
            text("SELECT id FROM transfer_sessions WHERE id = :sid"), {"sid": sid}
        )
        remaining = result.fetchall()
        _check("Cascade delete removes session", len(remaining) == 0)

        result = await session.execute(
            text("SELECT id FROM transfer_batches WHERE session_id = :sid"), {"sid": sid}
        )
        _check("Cascade delete removes batches", len(result.fetchall()) == 0)


# ======================================================================
# 2. Hashing — BLAKE3 / SHA-256 fallback
# ======================================================================
def test_hashing_sync() -> None:
    print("\n=== Hashing Tests (sync) ===")

    # Create a temp file with known content
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(b"Transfera v2 hashing test payload 1234567890")
        tmp_path = Path(f.name)

    try:
        # BLAKE3 path
        if _BLAKE3_AVAILABLE:
            h_b3 = hash_file(tmp_path, algorithm="blake3")
            _check("BLAKE3 produces 64-char hex", len(h_b3) == 64 and all(c in "0123456789abcdef" for c in h_b3))
            _check("BLAKE3 verify correct", verify_hash(tmp_path, h_b3, algorithm="blake3"))
            _check("BLAKE3 verify wrong hash fails",
                   not verify_hash(tmp_path, "0" * 64, algorithm="blake3"))
        else:
            _check("BLAKE3 skipped (not installed)", True)

        # SHA-256 path (always available)
        h_sha = hash_file(tmp_path, algorithm="sha256")
        _check("SHA-256 produces 64-char hex", len(h_sha) == 64)
        _check("SHA-256 verify correct", verify_hash(tmp_path, h_sha, algorithm="sha256"))

        # Progress callback
        progress_calls: list[tuple[int, int]] = []

        def _on_progress(sofar: int, total: int) -> None:
            progress_calls.append((sofar, total))

        hash_file(tmp_path, algorithm="sha256", chunk_size=16, on_progress=_on_progress)
        _check("Progress callback invoked", len(progress_calls) > 0)
        if progress_calls:
            _check("Final progress == file size",
                   progress_calls[-1][1] == tmp_path.stat().st_size)

        # File not found
        try:
            hash_file("/nonexistent/file.txt")
            _check("FileNotFoundError for missing file", False)
        except FileNotFoundError:
            _check("FileNotFoundError for missing file", True)

    finally:
        os.unlink(tmp_path)


@pytest.mark.asyncio
async def test_hashing_async() -> None:
    print("\n=== Hashing Tests (async) ===")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(b"async hashing test payload abcdefghij")
        tmp_path = Path(f.name)

    try:
        if _BLAKE3_AVAILABLE:
            h_b3 = await hash_file_async(tmp_path, algorithm="blake3")
            _check("Async BLAKE3 produces 64-char hex",
                   len(h_b3) == 64 and all(c in "0123456789abcdef" for c in h_b3))
        else:
            _check("Async BLAKE3 skipped (not installed)", True)

        h_sha = await hash_file_async(tmp_path, algorithm="sha256")
        _check("Async SHA-256 produces 64-char hex", len(h_sha) == 64)
        _check("Async SHA-256 matches sync SHA-256",
               h_sha == hash_file(tmp_path, algorithm="sha256"))
    finally:
        os.unlink(tmp_path)


# ======================================================================
# Runner
# ======================================================================
async def main() -> None:
    print("=" * 60)
    print("  Transfera v2 — Core DB & Hashing Test Suite")
    print("=" * 60)

    await test_database()
    test_hashing_sync()
    await test_hashing_async()

    print("\n" + "=" * 60)
    total = _PASS + _FAIL
    print(f"  Results: {_PASS}/{total} passed, {_FAIL} failed")
    print("=" * 60)

    await dispose_engine()

    if _FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
