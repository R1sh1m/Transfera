"""
Transfera v2 — Scanner Test Suite
Verifies chronological sorting, Live Photo UUID grouping, and dedup behaviour.
Run: python -m backend.tests.test_scanner
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure repo root is on sys.path when running directly.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from datetime import UTC

from backend.database.manager import create_all_tables, dispose_engine, get_engine  # noqa: E402
from backend.database.models import Base, HopStatus, MediaItem  # noqa: E402
from backend.engines.metadata_extractor import FileMetadata  # noqa: E402
from backend.engines.scanner import (  # noqa: E402
    _detect_live_photo_groups_from_paths,
    _normalise_stem,
    _sort_key,
    scan,
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
            msg += f"  — {detail}"
        print(msg)


# ======================================================================
# Helpers: create temp files with controlled mtimes
# ======================================================================
def _touch(path: Path, content: bytes = b"x", mtime: float | None = None) -> Path:
    """Create a file and optionally override its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _ts(year: int, month: int, day: int) -> float:
    """Shorthand: (year, month, day) → POSIX timestamp (UTC)."""
    import calendar
    return calendar.timegm((year, month, day, 12, 0, 0, 0, 0, 0))


# ======================================================================
# 1. Live Photo group detection (pure logic, no DB)
# ======================================================================
def test_live_photo_groups() -> None:
    print("\n=== Live Photo Group Detection ===")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)

        # Case 1: classic Live Photo pair (HEIC + MOV, same stem)
        img = _touch(root / "photo.HEIC", b"img")
        vid = _touch(root / "photo.mov", b"vid")
        groups = _detect_live_photo_groups_from_paths([img, vid])
        g1 = groups.get(str(img.resolve()))
        g2 = groups.get(str(vid.resolve()))
        _check("Live pair gets same UUID", g1 is not None and g1 == g2)

        # Case 2: case-insensitive stem matching
        img2 = _touch(root / "Vacation.JPG", b"i")
        vid2 = _touch(root / "vacation.mp4", b"v")
        groups2 = _detect_live_photo_groups_from_paths([img2, vid2])
        g3 = groups2.get(str(img2.resolve()))
        g4 = groups2.get(str(vid2.resolve()))
        _check("Case-insensitive stem match", g3 is not None and g3 == g4)

        # Case 3: image-only → no group
        solo = _touch(root / "solo.png", b"s")
        groups3 = _detect_live_photo_groups_from_paths([solo])
        _check("Image-only gets no group", str(solo.resolve()) not in groups3)

        # Case 4: video-only → no group
        vsolo = _touch(root / "clip.mp4", b"v")
        groups4 = _detect_live_photo_groups_from_paths([vsolo])
        _check("Video-only gets no group", str(vsolo.resolve()) not in groups4)

        # Case 5: three files (image + video + doc) → image+video grouped
        img3 = _touch(root / "trip.jpg", b"i")
        vid3 = _touch(root / "trip.mp4", b"v")
        doc = _touch(root / "trip.txt", b"d")  # not media
        groups5 = _detect_live_photo_groups_from_paths([img3, vid3, doc])
        g5 = groups5.get(str(img3.resolve()))
        g6 = groups5.get(str(vid3.resolve()))
        _check("Three-file group: image+video share UUID", g5 is not None and g5 == g6)
        _check("Three-file group: txt excluded", str(doc.resolve()) not in groups5)

        # Case 6: UUID uniqueness across groups
        img_a = _touch(root / "a/a.jpg", b"a")
        vid_a = _touch(root / "a/a.mp4", b"a")
        img_b = _touch(root / "b/b.jpg", b"b")
        vid_b = _touch(root / "b/b.mp4", b"b")
        groups6 = _detect_live_photo_groups_from_paths([img_a, vid_a, img_b, vid_b])
        ga1 = groups6.get(str(img_a.resolve()))
        ga2 = groups6.get(str(vid_a.resolve()))
        gb1 = groups6.get(str(img_b.resolve()))
        gb2 = groups6.get(str(vid_b.resolve()))
        _check("Distinct dirs get distinct UUIDs", ga1 == ga2 and gb1 == gb2 and ga1 != gb1)


# ======================================================================
# 2. Sort key logic
# ======================================================================
def test_sort_key() -> None:
    print("\n=== Sort Key Logic ===")

    EPOCH = __import__("datetime").datetime(1970, 1, 1, tzinfo=__import__("datetime").timezone.utc)

    m1 = FileMetadata(file_path="/a", file_name="a", file_size=1, extension=".jpg",
                       date_taken=None, date_created=None, date_modified=None)
    _check("No dates -> epoch", _sort_key(m1) == EPOCH)

    from datetime import datetime
    dt_mod = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
    m2 = FileMetadata(file_path="/b", file_name="b", file_size=1, extension=".jpg",
                       date_taken=None, date_created=None, date_modified=dt_mod)
    _check("Only date_modified used", _sort_key(m2) == dt_mod)

    dt_create = datetime(2024, 1, 1, tzinfo=UTC)
    m3 = FileMetadata(file_path="/c", file_name="c", file_size=1, extension=".jpg",
                       date_taken=None, date_created=dt_create, date_modified=dt_mod)
    _check("date_created preferred over date_modified", _sort_key(m3) == dt_create)

    dt_taken = datetime(2023, 3, 10, tzinfo=UTC)
    m4 = FileMetadata(file_path="/d", file_name="d", file_size=1, extension=".jpg",
                       date_taken=dt_taken, date_created=dt_create, date_modified=dt_mod)
    _check("date_taken preferred over all", _sort_key(m4) == dt_taken)


