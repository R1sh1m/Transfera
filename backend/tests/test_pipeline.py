"""
Transfera v2 — Pipeline Interruption Test Suite
Tests crash recovery, .partial cleanup, and data integrity across Hop 1 & 2.
Run: python -m backend.tests.test_pipeline
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import select  # noqa: E402
from backend.config import CACHE_DIR, PARTIAL_SUFFIX  # noqa: E402
from backend.database.manager import (  # noqa: E402
    create_all_tables,
    dispose_engine,
    get_engine,
    session_scope,
)
from backend.database.models import (  # noqa: E402
    Base,
    BatchStatus,
    HopStatus,
    MediaItem,
    TransferBatch,
    TransferSession,
)
from backend.engines.batch_manager import create_batches, get_batch_items  # noqa: E402
from backend.engines.cache_manager import (  # noqa: E402
    _cache_path_for,
    _partial_path,
    cache_batch,
)
from backend.engines.importer import (  # noqa: E402
    compute_archive_path,
    import_batch,
)
from backend.engines.recovery import recover_interrupted_batches  # noqa: E402

# ======================================================================
# Test accounting
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
            msg += f"  -- {detail}"
        print(msg)


# ======================================================================
# Helpers
# ======================================================================
def _touch(path: Path, content: bytes | None = None, size: int = 0) -> Path:
    """Create a file with optional content or padded size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        path.write_bytes(content)
    elif size > 0:
        path.write_bytes(os.urandom(size))
    else:
        path.write_bytes(b"media-content-" + path.name.encode())
    return path


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


async def _reset_db() -> None:
    await dispose_engine()
    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()


# ======================================================================
# 1. Batch creation and chunking
# ======================================================================
async def test_batch_creation() -> None:
    print("\n=== Batch Creation ===")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "source"
        src.mkdir()
        for i in range(250):
            _touch(src / f"img_{i:04d}.jpg")

        # Create session
        async with session_scope() as session:
            ts = TransferSession(
                session_name="batch-test",
                source_root=str(src),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        # Scan files into DB
        from backend.engines.scanner import scan
        item_ids = await scan(src, session_id=session_id)
        _check("Scanned 250 items", len(item_ids) == 250)

        # Create batches
        batch_ids = await create_batches(session_id, item_ids)
        _check("Created 3 batches (ceil(250/100))", len(batch_ids) == 3)

        # Verify batch sizes
        for bid in batch_ids:
            items = await get_batch_items(bid)
            batch_num = batch_ids.index(bid) + 1
            if batch_num < 3:
                _check(f"Batch {batch_num} has 100 items", len(items) == 100)
            else:
                _check(f"Batch 3 has 50 items", len(items) == 50)


# ======================================================================
# 2. Hop 1: Full cache cycle
# ======================================================================
async def test_hop1_full_cache() -> None:
    print("\n=== Hop 1: Full Cache Cycle ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        src_dir.mkdir()

        # Create 5 source files
        source_files = []
        for i in range(5):
            f = _touch(src_dir / f"photo_{i}.jpg", content=f"content-{i}".encode() * 100)
            source_files.append(f)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="hop1-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Run Hop 1
        cached = await cache_batch(batch_ids[0], cache_dir=cache_dir)
        _check("Hop 1 cached 5 items", cached == 5)

        # Verify cache files exist (sharded by md5 prefix)
        cache_files_found = list(cache_dir.rglob("*.jpg"))
        _check(f"Cache has {len(source_files)} files", len(cache_files_found) == len(source_files))

        # Verify .partial files are cleaned up
        partials = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("No .partial files remain", len(partials) == 0)

        # Verify source hashes stored
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.id, MediaItem.source_hash)
            )
            rows = result.all()
            all_hashed = all(r[1] is not None for r in rows)
            _check("All items have source_hash", all_hashed)


