"""
Transfera v2 — File Hashing Utility
Optimised BLAKE3 streaming with hashlib SHA-256 fallback.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional

from backend.config import BATCH_SIZE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BLAKE3 import with graceful fallback
# ---------------------------------------------------------------------------
_BLAKE3_AVAILABLE = False
try:
    import blake3 as _blake3

    _BLAKE3_AVAILABLE = True
    logger.debug("blake3 native extension loaded.")
except ImportError:
    logger.warning("blake3 package not installed — falling back to hashlib SHA-256.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
HashProgressCallback = Optional[Callable[[int, int], None]]


def hash_file(
    file_path: str | Path,
    *,
    algorithm: str = "blake3",
    chunk_size: int = BATCH_SIZE * 1024,
    on_progress: HashProgressCallback = None,
) -> str:
    """
    Compute a hex digest of *file_path* using streaming reads.

    Parameters
    ----------
    file_path : str | Path
        Absolute path to the file.
    algorithm : str
        ``"blake3"`` (default) or ``"sha256"``.
    chunk_size : int
        Read buffer size in bytes (defaults to BATCH_SIZE KB).
    on_progress : callable | None
        ``callback(bytes_processed, total_bytes)`` invoked after each chunk.

    Returns
    -------
    str
        Lower-case hex digest string.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    file_size = path.stat().st_size

    # Select hash backend
    use_blake3 = algorithm == "blake3" and _BLAKE3_AVAILABLE
    if use_blake3:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        if algorithm == "blake3" and not _BLAKE3_AVAILABLE:
            logger.info("BLAKE3 unavailable — using SHA-256 for %s", path.name)
        hasher = hashlib.sha256()

    bytes_read = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            bytes_read += len(chunk)
            if on_progress is not None:
                on_progress(bytes_read, file_size)

    return hasher.hexdigest()


async def hash_file_async(
    file_path: str | Path,
    *,
    algorithm: str = "blake3",
    chunk_size: int = BATCH_SIZE * 1024,
    on_progress: HashProgressCallback = None,
) -> str:
    """
    Async variant of :func:`hash_file` using ``aiofiles``.
    """
    import aiofiles  # noqa: F811

    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    file_size = path.stat().st_size

    use_blake3 = algorithm == "blake3" and _BLAKE3_AVAILABLE
    if use_blake3:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        if algorithm == "blake3" and not _BLAKE3_AVAILABLE:
            logger.info("BLAKE3 unavailable — using SHA-256 for %s", path.name)
        hasher = hashlib.sha256()

    bytes_read = 0
    async with aiofiles.open(path, "rb") as fh:
        while True:
            chunk = await fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            bytes_read += len(chunk)
            if on_progress is not None:
                on_progress(bytes_read, file_size)

    return hasher.hexdigest()


def verify_hash(
    file_path: str | Path,
    expected: str,
    *,
    algorithm: str = "blake3",
    chunk_size: int = BATCH_SIZE * 1024,
) -> bool:
    """Convenience wrapper — returns ``True`` if computed == expected."""
    actual = hash_file(file_path, algorithm=algorithm, chunk_size=chunk_size)
    return actual == expected.lower()
