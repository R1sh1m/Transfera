"""
Transfera v2 — File Organizer
Resolves hierarchical destination paths (Year/Month/Day) with conflict
resolution via numerical suffixes.  Live Photo pairs are placed in the
same folder using the image component's timestamp.

Documents live under a sibling tree — ``<dest_root>/Documents/<Kind>/`` —
with the same date-wise skeleton below it. Photos/video/audio are
unchanged directly under ``<dest_root>/``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from backend.config import (
    DOCUMENT_EXTENSIONS,
    DOCUMENTS_DIR_NAME,
    document_kind_for_extension,
)
from backend.database.models import MediaItem

logger = logging.getLogger(__name__)

# Maximum suffix index before giving up (stem_001 .. stem_999)
_MAX_SUFFIX = 999

# Fixed English month names — locale-independent
MONTH_NAMES = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
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
# Document helpers
# ---------------------------------------------------------------------------
def _item_extension(item: MediaItem) -> str:
    """Best-effort lowercase extension for *item* (column first, filename fallback)."""
    ext = (item.extension or "").lower()
    if ext:
        return ext
    try:
        return Path(item.file_name).suffix.lower()
    except Exception:
        return ""


def is_document_item(item: MediaItem) -> bool:
    """Return True when *item* is a document (separate Documents tree)."""
    return _item_extension(item) in DOCUMENT_EXTENSIONS


def document_kind_for_item(item: MediaItem) -> str | None:
    """Kind bucket (``PDFs``, ``Spreadsheets``, ...) for documents, else None."""
    if not is_document_item(item):
        return None
    return document_kind_for_extension(_item_extension(item))


def build_folder_for_item(
    dest_root: Path,
    item: MediaItem,
    dt: datetime | None,
    layout: str,
) -> Path:
    """Document-aware folder builder — Documents/<Kind>/ prefix for docs."""
    return build_folder(dest_root, dt, layout, doc_kind=document_kind_for_item(item))


def is_under_documents(path: Path, dest_root: Path) -> bool:
    """Return True when *path* already lives inside the Documents tree."""
    try:
        path.relative_to(dest_root / DOCUMENTS_DIR_NAME)
        return True
    except ValueError:
        return False


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
    from backend.utils.durability import sanitize_filename

    dt = derive_timestamp(item)
    safe_name = sanitize_filename(item.file_name)

    folder = build_folder_for_item(dest_root, item, dt, layout)
    base = folder / safe_name

    # Conflict resolution — never overwrite
    return _safe_path(base)


def locate_archive_file(
    dest_root: Path,
    item: MediaItem,
    *,
    layout: str = "year/month/day",
    verify_hash: bool = False,
) -> Path | None:
    """
    Locate the existing destination path for *item* under *dest_root*.
    Checks the base path and sequential conflict paths, verifying that
    the file exists and matches the item's file size (and optionally hash).

    When ``verify_hash`` is True and ``item.source_hash`` is set, candidates
    with matching size are hash-verified to avoid size-collision false hits.
    Size-only fast path is kept for hot loops; recovery passes verify_hash=True.
    """
    from backend.utils.durability import sanitize_filename
    from backend.utils.hashing import verify_hash as _verify_hash_fn

    dt = derive_timestamp(item)
    safe_name = sanitize_filename(item.file_name)
    doc_kind = document_kind_for_item(item)
    folder = build_folder(dest_root, dt, layout, doc_kind=doc_kind)
    base = folder / safe_name

    def _matches(p: Path) -> bool:
        try:
            if not p.is_file():
                return False
            if p.stat().st_size != item.file_size:
                return False
            if verify_hash and item.source_hash:
                try:
                    return _verify_hash_fn(p, item.source_hash)
                except OSError:
                    return False
            return True
        except OSError:
            return False

    def _search(base_path: Path) -> Path | None:
        if _matches(base_path):
            return base_path
        stem = base_path.stem
        suffix = base_path.suffix
        parent = base_path.parent
        for i in range(1, _MAX_SUFFIX + 1):
            candidate = parent / f"{stem}_{i:03d}{suffix}"
            if candidate.is_file():
                if _matches(candidate):
                    return candidate
            else:
                # Break early as organizer._safe_path allocates suffixes sequentially.
                break
        return None

    # 1. New-tree location (Documents/<Kind>/... for documents)
    found = _search(base)
    if found is not None:
        return found

    # 2. Legacy fallback: documents imported before the split live in the
    # unified media tree. Probe it so old rows stay resolvable during and
    # after the manual migration.
    if doc_kind is not None:
        legacy_folder = build_folder(dest_root, dt, layout)
        legacy_base = legacy_folder / safe_name
        found = _search(legacy_base)
        if found is not None:
            logger.debug("Located legacy-tree document %s at %s", item.file_name, found)
            return found

    return None


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
    dt = derive_timestamp(image_item)
    return build_folder(dest_root, dt, layout)


# ---------------------------------------------------------------------------
# Folder construction
# ---------------------------------------------------------------------------
def build_folder(
    dest_root: Path,
    dt: datetime | None,
    layout: str,
    *,
    doc_kind: str | None = None,
) -> Path:
    """Build the sub-folder hierarchy based on the chosen layout.

    When *doc_kind* is set, the hierarchy is rooted at
    ``<dest_root>/Documents/<Kind>/``; otherwise directly at *dest_root*.
    """
    base = dest_root / DOCUMENTS_DIR_NAME / doc_kind if doc_kind else dest_root

    if layout.lower() == "flat":
        return base

    if dt is None:
        return base / "_unsorted"

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
        return base / "_unsorted"

    return base / Path(*segments)


# ---------------------------------------------------------------------------
# Timestamp derivation
# ---------------------------------------------------------------------------
def derive_timestamp(item: MediaItem) -> datetime | None:
    """
    Extract the best available timestamp from a MediaItem.

    Priority: date_taken (resolved capture date) > original_capture_time.
    Do NOT fall back to created_at (DB insert time) — that is today's date,
    not the capture date. Files without a resolvable date go to _unsorted/.
    """
    if item.date_taken is not None:
        return item.date_taken
    if item.original_capture_time is not None:
        return item.original_capture_time
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

    raise FileExistsError(f"Cannot resolve conflict for {base.name}: all slots {_MAX_SUFFIX} exhausted.")


def claim_archive_path(
    dest_root: Path,
    item: MediaItem,
    *,
    layout: str = "year/month/day",
) -> Path:
    """
    Atomically claim a free destination path via O_CREAT|O_EXCL reservation.

    Closes the TOCTOU between parallel Hop-2 workers: the ``.partial``
    reservation file is created exclusively; the winner keeps the slot,
    losers advance to the next suffix. Returns the clean (non-partial)
    path whose ``.partial`` sibling is reserved by the caller.
    """
    import os

    from backend.config import PARTIAL_SUFFIX
    from backend.utils.durability import sanitize_filename

    dt = derive_timestamp(item)
    safe_name = sanitize_filename(item.file_name)
    folder = build_folder_for_item(dest_root, item, dt, layout)
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / safe_name

    candidates: list[Path] = [base]
    stem = base.stem
    suffix = base.suffix
    parent = base.parent
    for i in range(1, _MAX_SUFFIX + 1):
        candidates.append(parent / f"{stem}_{i:03d}{suffix}")

    last_exc: Exception | None = None
    for clean in candidates:
        if clean.exists():
            continue
        reservation = clean.with_suffix(clean.suffix + PARTIAL_SUFFIX)
        if reservation.exists():
            continue
        try:
            fd = os.open(str(reservation), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            # Reservation created on disk — caller holds this slot until copy completes
            # or fails. Do NOT unlink here: keeping it guarantees mutual exclusion against
            # concurrent workers attempting to claim the same filename.
            return clean
        except FileExistsError as exc:
            last_exc = exc
            continue
        except OSError as exc:
            last_exc = exc
            continue

    raise FileExistsError(f"Cannot resolve conflict for {base.name}: all slots {_MAX_SUFFIX} exhausted.") from last_exc


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