# ======================================================================
# 3. Hop 1: Skip if cache already valid
# ======================================================================
async def test_hop1_skip_existing() -> None:
    print("\n=== Hop 1: Skip Existing Cache ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        src_dir.mkdir()

        f = _touch(src_dir / "existing.jpg", content=b"cached-content")

        # Compute hash using the same algorithm as cache_manager
        from backend.engines.cache_manager import _BLAKE3_AVAILABLE
        if _BLAKE3_AVAILABLE:
            import blake3
            h = blake3.blake3()
        else:
            import hashlib as hl
            h = hl.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                h.update(chunk)
        expected_hash = h.hexdigest()

        async with session_scope() as session:
            ts = TransferSession(
                session_name="skip-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)

        # Set source_hash to match what cache_manager would compute
        async with session_scope() as session:
            item = await session.get(MediaItem, item_ids[0])
            item.source_hash = expected_hash

        # Pre-populate cache file at correct sharded path
        cache_path = _cache_path_for(cache_dir, f)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, cache_path)

        batch_ids = await create_batches(session_id, item_ids)
        cached = await cache_batch(batch_ids[0], cache_dir=cache_dir)
        _check("Hop 1 skipped 1 item (cache hit)", cached == 1)


# ======================================================================
# 4. Hop 2: Import cycle
# ======================================================================
async def test_hop2_import() -> None:
    print("\n=== Hop 2: Import Cycle ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        dest_dir = Path(tmp) / "dest"
        src_dir.mkdir()

        for i in range(3):
            _touch(src_dir / f"doc_{i}.pdf", content=f"doc-content-{i}".encode() * 50)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="hop2-test",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Run Hop 1 first
        await cache_batch(batch_ids[0], cache_dir=cache_dir)

        # Run Hop 2
        imported = await import_batch(
            batch_ids[0],
            dest_root=dest_dir,
            cache_dir=cache_dir,
        )
        _check("Hop 2 imported 3 items", imported == 3)

        # Verify destination files
        dest_files = list(dest_dir.rglob("*.pdf"))
        _check("Destination has 3 files", len(dest_files) == 3)


# ======================================================================
# 5. Crash recovery: LOADING batch
# ======================================================================
async def test_recovery_loading() -> None:
    print("\n=== Crash Recovery: LOADING Batch ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        src_dir.mkdir()

        for i in range(3):
            _touch(src_dir / f"img_{i}.jpg")

        async with session_scope() as session:
            ts = TransferSession(
                session_name="recovery-loading",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Simulate crash: set batch to LOADING, leave .partial files
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            batch.status = BatchStatus.LOADING.value
            batch.touch()

            # Create fake .partial files
            for iid in item_ids:
                item = await session.get(MediaItem, iid)
                src = Path(item.source_path).resolve()
                partial = _partial_path(_cache_path_for(cache_dir, src))
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"partial-data")

        # Verify .partial files exist
        partials_before = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("Stale .partial files exist before recovery", len(partials_before) > 0)

        # Run recovery
        stats = await recover_interrupted_batches(cache_dir=cache_dir)
        _check("Recovered 1 LOADING batch", stats["loading_recovered"] == 1)

        # Verify .partial files cleaned up
        partials_after = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("All .partial files cleaned up", len(partials_after) == 0)

        # Verify batch reset to PENDING
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            _check("Batch reset to PENDING", batch.status == BatchStatus.PENDING.value)

        # Verify items reset to PENDING
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.hop1_status).where(MediaItem.batch_id == batch_ids[0])
            )
            statuses = [r[0] for r in result.all()]
            all_pending = all(s == HopStatus.PENDING.value for s in statuses)
            _check("All items reset to PENDING", all_pending)


# ======================================================================
# 6. Crash recovery: ARCHIVED batch
# ======================================================================
async def test_recovery_archived() -> None:
    print("\n=== Crash Recovery: ARCHIVED Batch ===")

    await _reset_db()

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        dest_dir = Path(tmp) / "dest"
        src_dir.mkdir()

        _touch(src_dir / "verified.jpg", content=b"verified-content")
        _touch(src_dir / "bad.jpg", content=b"bad-content")

        async with session_scope() as session:
            ts = TransferSession(
                session_name="recovery-archived",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Run Hop 1 to populate source_hash
        await cache_batch(batch_ids[0], cache_dir=cache_dir)

        # Get hashes
        items = await get_batch_items(batch_ids[0])
        verified_hash = items[0].source_hash
        bad_hash = items[1].source_hash

        # Simulate crash: set batch to ARCHIVED
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            batch.status = BatchStatus.ARCHIVED.value
            batch.touch()

        # Place verified file at destination (correct hash)
        verified_cache = _cache_path_for(cache_dir, Path(items[0].source_path))
        verified_dest = compute_archive_path(dest_dir, items[0])
        verified_dest.parent.mkdir(parents=True, exist_ok=True)
        if verified_cache.is_file():
            shutil.copy2(verified_cache, verified_dest)
        else:
            # If cache not present, create file with matching content
            verified_dest.write_bytes(b"verified-content")

        # Place bad file at destination (wrong content -> wrong hash)
        bad_dest = compute_archive_path(dest_dir, items[1])
        bad_dest.parent.mkdir(parents=True, exist_ok=True)
        bad_dest.write_bytes(b"wrong-content")

        # Run recovery
        stats = await recover_interrupted_batches(cache_dir=cache_dir)
        _check("Recovered 1 ARCHIVED batch", stats["archived_recovered"] == 1)

        # Verify item states
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.file_name, MediaItem.hop2_status, MediaItem.final_status)
                .where(MediaItem.batch_id == batch_ids[0])
            )
            rows = {r[0]: (r[1], r[2]) for r in result.all()}

        verified_status = rows["verified.jpg"]
        _check("Verified item marked COMPLETED", verified_status[0] == HopStatus.COMPLETED.value)

        bad_status = rows["bad.jpg"]
        _check("Bad item reset to PENDING", bad_status[0] == HopStatus.PENDING.value)

        # Verify bad destination file removed
        _check("Bad destination file removed", not bad_dest.exists())


