"""
Transfera v2 — File Organizer
Resolves hierarchical destination paths (Year/Month/Day) with conflict
resolution via numerical suffixes.  Live Photo pairs are placed in the
same folder using the image component's timestamp.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from backend.database.models import MediaItem

logger = logging.getLogger(__name__)

# Maximum suffix index before giving up (stem_001 .. stem_999)
_MAX_SUFFIX = 999

# Fixed English month names — locale-independent
MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def format_month_folder(dt: datetime) -> str:
    """Format a month folder name as ``MM-MonthName`` (e.g. ``04-April``)."""
    return f"{dt.month:02d}-{MONTH_NAMES[dt.month]}"


def parse_month_folder(name: str) -> int | None:
    """
    Parse a month folder name and return the month number (1-12), or None.

    Recognizes three formats used across Transfera versions:
    - ``MM-MonthName`` (current): ``04-April``
    - ``MonthName(MM)`` (prior):  ``April(04)``
    - ``MM`` (original):          ``04``
    """
    # Current format: MM-MonthName
    if len(name) >= 3 and name[2] == "-" and name[:2].isdigit():
        month = int(name[:2])
        if 1 <= month <= 12:
            return month

    # Prior format: MonthName(MM) — e.g. "April(04)"
    paren = name.rfind("(")
    if paren != -1 and name.endswith(")"):
        inside = name[paren + 1 : -1]
        if inside.isdigit():
            month = int(inside)
            if 1 <= month <= 12:
                return month

    # Original format: plain MM
    if name.isdigit() and len(name) <= 2:
        month = int(name)
        if 1 <= month <= 12:
            return month

    return None


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
            segments.append(format_month_folder(dt))
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

    Priority: date_taken (resolved capture date) > original_capture_time >
    created_at (DB insert time).
    """
    if item.date_taken is not None:
        return item.date_taken
    if item.original_capture_time is not None:
        return item.original_capture_time
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
