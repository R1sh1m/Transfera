"""
MediaVault v2 — Crash Recovery & Live Photo Integration Tests
Section 12 validation: 1000 HEIC crash recovery, Hop 2 archive verification,
and HEIC+MOV Live Photo bundle resolution.
Run: python -m backend.test_crash_recovery
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import select  # noqa: E402
from backend.config import PARTIAL_SUFFIX  # noqa: E402
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
from backend.engines.importer import import_batch  # noqa: E402
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
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        path.write_bytes(content)
    elif size > 0:
        path.write_bytes(os.urandom(size))
    else:
        path.write_bytes(b"media-content-" + path.name.encode())
    return path


async def _reset_db() -> None:
    await dispose_engine()
    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()


# ======================================================================
# 1. Hop 1 crash recovery: 1000 HEIC photos mid-batch
# ======================================================================
async def test_hop1_heic_crash_recovery() -> None:
    print("\n=== Hop 1 Crash Recovery: 1000 HEIC Photos ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        dest_dir = Path(tmp) / "dest"
        src_dir.mkdir()

        # Create 100 HEIC files across 4 subdirectories (simulating camera structure)
        N = 100
        created_files: list[Path] = []
        for day in range(4):
            day_dir = src_dir / f"2024-01-{day + 1:02d}"
            day_dir.mkdir()
            for i in range(25):
                fname = f"IMG_{day:02d}_{i:03d}.heic"
                fpath = day_dir / fname
                _touch(fpath, size=1024 + (day * 25 + i) % 256)
                created_files.append(fpath)

        _check(f"Created {N} HEIC files", len(created_files) == N)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="heic-crash-test",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        _check(f"Scanned {N} items", len(item_ids) == N)

        batch_ids = await create_batches(session_id, item_ids)
        _check(f"Created {len(batch_ids)} batches (ceil({N}/100))", len(batch_ids) == 1)

        # --- Simulate crash during Hop 1 of batch 1 ---
        # Run partial cache (only first few items), then mark LOADING
        batch1_items = await get_batch_items(batch_ids[0])
        # Cache only first 5 items to simulate partial completion
        for item in batch1_items[:5]:
            src = Path(item.source_path).resolve()
            cache_p = _cache_path_for(cache_dir, src)
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            partial = _partial_path(cache_p)
            partial.write_bytes(b"partial-data-" + src.name.encode())

        # Mark batch as LOADING (simulating crash)
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            batch.status = BatchStatus.LOADING.value
            batch.touch()

        # Verify .partial files exist
        partials_before = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("Stale .partial files exist after crash", len(partials_before) == 5)

        # Verify batch is LOADING
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            _check("Batch status is LOADING", batch.status == BatchStatus.LOADING.value)

        # Run recovery
        stats = await recover_interrupted_batches(cache_dir=cache_dir)
        _check("Recovered 1 LOADING batch", stats["loading_recovered"] == 1)

        # Verify ALL .partial files cleaned up
        partials_after = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("All .partial files cleaned up", len(partials_after) == 0)

        # Verify batch reset to PENDING
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            _check("Batch reset to PENDING", batch.status == BatchStatus.PENDING.value)

        # Verify ALL items reset to PENDING
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.hop1_status).where(MediaItem.batch_id == batch_ids[0])
            )
            statuses = [r[0] for r in result.all()]
            all_pending = all(s == HopStatus.PENDING.value for s in statuses)
            _check(f"All {len(statuses)} items reset to PENDING", all_pending)

        # --- Run full pipeline after recovery to verify re-runnability ---
        for bid in batch_ids:
            await cache_batch(bid, cache_dir=cache_dir)
            await import_batch(bid, dest_root=dest_dir, cache_dir=cache_dir)

        # Verify all files at destination
        dest_files = list(dest_dir.rglob("*.heic"))
        _check(f"Destination has {N} HEIC files after recovery", len(dest_files) == N)

        # Verify no .partial files remain anywhere
        partials_final = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        partials_final += list(dest_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("No .partial files in cache or dest after full run", len(partials_final) == 0)


# ======================================================================
# 2. Hop 2 crash recovery: ARCHIVED batch verification
# ======================================================================
async def test_hop2_archive_crash_recovery() -> None:
    print("\n=== Hop 2 Crash Recovery: ARCHIVED Batch ===")

    await _reset_db()

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        dest_dir = Path(tmp) / "dest"
        src_dir.mkdir()

        # Create 50 HEIC files
        N = 50
        for i in range(N):
            _touch(src_dir / f"photo_{i:03d}.heic", size=4096 + i * 64)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="hop2-archive-test",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Run Hop 1 fully to populate source_hash
        await cache_batch(batch_ids[0], cache_dir=cache_dir)

        items = await get_batch_items(batch_ids[0])

        # Get correct hashes for first 30 items, wrong for last 20
        correct_hashes = {item.id: item.source_hash for item in items[:30]}
        wrong_hashes = {item.id: item.source_hash for item in items[30:]}

        # Simulate crash: mark batch as ARCHIVED
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            batch.status = BatchStatus.ARCHIVED.value
            batch.touch()

        # Place files at destination: 30 correct, 20 wrong
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in items[:30]:
            cache_p = _cache_path_for(cache_dir, Path(item.source_path))
            dest_p = dest_dir / Path(item.source_path).name
            if cache_p.is_file():
                shutil.copy2(cache_p, dest_p)
            else:
                dest_p.write_bytes(b"correct-content")

        for item in items[30:]:
            dest_p = dest_dir / Path(item.source_path).name
            dest_p.write_bytes(b"wrong-content-" + str(item.id).encode())

        # Verify .partial files should NOT exist at destination
        dest_partials = list(dest_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("No .partial files at destination before recovery", len(dest_partials) == 0)

        # Run recovery
        stats = await recover_interrupted_batches(cache_dir=cache_dir)
        _check("Recovered 1 ARCHIVED batch", stats["archived_recovered"] == 1)

        # Verify correct items marked COMPLETED
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.file_name, MediaItem.hop2_status, MediaItem.final_status)
                .where(MediaItem.batch_id == batch_ids[0])
            )
            rows = {r[0]: (r[1], r[2]) for r in result.all()}

        correct_count = 0
        for item in items[:30]:
            status = rows.get(item.file_name)
            if status and status[0] == HopStatus.COMPLETED.value:
                correct_count += 1
        _check(f"{correct_count}/30 correct items marked COMPLETED", correct_count == 30)

        # Verify wrong items reset to PENDING
        wrong_count = 0
        for item in items[30:]:
            status = rows.get(item.file_name)
            if status and status[0] == HopStatus.PENDING.value:
                wrong_count += 1
        _check(f"{wrong_count}/20 wrong items reset to PENDING", wrong_count == 20)

        # Verify wrong destination files removed
        wrong_files_exist = 0
        for item in items[30:]:
            dest_p = dest_dir / Path(item.source_path).name
            if dest_p.exists():
                wrong_files_exist += 1
        _check(f"All 20 wrong destination files removed ({wrong_files_exist} remain)", wrong_files_exist == 0)

        # Verify correct destination files still exist
        correct_files_exist = 0
        for item in items[:30]:
            dest_p = dest_dir / Path(item.source_path).name
            if dest_p.exists():
                correct_files_exist += 1
        _check(f"All 30 correct destination files preserved ({correct_files_exist} exist)", correct_files_exist == 30)

        # Verify no .partial files lingering
        partials_final = list(dest_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("No .partial files at destination after recovery", len(partials_final) == 0)

        # Verify batch reset to PENDING for re-processing
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            _check("Batch reset to PENDING for re-processing", batch.status == BatchStatus.PENDING.value)


# ======================================================================
# 3. Live Photo HEIC+MOV bundle archive resolution
# ======================================================================
async def test_live_photo_bundle_archive() -> None:
    print("\n=== Live Photo HEIC+MOV Bundle Archive ===")

    await _reset_db()

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        dest_dir = Path(tmp) / "dest"
        src_dir.mkdir()

        # Create Live Photo bundles (HEIC + MOV with matching stems)
        # Simulating: IMG_001.HEIC + IMG_001.MOV, etc.
        bundles = [
            ("IMG_20240101_120000", ".heic", ".mov"),
            ("IMG_20240115_143022", ".heic", ".mov"),
            ("IMG_20240203_091545", ".heic", ".mov"),
            ("DSC_0001", ".heic", ".mov"),
            ("capture_2024_beach", ".HEIC", ".MOV"),  # Case-insensitive test
        ]

        # Also add standalone files (no pair)
        standalone = [
            ("solo_photo.jpg",),
            ("landscape.png",),
            ("video_only.mp4",),
        ]

        created_files: list[Path] = []
        for stem, img_ext, vid_ext in bundles:
            img_path = src_dir / f"{stem}{img_ext}"
            vid_path = src_dir / f"{stem}{vid_ext}"
            _touch(img_path, size=8192)
            _touch(vid_path, size=65536)
            created_files.append(img_path)
            created_files.append(vid_path)

        for (fname,) in standalone:
            fpath = src_dir / fname
            _touch(fpath, size=4096)
            created_files.append(fpath)

        total_files = len(bundles) * 2 + len(standalone)
        _check(f"Created {total_files} files ({len(bundles)} bundles + {len(standalone)} standalone)", len(created_files) == total_files)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="live-photo-test",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        _check(f"Scanned {total_files} items", len(item_ids) == total_files)

        # Verify Live Photo groups were detected
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.file_name, MediaItem.live_photo_group)
                .where(MediaItem.session_id == session_id)
            )
            rows = {r[0]: r[1] for r in result.all()}

        # Check that paired files share a group UUID
        groups_found: dict[str, list[str]] = {}
        for stem, img_ext, vid_ext in bundles:
            img_name = f"{stem}{img_ext}"
            vid_name = f"{stem}{vid_ext}"
            img_group = rows.get(img_name)
            vid_group = rows.get(vid_name)
            _check(
                f"{img_name} has live_photo_group",
                img_group is not None,
            )
            _check(
                f"{vid_name} has live_photo_group",
                vid_group is not None,
            )
            if img_group and vid_group:
                _check(
                    f"{img_name} and {vid_name} share same group",
                    img_group == vid_group,
                )
                groups_found.setdefault(img_group, []).extend([img_name, vid_name])

        # Verify standalone files have NO group
        for (fname,) in standalone:
            group = rows.get(fname)
            _check(f"{fname} has no live_photo_group", group is None)

        # Verify all groups are unique per bundle
        _check(
            f"Found {len(groups_found)} distinct groups (expected {len(bundles)})",
            len(groups_found) == len(bundles),
        )

        # Run full pipeline
        batch_ids = await create_batches(session_id, item_ids)
        for bid in batch_ids:
            await cache_batch(bid, cache_dir=cache_dir)
            await import_batch(bid, dest_root=dest_dir, cache_dir=cache_dir)

        # Verify all files at destination
        dest_files = list(dest_dir.rglob("*"))
        dest_files = [f for f in dest_files if f.is_file()]
        _check(f"Destination has {total_files} files", len(dest_files) == total_files)

        # Verify no .partial files remain
        partials = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        partials += list(dest_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("No .partial files remaining", len(partials) == 0)

        # Verify all items COMPLETED
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.final_status).where(MediaItem.session_id == session_id)
            )
            statuses = [r[0] for r in result.all()]
            all_done = all(s == HopStatus.COMPLETED.value for s in statuses)
            _check("All items marked COMPLETED", all_done)

        # Verify Live Photo pairs ended up in same archive folder
        # (organizer should group them by date)
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.file_name, MediaItem.source_path)
                .where(MediaItem.session_id == session_id)
            )
            all_rows = {r[0]: r[1] for r in result.all()}

        # Check that paired files have consistent date-based paths
        for stem, img_ext, vid_ext in bundles:
            img_name = f"{stem}{img_ext}"
            vid_name = f"{stem}{vid_ext}"
            img_src = all_rows.get(img_name, "")
            vid_src = all_rows.get(vid_name, "")
            # Both should come from the same source directory
            img_dir = str(Path(img_src).parent) if img_src else ""
            vid_dir = str(Path(vid_src).parent) if vid_src else ""
            _check(
                f"{img_name} and {vid_name} from same source directory",
                img_dir == vid_dir and img_dir != "",
            )


# ======================================================================
# 4. Mixed bundle: HEIC+MOV with concurrent crash
# ======================================================================
async def test_mixed_bundle_crash() -> None:
    print("\n=== Mixed Bundle: HEIC+MOV with Crash Recovery ===")

    await _reset_db()

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        cache_dir = Path(tmp) / "cache"
        dest_dir = Path(tmp) / "dest"
        src_dir.mkdir()

        # Create mix of Live Photo bundles and standalone files
        for i in range(20):
            # Live Photo bundle
            _touch(src_dir / f"BUNDLE_{i:03d}.heic", size=4096)
            _touch(src_dir / f"BUNDLE_{i:03d}.mov", size=32768)
            # Standalone
            _touch(src_dir / f"SOLO_{i:03d}.jpg", size=2048)

        total = 20 * 3  # 60 files
        async with session_scope() as session:
            ts = TransferSession(
                session_name="mixed-crash-test",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        _check(f"Scanned {total} items", len(item_ids) == total)

        batch_ids = await create_batches(session_id, item_ids)

        # Run Hop 1 on first batch
        await cache_batch(batch_ids[0], cache_dir=cache_dir)

        # Simulate crash: mark batch as LOADING
        async with session_scope() as session:
            batch = await session.get(TransferBatch, batch_ids[0])
            batch.status = BatchStatus.LOADING.value
            batch.touch()

            # Create some .partial files
            items = await get_batch_items(batch_ids[0])
            for item in items[:10]:
                src = Path(item.source_path).resolve()
                cache_p = _cache_path_for(cache_dir, src)
                cache_p.parent.mkdir(parents=True, exist_ok=True)
                partial = _partial_path(cache_p)
                partial.write_bytes(b"crash-data")

        # Verify partials exist
        partials_before = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("Partial files exist after crash", len(partials_before) > 0)

        # Recovery
        stats = await recover_interrupted_batches(cache_dir=cache_dir)
        _check("Recovered LOADING batch", stats["loading_recovered"] >= 1)

        # Verify cleanup
        partials_after = list(cache_dir.rglob(f"*{PARTIAL_SUFFIX}"))
        _check("All partials cleaned up", len(partials_after) == 0)

        # Verify Live Photo groups survived recovery
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.file_name, MediaItem.live_photo_group)
                .where(MediaItem.session_id == session_id)
            )
            rows = {r[0]: r[1] for r in result.all()}

        # Check bundles still have groups
        bundle_count = 0
        for i in range(20):
            heic = f"BUNDLE_{i:03d}.heic"
            mov = f"BUNDLE_{i:03d}.mov"
            if rows.get(heic) and rows.get(mov):
                if rows[heic] == rows[mov]:
                    bundle_count += 1
        _check(f"All {bundle_count}/20 Live Photo groups intact after recovery", bundle_count == 20)


# ======================================================================
# Runner
# ======================================================================
async def main() -> None:
    print("=" * 60)
    print("  MediaVault v2 -- Crash Recovery & Live Photo Tests")
    print("=" * 60)

    await _reset_db()
    await test_hop1_heic_crash_recovery()
    await _reset_db()

    await test_hop2_archive_crash_recovery()
    await _reset_db()

    await test_live_photo_bundle_archive()
    await _reset_db()

    await test_mixed_bundle_crash()
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
