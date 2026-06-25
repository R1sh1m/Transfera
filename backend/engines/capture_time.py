"""
Transfera v2 — Capture Time Extractor

Extracts the original capture datetime from a media file using:
- Pillow EXIF for images (with multi-strategy extraction)
- ffprobe for videos
- Filesystem mtime as final fallback
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Register pillow-heif opener so Image.open() can decode HEIC/HEIF files
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    logger.debug("pillow-heif registered for HEIC/HEIF support")
except ImportError:
    logger.warning(
        "pillow-heif not installed — HEIC files will fall back to mtime. "
        "Install with: pip install pillow-heif"
    )

_IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".webp", ".tiff", ".tif", ".bmp", ".avif", ".jxl"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".webm", ".m4v", ".wmv"}


def _extract_image_capture_time(file_path: Path) -> datetime | None:
    """Extract capture datetime from image EXIF using multiple strategies.

    Strategy chain:
      1. ``img._getexif()`` — JPEG fast path.
      2. ``img.getexif()`` — modern Pillow API (works on JPEG, TIFF, WEBP,
         and some AVIF/HEIC via format plugins).
      3. ``exifread`` — third-party library for exotic / non-Pillow EXIF.
      4. ExifTool — handles HEIC natively even without pillow-heif.
      5. Returns ``None`` (caller falls through to mtime).

    Never raises.
    """
    # Strategy 1-3: Pillow-based extraction
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(file_path) as img:
            raw = _get_exif_datetime_str(img, file_path)
            if raw is not None:
                return _parse_exif_datetime_str(raw)
    except Exception as exc:
        logger.debug("Pillow open failed for %s: %s", file_path, exc)

    # Strategy 4: ExifTool fallback — handles HEIC natively even when
    # pillow-heif is not installed or Pillow cannot decode the format.
    try:
        from backend.engines.metadata_extractor import extract_metadata

        meta = extract_metadata(file_path)
        if meta and meta.date_taken:
            return meta.date_taken
    except Exception as exc:
        logger.debug("ExifTool fallback failed for %s: %s", file_path, exc)

    return None


def _get_exif_datetime_str(img, file_path: Path) -> str | None:
    """Extract the raw EXIF datetime string via multiple backends."""
    fmt = img.format
    exif = None

    if fmt == "JPEG":
        exif = _try_getexif(img, file_path)

    if exif is None:
        exif = _try_getexif_modern(img, fmt, file_path)

    if exif is not None:
        raw = exif.get(36867) or exif.get(36868) or exif.get(306)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()

    raw = _try_exifread(img, file_path)
    if raw is not None:
        return raw

    return None


def _try_getexif(img, file_path: Path):
    """JPEG _getexif fast path."""
    try:
        exif = img._getexif()
        if exif:
            return exif
    except AttributeError:
        logger.warning(
            "_getexif() unexpectedly unavailable for JPEG: %s", file_path
        )
    except Exception as exc:
        logger.warning("_getexif() failed for JPEG %s: %s", file_path, exc)
    return None


def _try_getexif_modern(img, fmt: str | None, file_path: Path):
    """Modern Pillow getexif() API — works on more formats than _getexif."""
    try:
        exif = img.getexif()
        if exif:
            return exif
    except Exception as exc:
        logger.debug("getexif() failed for %s %s: %s", fmt, file_path, exc)
    return None


def _try_exifread(img, file_path: Path) -> str | None:
    """Fallback EXIF via exifread library (AVIF / HEIC / exotic formats)."""
    raw_bytes = img.info.get("exif")
    if not raw_bytes:
        return None
    try:
        import io

        import exifread

        tags = exifread.process_file(io.BytesIO(raw_bytes), details=False)
        for tag_name in ("EXIF DateTimeOriginal", "Image DateTime"):
            tag = tags.get(tag_name)
            if tag:
                val = str(tag).strip()
                if val:
                    return val
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("exifread fallback failed for %s: %s", file_path, exc)
    return None


def _parse_exif_datetime_str(raw: str) -> datetime | None:
    """Parse an EXIF datetime string into a timezone-aware datetime.

    EXIF DateTimeOriginal is always local time at the capture location with
    NO timezone information. We return a datetime using the local system
    timezone so callers that need POSIX timestamps compute them correctly.
    Formats that include an explicit timezone offset are parsed as-is.
    """
    tz_formats = ("%Y:%m:%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z")
    for fmt in tz_formats:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    local_formats = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")
    for fmt in local_formats:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.astimezone()
        except ValueError:
            continue

    return None


def _extract_video_capture_time(file_path: Path) -> datetime | None:
    """Extract creation_time from video metadata using ExifTool then ffprobe."""
    # 1. Try ExifTool for DateTimeOriginal or CreateDate (handles .mov, .m4v, .3gp better)
    try:
        from backend.engines.metadata_extractor import extract_metadata_batch
        results = extract_metadata_batch([file_path])
        meta = results.get(str(file_path.resolve()))
        if meta and meta.date_taken:
            return meta.date_taken
        if meta and meta.date_created:
            return meta.date_created
    except Exception as exc:
        logger.debug("ExifTool video fallback failed for %s: %s", file_path.name, exc)

    # 2. ffprobe as secondary
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_entries", "format_tags=creation_time",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        raw = data.get("format", {}).get("tags", {}).get("creation_time")
        if not raw:
            return None

        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                continue
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, FileNotFoundError) as exc:
        logger.warning("ffprobe failed for %s: %s", file_path, exc)
    return None


def extract_capture_datetime(file_path: str | Path) -> datetime:
    """
    Extract the original capture datetime from a media file.

    Priority chain:
      1. Image EXIF: DateTimeOriginal (36867) -> DateTime (306)
      2. Video: ffprobe creation_time tag
      3. Filesystem mtime of the source file
      4. Current UTC time (final fallback)

    Always returns a value — never raises.
    """
    path = Path(file_path).resolve()
    ext = path.suffix.lower()

    dt: datetime | None = None

    if ext in _IMAGE_EXTS:
        dt = _extract_image_capture_time(path)
    elif ext in _VIDEO_EXTS:
        dt = _extract_video_capture_time(path)

    if dt is not None:
        return dt

    try:
        mtime = os.path.getmtime(path)
        return datetime.fromtimestamp(mtime, tz=UTC)
    except OSError:
        logger.warning("Could not read mtime for %s — using current time", path)

    return datetime.now(UTC)
