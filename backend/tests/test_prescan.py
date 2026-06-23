"""
Transfera v2 — Pre-scan Duplicate Detection Tests
Tests prescan_against_library() which does hash-free (filename + size) matching.
Run: python -m pytest backend/tests/test_prescan.py -v
"""

from __future__ import annotations

import pytest

from backend.database.models import HopStatus, MediaItem
from backend.engines.duplicate_detector import prescan_against_library


@pytest.mark.asyncio
async def test_prescan_matches_by_filename_and_size(db_session):
    """Insert a COMPLETED MediaItem, then prescan with matching and
    non-matching candidates — only the match should be flagged."""

    # Insert a completed media item
    item = MediaItem(
        source_path="/src/existing_photo.jpg",
        file_name="photo.jpg",
        file_size=1024000,
        final_status=HopStatus.COMPLETED.value,
    )
    db_session.add(item)
    await db_session.commit()

    candidates = [
        {"abs_path": "/device/photo.jpg", "filename": "photo.jpg", "size_bytes": 1024000},
        {"abs_path": "/device/other.png", "filename": "other.png", "size_bytes": 512000},
    ]

    result = await prescan_against_library(candidates)

    assert result["checked"] == 2
    assert result["likely_duplicate_count"] == 1
    assert "/device/photo.jpg" in result["likely_duplicate_paths"]
    assert "/device/other.png" not in result["likely_duplicate_paths"]


@pytest.mark.asyncio
async def test_prescan_empty_candidates(db_session):
    """Empty candidate list returns zero matches."""
    result = await prescan_against_library([])
    assert result["checked"] == 0
    assert result["likely_duplicate_count"] == 0
    assert result["likely_duplicate_paths"] == []


@pytest.mark.asyncio
async def test_prescan_case_insensitive_match(db_session):
    """Filename matching is case-insensitive."""
    item = MediaItem(
        source_path="/src/Photo.JPG",
        file_name="Photo.JPG",
        file_size=2048000,
        final_status=HopStatus.COMPLETED.value,
    )
    db_session.add(item)
    await db_session.commit()

    candidates = [
        {"abs_path": "/device/photo.jpg", "filename": "photo.jpg", "size_bytes": 2048000},
    ]

    result = await prescan_against_library(candidates)
    assert result["likely_duplicate_count"] == 1
    assert "/device/photo.jpg" in result["likely_duplicate_paths"]


@pytest.mark.asyncio
async def test_prescan_only_completed_items(db_session):
    """Only COMPLETED items in the library should match."""
    # Add a non-completed item (should NOT match)
    item = MediaItem(
        source_path="/src/pending.mp4",
        file_name="video.mp4",
        file_size=5000000,
        final_status=HopStatus.PENDING.value,
    )
    db_session.add(item)
    await db_session.commit()

    candidates = [
        {"abs_path": "/device/video.mp4", "filename": "video.mp4", "size_bytes": 5000000},
    ]

    result = await prescan_against_library(candidates)
    assert result["likely_duplicate_count"] == 0
