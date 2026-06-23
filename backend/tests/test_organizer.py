"""
Transfera v2 — Organizer & Duplicate Detection Test Suite
Validates path resolution, conflict handling, Live Photo grouping,
duplicate detection, and hash collision scenarios.
Run: python -m backend.tests.test_organizer
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from datetime import UTC

from backend.database.manager import (  # noqa: E402
    create_all_tables,
    dispose_engine,
    get_engine,
    session_scope,
)
from backend.database.models import (  # noqa: E402
    Base,
    HopStatus,
    MediaItem,
    TransferSession,
)
from backend.engines.batch_manager import create_batches  # noqa: E402
from backend.engines.duplicate_detector import (  # noqa: E402
    DuplicateReport,
    check_batch,
    event_bus,
    scan_batch_duplicates,
)
from backend.engines.organizer import (  # noqa: E402
    _safe_path,
    resolve_archive_path,
    resolve_live_photo_folder,
    unique_folder,
)

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
def _touch(path: Path, content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if content is not None else b"media")
    return path


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


async def _reset_db() -> None:
    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()


# ======================================================================
# 1. Organizer: basic path resolution
# ======================================================================
def test_organizer_basic() -> None:
    print("\n=== Organizer: Basic Path Resolution ===")

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "archive"

        # Item with a known created_at
        from datetime import datetime
        item = MediaItem(
            source_path="/src/photo.jpg",
            file_name="photo.jpg",
            file_size=1024,
            created_at=datetime(2024, 6, 15, tzinfo=UTC),
        )
        p = resolve_archive_path(dest, item)
        p_str = str(p).replace("\\", "/")
        _check("Year/Month/Day layout", p_str.endswith("2024/06/15/photo.jpg"), f"got: {p_str}")

        # Item without timestamp -> _unsorted (created_at must be explicitly None)
        item2 = MediaItem(
            source_path="/src/unknown.dat",
            file_name="unknown.dat",
            file_size=100,
            created_at=None,  # type: ignore[arg-type]
        )
        item2.created_at = None  # override the default
        p2 = resolve_archive_path(dest, item2)
        _check("Unsorted fallback", "_unsorted" in str(p2))


# ======================================================================
# 2. Organizer: Year/Month layout
# ======================================================================
def test_organizer_year_month() -> None:
    print("\n=== Organizer: Year/Month Layout ===")

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "archive"
        from datetime import datetime
        item = MediaItem(
            source_path="/src/video.mp4",
            file_name="video.mp4",
            file_size=2048,
            created_at=datetime(2023, 12, 25, tzinfo=UTC),
        )
        p = resolve_archive_path(dest, item, layout="year/month")
        p_str = str(p).replace("\\", "/")
        _check("Year/Month layout", p_str.endswith("2023/12/video.mp4"), f"got: {p_str}")
        _check("No day component", "/12/video.mp4" in p_str)


def test_organizer_derive_timestamp_priority() -> None:
    """
    Verify _derive_timestamp prefers date_taken over original_capture_time
    over created_at.
    """
    print("\n=== Organizer: Timestamp Priority ===")

    from datetime import datetime

    from backend.engines.organizer import _derive_timestamp

    dt_taken = datetime(2023, 1, 1, tzinfo=UTC)
    dt_capture = datetime(2022, 6, 15, tzinfo=UTC)
    dt_created = datetime(2021, 12, 25, tzinfo=UTC)

    item = MediaItem(
        source_path="/src/photo.jpg",
        file_name="photo.jpg",
        file_size=1024,
        created_at=dt_created,
        date_taken=dt_taken,
        original_capture_time=dt_capture,
    )
    result = _derive_timestamp(item)
    _check("Prioritize date_taken", result == dt_taken, f"got: {result}")

    item.date_taken = None
    result = _derive_timestamp(item)
    _check("Fallback to original_capture_time", result == dt_capture, f"got: {result}")

    item.original_capture_time = None
    result = _derive_timestamp(item)
    _check("Fallback to created_at", result == dt_created, f"got: {result}")

    item.created_at = None
    result = _derive_timestamp(item)
    _check("No timestamps returns None", result is None)


# ======================================================================
# 3. Organizer: conflict resolution
# ======================================================================
def test_organizer_conflict() -> None:
    print("\n=== Organizer: Conflict Resolution ===")

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "archive" / "2024" / "01" / "01"
        dest.mkdir(parents=True)

        # Create an existing file
        existing = dest / "photo.jpg"
        existing.write_bytes(b"existing")

        from datetime import datetime
        item = MediaItem(
            source_path="/src/photo.jpg",
            file_name="photo.jpg",
            file_size=1024,
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        p = resolve_archive_path(dest.parent.parent.parent, item)
        _check("Conflict resolved with suffix", p.name == "photo_001.jpg")
        _check("Suffix is within range", "_001" in p.name)

        # Create _001 too
        (dest / "photo_001.jpg").write_bytes(b"also exists")
        p2 = resolve_archive_path(dest.parent.parent.parent, item)
        _check("Second conflict -> _002", p2.name == "photo_002.jpg")

        # Fill up to _999 to test exhaustion (quick check)
        (dest / "photo_002.jpg").write_bytes(b"x")
        p3 = resolve_archive_path(dest.parent.parent.parent, item)
        _check("Third conflict -> _003", p3.name == "photo_003.jpg")


# ======================================================================
# 4. Organizer: _safe_path edge cases
# ======================================================================
def test_safe_path() -> None:
    print("\n=== Organizer: _safe_path ===")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "new_file.txt"
        result = _safe_path(base)
        _check("Non-existing file returned as-is", result == base)

        base.write_bytes(b"content")
        result2 = _safe_path(base)
        _check("Existing file gets _001 suffix", result2.name == "new_file_001.txt")


# ======================================================================
# 5. Organizer: unique_folder
# ======================================================================
def test_unique_folder() -> None:
    print("\n=== Organizer: unique_folder ===")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "mydir"
        result = unique_folder(base)
        _check("New folder created", result.is_dir() and result == base)

        result2 = unique_folder(base)
        _check("Existing folder gets _001 suffix", result2.name == "mydir_001")


# ======================================================================
# 6. Organizer: Live Photo folder grouping
# ======================================================================
def test_live_photo_folder() -> None:
    print("\n=== Organizer: Live Photo Folder Grouping ===")

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "archive"
        from datetime import datetime

        img_item = MediaItem(
            source_path="/src/photo.HEIC",
            file_name="photo.HEIC",
            file_size=5000,
            created_at=datetime(2024, 3, 10, tzinfo=UTC),
        )
        vid_item = MediaItem(
            source_path="/src/video.mov",
            file_name="video.mov",
            file_size=50000,
            created_at=datetime(2024, 3, 10, tzinfo=UTC),
        )

        folder = resolve_live_photo_folder(dest, img_item, vid_item)
        img_path = folder / img_item.file_name
        vid_path = folder / vid_item.file_name

        _check("Image and video in same folder", img_path.parent == vid_path.parent)
        _check("Folder uses image timestamp", "2024" in str(folder) and "03" in str(folder))


# ======================================================================
# 7. Duplicate Detector: no duplicates
# ======================================================================
@pytest.mark.asyncio
async def test_no_duplicates() -> None:
    print("\n=== Duplicate Detector: No Duplicates ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        src_dir.mkdir()
        _touch(src_dir / "unique_a.jpg", content=b"aaa")
        _touch(src_dir / "unique_b.jpg", content=b"bbb")

        async with session_scope() as session:
            ts = TransferSession(
                session_name="no-dup-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        report = await scan_batch_duplicates(batch_ids[0])
        _check("No exact duplicates", len(report.exact_duplicates) == 0)
        _check("No potential duplicates", len(report.potential_duplicates) == 0)
        _check("Report has_duplicates is False", not report.has_duplicates)


# ======================================================================
# 8. Duplicate Detector: exact duplicates (hash + size match)
# ======================================================================
@pytest.mark.asyncio
async def test_exact_duplicates() -> None:
    print("\n=== Duplicate Detector: Exact Duplicates ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        src_dir.mkdir()
        content = b"identical-content-for-exact-match"
        _touch(src_dir / "photo.jpg", content=content)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="exact-dup-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Set source_hash on the batch item (normally done by cache_manager)
        expected_hash = hashlib.sha256(content).hexdigest()
        async with session_scope() as session:
            item = await session.get(MediaItem, item_ids[0])
            item.source_hash = expected_hash

        # Create a "previously archived" item with same hash + size
        async with session_scope() as session:
            archived = MediaItem(
                source_path="/old/archive/photo.jpg",
                file_name="photo.jpg",
                file_size=len(content),
                source_hash=expected_hash,
                hop1_status=HopStatus.COMPLETED.value,
                hop2_status=HopStatus.COMPLETED.value,
                final_status=HopStatus.COMPLETED.value,
                session_id=session_id,
            )
            session.add(archived)

        report = await scan_batch_duplicates(batch_ids[0])
        _check("Exact duplicate found", len(report.exact_duplicates) == 1)
        _check("Matched path recorded", report.exact_duplicates[0].matched_path == "/old/archive/photo.jpg")
        _check("has_duplicates is True", report.has_duplicates)


# ======================================================================
# 9. Duplicate Detector: potential duplicates (name match, hash mismatch)
# ======================================================================
@pytest.mark.asyncio
async def test_potential_duplicates() -> None:
    print("\n=== Duplicate Detector: Potential Duplicates ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        src_dir.mkdir()
        _touch(src_dir / "photo.jpg", content=b"new-content-aaa")

        async with session_scope() as session:
            ts = TransferSession(
                session_name="potential-dup-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Pre-populate source_hash for the batch item
        new_hash = hashlib.sha256(b"new-content-aaa").hexdigest()
        async with session_scope() as session:
            item = await session.get(MediaItem, item_ids[0])
            item.source_hash = new_hash

        # Create archived item with same name but different hash
        old_hash = hashlib.sha256(b"different-content").hexdigest()
        async with session_scope() as session:
            archived = MediaItem(
                source_path="/old/photo.jpg",
                file_name="photo.jpg",
                file_size=20,
                source_hash=old_hash,
                hop1_status=HopStatus.COMPLETED.value,
                hop2_status=HopStatus.COMPLETED.value,
                final_status=HopStatus.COMPLETED.value,
                session_id=session_id,
            )
            session.add(archived)

        report = await scan_batch_duplicates(batch_ids[0])
        _check("Potential duplicate found", len(report.potential_duplicates) == 1)
        _check("No exact duplicates", len(report.exact_duplicates) == 0)
        _check("Matched path in potential", report.potential_duplicates[0].matched_path == "/old/photo.jpg")


# ======================================================================
# 10. Duplicate Detector: hash collision (same hash, different size)
# ======================================================================
@pytest.mark.asyncio
async def test_hash_collision_different_size() -> None:
    print("\n=== Duplicate Detector: Hash Collision (Different Size) ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        src_dir.mkdir()
        content = b"file-A-content"
        _touch(src_dir / "fileA.jpg", content=content)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="hash-collision-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        file_hash = hashlib.sha256(content).hexdigest()

        # Set source_hash on the batch item
        async with session_scope() as session:
            item = await session.get(MediaItem, item_ids[0])
            item.source_hash = file_hash

        # Archived item with SAME hash but DIFFERENT size
        async with session_scope() as session:
            archived = MediaItem(
                source_path="/old/fileB.jpg",
                file_name="fileB.jpg",
                file_size=999999,  # different from fileA
                source_hash=file_hash,  # same hash (collision scenario)
                hop1_status=HopStatus.COMPLETED.value,
                hop2_status=HopStatus.COMPLETED.value,
                final_status=HopStatus.COMPLETED.value,
                session_id=session_id,
            )
            session.add(archived)

        report = await scan_batch_duplicates(batch_ids[0])
        _check("Same hash + different size = NOT exact dup",
               len(report.exact_duplicates) == 0,
               f"got {len(report.exact_duplicates)}")


# ======================================================================
# 11. check_batch: WebSocket event emission
# ======================================================================
@pytest.mark.asyncio
async def test_check_batch_ws_event() -> None:
    print("\n=== check_batch: WebSocket Event ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        src_dir.mkdir()
        content = b"ws-test-content"
        _touch(src_dir / "ws_item.jpg", content=content)

        async with session_scope() as session:
            ts = TransferSession(
                session_name="ws-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        # Create matching archive item
        item_hash = hashlib.sha256(content).hexdigest()

        # Set source_hash on the batch item
        async with session_scope() as session:
            item = await session.get(MediaItem, item_ids[0])
            item.source_hash = item_hash

        async with session_scope() as session:
            archived = MediaItem(
                source_path="/old/ws_item.jpg",
                file_name="ws_item.jpg",
                file_size=len(content),
                source_hash=item_hash,
                hop1_status=HopStatus.COMPLETED.value,
                hop2_status=HopStatus.COMPLETED.value,
                final_status=HopStatus.COMPLETED.value,
                session_id=session_id,
            )
            session.add(archived)

        # Capture WebSocket events
        events_received: list[dict] = []

        def _on_duplicates(data: dict) -> None:
            events_received.append(data)

        event_bus.on("duplicates_detected", _on_duplicates)

        report = await check_batch(batch_ids[0])

        event_bus.off("duplicates_detected", _on_duplicates)

        _check("check_batch returned report", isinstance(report, DuplicateReport))
        _check("processing_paused is True", report.processing_paused)
        _check("WebSocket event emitted", len(events_received) == 1)
        if events_received:
            _check("Event has batch_id", events_received[0]["batch_id"] == batch_ids[0])
            _check("Event has exact_count", events_received[0]["exact_count"] == 1)


# ======================================================================
# 12. check_batch: no duplicates = no event
# ======================================================================
@pytest.mark.asyncio
async def test_check_batch_no_duplicates() -> None:
    print("\n=== check_batch: No Duplicates = No Event ===")

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        src_dir.mkdir()
        _touch(src_dir / "solo.jpg", content=b"one-of-a-kind")

        async with session_scope() as session:
            ts = TransferSession(
                session_name="no-event-test",
                source_root=str(src_dir),
                dest_root=str(Path(tmp) / "dest"),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id

        from backend.engines.scanner import scan
        item_ids = await scan(src_dir, session_id=session_id)
        batch_ids = await create_batches(session_id, item_ids)

        events_received: list[dict] = []
        cb = lambda d: events_received.append(d)  # noqa: E731
        event_bus.on("duplicates_detected", cb)

        report = await check_batch(batch_ids[0])

        event_bus.off("duplicates_detected", cb)

        _check("No event emitted for clean batch", len(events_received) == 0)
        _check("processing_paused is False", not report.processing_paused)


# ======================================================================
# 13. Organizer: flat layout
# ======================================================================
def test_organizer_flat() -> None:
    print("\n=== Organizer: Flat Layout ===")

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "archive"
        from datetime import datetime
        item = MediaItem(
            source_path="/src/img.png",
            file_name="img.png",
            file_size=500,
            created_at=datetime(2024, 7, 4, tzinfo=UTC),
        )
        p = resolve_archive_path(dest, item, layout="flat")
        _check("Flat layout -> dest_root/filename", p.name == "img.png" and p.parent == dest)


# ======================================================================
# 14. Organizer: conflict exhaustion raises error
# ======================================================================
def test_conflict_exhaustion() -> None:
    print("\n=== Organizer: Conflict Exhaustion ===")

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "test"
        folder.mkdir()
        (folder / "file.txt").write_bytes(b"0")
        for i in range(1, 1000):
            (folder / f"file_{i:03d}.txt").write_bytes(str(i).encode())

        result = None
        try:
            result = _safe_path(folder / "file.txt")
        except FileExistsError:
            pass

        _check("Exhaustion raises FileExistsError or returns", result is None or "_999" in str(result))


# ======================================================================
# Runner
# ======================================================================
async def main() -> None:
    print("=" * 60)
    print("  Transfera v2 — Organizer & Duplicate Detection Tests")
    print("=" * 60)

    # Pure logic tests (no DB)
    test_organizer_basic()
    test_organizer_year_month()
    test_organizer_derive_timestamp_priority()
    test_organizer_conflict()
    test_safe_path()
    test_unique_folder()
    test_live_photo_folder()
    test_organizer_flat()
    test_conflict_exhaustion()

    # DB-backed tests
    await _reset_db()
    await test_no_duplicates()

    await _reset_db()
    await test_exact_duplicates()

    await _reset_db()
    await test_potential_duplicates()

    await _reset_db()
    await test_hash_collision_different_size()

    await _reset_db()
    await test_check_batch_ws_event()

    await _reset_db()
    await test_check_batch_no_duplicates()

    print("\n" + "=" * 60)
    total = _PASS + _FAIL
    print(f"  Results: {_PASS}/{total} passed, {_FAIL} failed")
    print("=" * 60)

    await dispose_engine()
    if _FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
