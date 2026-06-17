"""
MediaVault v2 — File Organizer
Resolves hierarchical destination paths (Year/Month/Day) with conflict
resolution via numerical suffixes.  Live Photo pairs are placed in the
same folder using the image component's timestamp.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.config import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from backend.database.models import MediaItem

logger = logging.getLogger(__name__)

# Maximum suffix index before giving up (stem_001 .. stem_999)
_MAX_SUFFIX = 999


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def resolve_archive_path(
    dest_root: Path,
    item: MediaItem,
    *,
    layout: str = "year/month/day",
) -> Path:
    """
    Compute the final destination path for *item* under *dest_root*.

    Parameters
    ----------
    dest_root : Path
        Root of the organised archive.
    item : MediaItem
        The database record for the file.
    layout : str
        Hierarchical format.  Supported: ``"year/month/day"``,
        ``"year/month"``, ``"flat"``.

    Returns
    -------
    Path
        A **non-existing** path guaranteed safe to write to.
    """
    dt = _derive_timestamp(item)
    name = item.file_name

    folder = _build_folder(dest_root, dt, layout)
    base = folder / name

    # Conflict resolution — never overwrite
    return _safe_path(base)


def resolve_live_photo_folder(
    dest_root: Path,
    image_item: MediaItem,
    video_item: MediaItem,
    *,
    layout: str = "year/month/day",
) -> Path:
    """
    Both components of a Live Photo pair must land in the same folder.
    The folder is derived from the **image** item's timestamp.
    """
    dt = _derive_timestamp(image_item)
    return _build_folder(dest_root, dt, layout)


# ---------------------------------------------------------------------------
# Folder construction
# ---------------------------------------------------------------------------
def _build_folder(
    dest_root: Path,
    dt: datetime | None,
    layout: str,
) -> Path:
    """Build the sub-folder hierarchy based on the chosen layout."""
    if layout.lower() == "flat":
        return dest_root

    if dt is None:
        return dest_root / "_unsorted"

    parts = layout.lower().split("/")
    segments: list[str] = []

    for part in parts:
        if part == "year":
            segments.append(f"{dt.year}")
        elif part == "month":
            segments.append(f"{dt.month:02d}")
        elif part == "day":
            segments.append(f"{dt.day:02d}")

    if not segments:
        return dest_root / "_unsorted"

    return dest_root / Path(*segments)


# ---------------------------------------------------------------------------
# Timestamp derivation
# ---------------------------------------------------------------------------
def _derive_timestamp(item: MediaItem) -> datetime | None:
    """
    Extract the best available timestamp from a MediaItem.

    Priority: created_at (set by the scanner from EXIF/filesystem).
    """
    if item.created_at is not None:
        return item.created_at
    return None


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------
def _safe_path(base: Path) -> Path:
    """
    If *base* does not exist, return it immediately.

    Otherwise append ``_001``, ``_002``, ... up to ``_999``.
    Raises ``FileExistsError`` if no slot is free.
    """
    if not base.exists():
        return base

    stem = base.stem
    suffix = base.suffix
    parent = base.parent

    for i in range(1, _MAX_SUFFIX + 1):
        candidate = parent / f"{stem}_{i:03d}{suffix}"
        if not candidate.exists():
            logger.info("Conflict resolved: %s -> %s", base.name, candidate.name)
            return candidate

    raise FileExistsError(
        f"Cannot resolve conflict for {base.name}: "
        f"all slots {_MAX_SUFFIX} exhausted."
    )


def unique_folder(base: Path) -> Path:
    """
    Ensure *base* is a unique directory.  Appends ``_001`` etc. if needed.
    """
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        return base

    stem = base.name
    parent = base.parent

    for i in range(1, _MAX_SUFFIX + 1):
        candidate = parent / f"{stem}_{i:03d}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate

    raise FileExistsError(f"Cannot resolve unique folder for {base.name}")
