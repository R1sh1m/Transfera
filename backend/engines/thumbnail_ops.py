"""
Transfera v2 — Thumbnail database operations.

Provides public functions for marking thumbnail status in the database and
resolving the source file path for thumbnail generation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.engines.organizer import locate_archive_file

logger = logging.getLogger(__name__)


async def mark_thumbnail_ready(item_id: int) -> None:
    """Set thumbnail_path sentinel so frontend knows the thumbnail is in cache."""
    from backend.database.manager import session_scope
    from backend.database.models import MediaItem
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
        if db_item is not None:
            db_item.thumbnail_path = "memory"
            db_item.thumbnail_status = "ready"
            db_item.touch()


async def mark_thumbnail_failed(item_id: int) -> None:
    """Mark a media item's thumbnail as failed so the frontend stops retrying."""
    from backend.database.manager import session_scope
    from backend.database.models import MediaItem
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
        if db_item is not None:
            db_item.thumbnail_status = "failed"
            db_item.touch()


async def set_item_thumbnail(item_id: int, thumbnail_path: str) -> None:
    """Update a single item's thumbnail_path in the database."""
    from backend.database.manager import session_scope
    from backend.database.models import MediaItem
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item_id)
        if db_item is not None:
            db_item.thumbnail_path = thumbnail_path
            db_item.touch()


def resolve_thumbnail_source_path(
    entry: dict,
    dest_root: Path | None,
) -> Path | None:
    """Resolve the best available file path for thumbnail generation.

    Tries the organised archive destination first, then the local Hop 1
    cache, and falls back to the original source path.
    """
    from backend.config import CACHE_DIR
    from backend.database.models import MediaItem
    from backend.engines.cache_manager import get_cache_path

    file_path: Path | None = None
    source_path = entry.get("source_path", "")
    file_name = entry.get("file_name", "")

    # Try destination path first (the final organised copy)
    if dest_root is not None:
        stub = MediaItem(
            date_taken=entry.get("date_taken"),
            original_capture_time=entry.get("original_capture_time"),
            created_at=entry.get("created_at"),
            file_name=file_name,
            file_size=entry.get("file_size", 0),
        )
        dst = locate_archive_file(dest_root, stub, layout=entry.get("folder_layout", "year/month"))
        if dst is not None:
            file_path = dst

    # Try local cache path (Hop 1 cache)
    if file_path is None:
        try:
            cache_file = get_cache_path(CACHE_DIR, source_path, file_name)
            if cache_file.is_file():
                file_path = cache_file
        except Exception:
            pass

    # Fall back to source path (original file still on disk)
    if file_path is None:
        src = Path(source_path)
        if src.is_file():
            file_path = src

    return file_path
