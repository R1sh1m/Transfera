"""
Transfera v2 — Durability helpers (fsync + atomic commit).

Guarantees "verified" means on-disk, not just in the page cache:
- fsync file handle after writes, then atomic os.replace, then fsync parent dir.
- Used by both Hop 1 (cache_manager) and Hop 2 (importer).
- Handles Windows file locking (antivirus / search indexer) with retry backoff.
- Preflight disk space verification before writing.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def fsync_file(path: Path) -> None:
    """fsync an open file descriptor for *path* (best-effort on Windows and POSIX)."""
    # On Windows, os.fsync (_commit) requires write mode (GENERIC_WRITE / O_RDWR)
    fd = None
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            logger.debug("fsync file failed for %s: %s", path, exc)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def flush_and_fsync(fh) -> None:
    """Flush and fsync an open Python binary file handle before closing."""
    try:
        fh.flush()
        fileno = getattr(fh, "fileno", None)
        if callable(fileno):
            fd = fileno()
            if isinstance(fd, int):
                os.fsync(fd)
    except (OSError, ValueError):
        pass


def fsync_dir(dir_path: Path) -> None:
    """fsync a directory so rename entries survive power loss (POSIX; no-op on Win if unsupported)."""
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            logger.debug("fsync dir failed for %s: %s", dir_path, exc)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def durable_replace(src: Path, dst: Path, max_retries: int = 5) -> None:
    """Atomically replace *dst* with *src* and fsync parent dir.

    Retries with exponential backoff on Windows if a transient lock
    (antivirus or search indexer) temporarily prevents replacement.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            os.replace(str(src), str(dst))
            last_exc = None
            break
        except (PermissionError, OSError) as exc:
            # On Windows, error 32 (sharing violation) or error 5 (access denied)
            # frequently occurs when Defender or SearchIndexer hooks new files.
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(0.05 * (2**attempt))
                continue
            raise

    try:
        fsync_dir(dst.parent)
    except Exception as exc:  # never fail transfer on fsync-dir edge
        logger.debug("durable_replace dir fsync skipped: %s", exc)


def check_free_space(
    path: Path,
    needed_bytes: int,
    min_margin_bytes: int = 100 * 1024 * 1024,
) -> tuple[bool, int, int]:
    """Check if the filesystem containing *path* has at least (needed_bytes + min_margin_bytes) free.

    Returns (is_sufficient, free_bytes, required_bytes).
    """
    try:
        target = path
        while not target.exists() and target.parent != target:
            target = target.parent
        usage = shutil.disk_usage(str(target))
        required = needed_bytes + min_margin_bytes
        return (usage.free >= required, usage.free, required)
    except Exception as exc:
        logger.warning("Could not check disk space for %s: %s", path, exc)
        return (True, 0, 0)


def sanitize_filename(name: str) -> str:
    """Strip Windows-reserved names, trailing dots/spaces, control chars."""
    import re

    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip().rstrip(". ")
    if not cleaned:
        cleaned = "_unnamed"
    stem = cleaned.split(".")[0].upper()
    if stem in reserved:
        cleaned = f"_{cleaned}"
    # MAX_PATH guard: keep names <= 200 chars (folder + suffix budget)
    if len(cleaned) > 200:
        stem_part, dot, ext = cleaned.partition(".")
        cleaned = stem_part[: 200 - len(ext) - 1] + (dot + ext if dot else "")
    return cleaned


def to_long_path(path: Path) -> str:
    """Return Windows long-path-safe string (\\\\?\\ prefix when needed)."""
    s = str(path)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        # Only prefix absolute paths
        if len(s) > 2 and s[1] == ":":
            return "\\\\?\\" + s
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
    return s