# ======================================================================
# 7. Pipeline integrity: no duplicates, no data loss
# ======================================================================
async def test_pipeline_integrity() -> None:
    print("\n=== Pipeline Integrity ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        dest_dir = Path(tmp) / "dest"
        src_dir.mkdir()

        N = 15
        for i in range(N):
            _touch(src_dir / f"item_{i:03d}.jpg", size=1024 + i * 100)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="integrity-test",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Full pipeline: Hop 1 -> Hop 2
        for bid in batch_ids:
            await cache_batch(bid, cache_dir=cache_dir)
            await import_batch(bid, dest_root=dest_dir, cache_dir=cache_dir)

        # Count DB rows
        async with session_scope() as session:
            from sqlalchemy import func, select as sel
            result = await session.execute(sel(func.count(MediaItem.id)))
            db_count = result.scalar()

        _check(f"DB has exactly {N} items (no duplicates)", db_count == N)

        # Count destination files
        dest_files = list(dest_dir.rglob("*"))
        dest_files = [f for f in dest_files if f.is_file()]
        _check(f"Destination has {N} files", len(dest_files) == N)

        # Verify all items completed
        async with session_scope() as session:
            result = await session.execute(
                sel(MediaItem.final_status).where(MediaItem.session_id == session_id)
            )
            statuses = [r[0] for r in result.all()]
            all_done = all(s == HopStatus.COMPLETED.value for s in statuses)
            _check("All items COMPLETED", all_done)

        # Verify no .partial files remain
        partials = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        partials += list(dest_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("No .partial files in cache or dest", len(partials) == 0)


# ======================================================================
# Runner
# ======================================================================
async def main() -> None:
    print("=" * 60)
    print("  Transfera v2 — Pipeline Integrity Test Suite")
    print("=" * 60)

    await _reset_db()

    await test_batch_creation()
    await _reset_db()

    await test_hop1_full_cache()
    await _reset_db()

    await test_hop1_skip_existing()
    await _reset_db()

    await test_hop2_import()
    await _reset_db()

    await test_recovery_loading()
    await _reset_db()

    await test_recovery_archived()
    await _reset_db()

    await test_pipeline_integrity()
    await _reset_db()

    print("\n" + "=" * 60)
    total = _PASS + _FAIL
    print(f"  Results: {_PASS}/{total} passed, {_FAIL} failed")
    print("=" * 60)

    await dispose_engine()
    if _FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