# ======================================================================
# 3. Normalise stem
# ======================================================================
def test_normalise_stem() -> None:
    print("\n=== Normalise Stem ===")
    _check("lowercased", _normalise_stem("Photo") == "photo")
    _check("stripped", _normalise_stem("  img  ") == "img")
    _check("empty", _normalise_stem("") == "")


# ======================================================================
# 4. Full scan with DB integration
# ======================================================================
@pytest.mark.asyncio
async def test_full_scan() -> None:
    print("\n=== Full Scan Integration ===")

    # Drop & recreate tables for a clean slate
    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)

        # Create files with DISTINCT dates so we can verify sort order
        #   oldest: 2020-01-01
        #   middle: 2022-06-15
        #   newest: 2025-12-25
        f_old = _touch(root / "old_photo.jpg", b"old", mtime=_ts(2020, 1, 1))
        f_mid = _touch(root / "mid_video.mp4", b"mid", mtime=_ts(2022, 6, 15))
        f_new = _touch(root / "new_image.png", b"new", mtime=_ts(2025, 12, 25))

        # Live Photo pair: same stem, one image + one video
        lp_img = _touch(root / "live.HEIC", b"lp", mtime=_ts(2023, 8, 10))
        lp_vid = _touch(root / "live.mov", b"lv", mtime=_ts(2023, 8, 10))

        ids = await scan(root)
        _check("Scan returned 5 IDs", len(ids) == 5)

        # Verify chronological order by reading back from DB
        from sqlalchemy import select

        from backend.database.manager import session_scope
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem).order_by(MediaItem.id)
            )
            items = result.scalars().all()

        file_names = [i.file_name for i in items]
        _check("Order: old first", file_names[0] == "old_photo.jpg")
        _check("Order: mid second", file_names[1] == "mid_video.mp4")
        _check("Order: live pair before new", file_names.index("live.HEIC") < file_names.index("new_image.png"))
        _check("Order: new last", file_names[-1] == "new_image.png")

        # Live Photo group UUIDs
        lp_items = [i for i in items if i.live_photo_group is not None]
        _check("Two Live Photo items", len(lp_items) == 2)
        if len(lp_items) == 2:
            _check("Live Photo pair shares UUID", lp_items[0].live_photo_group == lp_items[1].live_photo_group)
            _check("UUID is valid", len(lp_items[0].live_photo_group) == 36)

        # Verify all hop1_status == SCANNED
        all_scanned = all(i.hop1_status == HopStatus.SCANNED.value for i in items)
        _check("All items hop1=SCANNED", all_scanned)


# ======================================================================
# 5. Dedup test: re-scan should not duplicate
# ======================================================================
@pytest.mark.asyncio
async def test_dedup() -> None:
    print("\n=== Dedup (Re-Scan) ===")

    # Fresh tables for this test
    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _touch(root / "dup.jpg", b"dup", mtime=_ts(2021, 5, 5))

        # First scan
        ids1 = await scan(root)
        _check("First scan returns 1 ID", len(ids1) == 1)

        # Second scan — same files
        ids2 = await scan(root)
        _check("Second scan returns same ID (no duplicate)", ids1 == ids2)

        # Verify only 1 row in DB
        from sqlalchemy import func as sql_func
        from sqlalchemy import select

        from backend.database.manager import session_scope
        async with session_scope() as session:
            result = await session.execute(select(sql_func.count(MediaItem.id)))
            count = result.scalar()
        _check("DB still has 1 row", count == 1)


# ======================================================================
# 6. Single-file scan
# ======================================================================
@pytest.mark.asyncio
async def test_single_file_scan() -> None:
    print("\n=== Single File Scan ===")

    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        f = _touch(Path(tmp) / "single.jpg", b"s", mtime=_ts(2019, 12, 31))
        ids = await scan(f)
        _check("Single file scan returns 1 ID", len(ids) == 1)


# ======================================================================
# 7. Empty directory scan
# ======================================================================
@pytest.mark.asyncio
async def test_empty_dir_scan() -> None:
    print("\n=== Empty Directory Scan ===")

    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all_tables()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ids = await scan(tmp)
        _check("Empty dir returns 0 IDs", len(ids) == 0)


# ======================================================================
# Runner
# ======================================================================
async def main() -> None:
    print("=" * 60)
    print("  Transfera v2 — Scanner Test Suite")
    print("=" * 60)

    # Pure-logic tests (no DB)
    test_normalise_stem()
    test_sort_key()
    test_live_photo_groups()

    # DB-backed integration tests
    await test_full_scan()
    await test_dedup()
    await test_single_file_scan()
    await test_empty_dir_scan()

    print("\n" + "=" * 60)
    total = _PASS + _FAIL
    print(f"  Results: {_PASS}/{total} passed, {_FAIL} failed")
    print("=" * 60)

    await dispose_engine()
    if _FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
