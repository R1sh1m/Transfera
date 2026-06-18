"""
Transfera v2 — Metadata Extractor
ExifTool subprocess integration with automated bootstrapper and filesystem fallback.

Bootstrapper priority:
  1. Local app-relative binary (backend/bin/exiftool/exiftool.exe)
  2. Global system PATH lookup
  3. Auto-download from official distribution (Windows only, with full error handling)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from backend.config import EXIFTOOL_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EXIFTOOL_EXE_NAME = "exiftool.exe" if sys.platform == "win32" else "exiftool"
_LOCAL_EXIFTOOL: Path = EXIFTOOL_DIR / _EXIFTOOL_EXE_NAME

# Official ExifTool Windows zip download page
_EXIFTOOL_HOME = "https://exiftool.org"
_EXIFTOOL_ZIP_PATTERN = "exiftool-{ver}_?\\.zip"

# Network protections
_CONNECT_TIMEOUT = 10  # seconds
_READ_TIMEOUT = 60  # seconds
_DOWNLOAD_CHUNK = 8192  # bytes


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class FileMetadata:
    """Normalised metadata for a single media file."""

    file_path: str
    file_name: str
    file_size: int
    extension: str
    mime_type: Optional[str] = None

    # Prioritised date fields (oldest -> newest preference in the scanner)
    date_taken: Optional[datetime] = None  # EXIF DateTimeOriginal
    date_created: Optional[datetime] = None  # EXIF CreateDate / filesystem ctime
    date_modified: Optional[datetime] = None  # EXIF ModifyDate / filesystem mtime

    # Raw EXIF tags (if available)
    exif_tags: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
_EXIF_DATETIME_FORMATS: list[str] = [
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f%z",
]


def _parse_exif_datetime(raw: str | None) -> datetime | None:
    """Attempt to parse an EXIF datetime string into a timezone-aware datetime."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    for fmt in _EXIF_DATETIME_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _ts_to_datetime(ts: float) -> datetime:
    """Convert a POSIX timestamp to a UTC datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ---------------------------------------------------------------------------
# ExifTool Bootstrapper
# ---------------------------------------------------------------------------
# Resolved absolute path to the ExifTool binary; None until bootstrap runs.
_resolved_exiftool: str | None = None
_bootstrap_done = False


def _bootstrap_exiftool() -> str | None:
    """
    Resolve the ExifTool binary path using a three-tier fallback:

      1. Local app-relative directory (backend/bin/exiftool/)
      2. System PATH via shutil.which()
      3. Auto-download from official source (Windows only)

    Returns the absolute path string on success, or None if unavailable.
    """
    global _resolved_exiftool, _bootstrap_done
    if _bootstrap_done:
        return _resolved_exiftool
    _bootstrap_done = True

    # Tier 1: Local binary
    if _LOCAL_EXIFTOOL.is_file():
        logger.info("ExifTool found locally at %s", _LOCAL_EXIFTOOL)
        _resolved_exiftool = str(_LOCAL_EXIFTOOL)
        return _resolved_exiftool

    # Tier 2: System PATH
    path_loc = shutil.which("exiftool")
    if path_loc:
        logger.info("ExifTool found on system PATH at %s", path_loc)
        _resolved_exiftool = path_loc
        return _resolved_exiftool

    logger.info(
        "ExifTool not found locally or on PATH -- attempting automated download"
    )

    # Tier 3: Auto-download (Windows only)
    if sys.platform != "win32":
        logger.warning(
            "Auto-download only supported on Windows; "
            "ExifTool unavailable -- falling back to filesystem timestamps"
        )
        return None

    try:
        downloaded = _download_exiftool()
        if downloaded and downloaded.is_file():
            _resolved_exiftool = str(downloaded)
            logger.info("ExifTool bootstrapped successfully at %s", _resolved_exiftool)
            return _resolved_exiftool
    except Exception:
        logger.warning(
            "ExifTool auto-download failed -- falling back to filesystem timestamps",
            exc_info=True,
        )

    return None


def _fetch_latest_version() -> str | None:
    """
    Scrape the ExifTool homepage to find the latest Windows zip filename.
    Returns the version string (e.g. '12.97') or None on failure.
    """
    import re

    try:
        req = Request(
            _EXIFTOOL_HOME,
            headers={"User-Agent": "Transfera/2.0"},
        )
        with urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Match: exiftool-12.97.zip or exiftool-12_97.zip
        pattern = re.compile(r"exiftool[_-](\d+\.\d+(?:_\d+)?)\.zip", re.IGNORECASE)
        matches = pattern.findall(html)
        if not matches:
            return None

        # Pick the first (latest) match; normalise underscores to dots
        ver = matches[0].replace("_", ".")
        return ver
    except (URLError, OSError, ValueError):
        return None


def _download_exiftool() -> Path | None:
    """
    Download the official ExifTool Windows zip, extract exiftool.exe,
    and clean up the archive. Returns the path to the extracted binary.
    """
    import re
    import tempfile

    version = _fetch_latest_version()
    if not version:
        logger.warning("Could not determine latest ExifTool version from homepage")
        return None

    # Build download URL (version with underscore for zip filename)
    ver_underscore = version.replace(".", "_")
    zip_name = f"exiftool-{ver_underscore}.zip"
    url = f"{_EXIFTOOL_HOME}/{zip_name}"
    logger.info("Downloading ExifTool %s from %s", version, url)

    tmp_dir = Path(tempfile.mkdtemp(prefix="exiftool_dl_"))
    zip_path = tmp_dir / zip_name

    try:
        req = Request(url, headers={"User-Agent": "Transfera/2.0"})
        with urlopen(req, timeout=_READ_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(zip_path, "wb") as fh:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total and downloaded % (1024 * 1024) == 0:
                        pct = (downloaded / total) * 100 if total else 0
                        logger.debug(
                            "Download progress: %d/%d bytes (%.0f%%)",
                            downloaded, total, pct,
                        )

        logger.info(
            "Download complete: %d bytes -- extracting", zip_path.stat().st_size
        )

        return _extract_from_zip(zip_path)

    except (URLError, OSError, TimeoutError) as exc:
        logger.warning("ExifTool download failed: %s", exc)
        return None
    finally:
        # Always clean up the temp directory and zip
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _extract_from_zip(zip_path: Path) -> Path | None:
    """
    Extract exiftool.exe from the downloaded zip into the local bin directory.
    The official zip contains a single directory with exiftool.exe at its root.
    """
    EXIFTOOL_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Find the exiftool executable inside the archive
            exe_names = [
                n for n in zf.namelist()
                if n.lower().endswith(_EXIFTOOL_EXE_NAME.lower())
            ]
            if not exe_names:
                logger.warning(
                    "ExifTool executable not found inside zip archive"
                )
                return None

            # Extract the executable
            exe_entry = exe_names[0]
            target = EXIFTOOL_DIR / _EXIFTOOL_EXE_NAME

            with zf.open(exe_entry) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)

            # Mark as executable on Unix-like systems
            if sys.platform != "win32":
                target.chmod(0o755)

            logger.info("Extracted %s -> %s", exe_entry, target)
            return target

    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Failed to extract ExifTool zip: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ExifTool extraction
# ---------------------------------------------------------------------------

def _build_exiftool_cmd() -> list[str]:
    """Build the ExifTool command list using the resolved binary path."""
    exe = _bootstrap_exiftool()
    if not exe:
        return []
    return [
        exe,
        "-json",
        "-time:all",
        "-s3",
        "-charset",
        "utf8",
    ]


def _extract_via_exiftool(file_path: Path) -> FileMetadata:
    """Run ExifTool and parse its JSON output into a FileMetadata."""
    cmd = _build_exiftool_cmd()
    if not cmd:
        return _extract_via_filesystem(file_path)

    try:
        result = subprocess.run(
            cmd + [str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            logger.warning("ExifTool returned %d for %s", result.returncode, file_path)
            return _extract_via_filesystem(file_path)

        data = json.loads(result.stdout)
        if not data:
            return _extract_via_filesystem(file_path)

        tags = data[0] if isinstance(data, list) else data

        # Extract timestamps -- ExifTool returns ISO-ish strings with -time:all
        date_taken = _parse_exif_datetime(tags.get("DateTimeOriginal"))
        date_created = _parse_exif_datetime(tags.get("CreateDate"))
        date_modified = _parse_exif_datetime(tags.get("ModifyDate"))

        stat = file_path.stat()
        return FileMetadata(
            file_path=str(file_path.resolve()),
            file_name=file_path.name,
            file_size=stat.st_size,
            extension=file_path.suffix.lower(),
            mime_type=tags.get("MIMEType"),
            date_taken=date_taken,
            date_created=date_created or _ts_to_datetime(stat.st_ctime),
            date_modified=date_modified or _ts_to_datetime(stat.st_mtime),
            exif_tags={k: str(v) for k, v in tags.items() if v},
        )
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "ExifTool failed for %s: %s -- using filesystem fallback",
            file_path, exc,
        )
        return _extract_via_filesystem(file_path)


# ---------------------------------------------------------------------------
# Filesystem fallback
# ---------------------------------------------------------------------------
def _extract_via_filesystem(file_path: Path) -> FileMetadata:
    """Derive metadata purely from the OS filesystem."""
    stat = file_path.stat()
    mtime = _ts_to_datetime(stat.st_mtime)
    # On Windows st_ctime is "creation time" (not change time) and os.utime
    # cannot set it, so we mirror mtime for date_created to keep sort stable.
    return FileMetadata(
        file_path=str(file_path.resolve()),
        file_name=file_path.name,
        file_size=stat.st_size,
        extension=file_path.suffix.lower(),
        mime_type=None,
        date_taken=None,
        date_created=mtime,
        date_modified=mtime,
        exif_tags={},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_metadata(file_path: str | Path) -> FileMetadata:
    """
    Extract metadata for a single file.

    Uses ExifTool when available; falls back to filesystem timestamps.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    if _bootstrap_exiftool():
        return _extract_via_exiftool(path)
    return _extract_via_filesystem(path)
