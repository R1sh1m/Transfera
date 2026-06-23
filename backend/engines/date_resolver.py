"""
Transfera v2 — Date Resolver
Single source of truth for determining which date a media item belongs to.

Fallback chain:
  1. EXIF/metadata "date_taken" (if sane)
  2. Filesystem "mtime" (if sane)
  3. None → item goes to Unsorted

Sanity check: date must be >= 1995-01-01 AND <= now (+ 1 day tolerance).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# Earliest plausible date for digital photography
_MIN_DATE = datetime(1995, 1, 1, tzinfo=UTC)

# Tolerance for future dates (clock skew, etc.)
_FUTURE_TOLERANCE = timedelta(days=1)


def is_date_sane(dt: datetime | None) -> bool:
    """Return True if the datetime is plausible for a 'date taken' value."""
    if dt is None:
        return False
    # Ensure timezone-aware for comparison
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return _MIN_DATE <= dt <= (now + _FUTURE_TOLERANCE)


def resolve_item_date(
    date_taken: datetime | None,
    date_modified: datetime | None,
) -> tuple[datetime | None, str | None]:
    """
    Resolve the best date for a media item using the defined fallback chain.

    Returns (date, source) where source is one of:
      "exif"          — EXIF/metadata date_taken was sane
      "file_modified" — filesystem mtime was sane (date_taken missing/bad)
      None            — no sane date found (item should go to Unsorted)
    """
    # 1. Try EXIF date_taken
    if is_date_sane(date_taken):
        return date_taken, "exif"

    # 2. Try filesystem mtime
    if is_date_sane(date_modified):
        return date_modified, "file_modified"

    # 3. No sane date — unsorted
    if date_taken is not None and not is_date_sane(date_taken):
        logger.debug(
            "Rejecting unsane EXIF date: %s (before min or future)", date_taken
        )
    if date_modified is not None and not is_date_sane(date_modified):
        logger.debug(
            "Rejecting unsane mtime: %s (before min or future)", date_modified
        )

    return None, None
