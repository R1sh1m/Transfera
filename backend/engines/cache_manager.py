"""
Transfera v2 — Cache Manager (Hop 1: Source -> PC Local Cache)
Stream-by-stream copy with simultaneous BLAKE3 hash computation.
Writes .partial first; renames on verified hash match.
Supports both local filesystem and iOS device sources.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue as _queue
import threading as _threading
from asyncio import Event
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional

import aiofiles

from backend.config import CACHE_DIR, PARTIAL_SUFFIX

_HASH_CHUNK_SIZE: int = 4 * 1024 * 1024  # 4 MB
from backend.database.manager import increment_session_counter, session_scope
from backend.database.models import BatchStatus, HopStatus, MediaItem, TransferBatch
from backend.engines.batch_manager import get_batch_items, mark_batch_status
from backend.ios_device import is_ios_source, parse_ios_source

# Module-level queue for thumbnail DB updates (avoids per-file event loops)
_thumb_update_queue: _queue.Queue[tuple[int, str] | None] = _queue.Queue()
_thumb_worker_started = False
_thumb_worker_lock = _threading.Lock()

THUMB_CONCURRENCY = min(8, max(1, (os.cpu_count() or 2)))


def _ensure_thumb_worker() -> None:
    """Start the singleton thumbnail DB update worker thread if not running."""
    global _thumb_worker_started
    with _thumb_worker_lock:
        if _thumb_worker_started:
            return
        _thumb_worker_started = True
        t = _threading.Thread(target=_thumb_worker_loop, daemon=True, name="thumb-db-worker")
        t.start()


def _thumb_worker_loop() -> None:
    """Drain the thumbnail update queue in a single persistent event loop."""
    from backend.engines.thread_runner import submit_and_wait
    batch: list[tuple[int, str]] = []

    async def _flush(items: list[tuple[int, str]]) -> None:
        from backend.database.manager import session_scope
        from backend.database.models import MediaItem
        try:
            async with session_scope() as session:
                for item_id, status in items:
                    db_item = await session.get(MediaItem, item_id)
                    if db_item is not None:
                        db_item.thumbnail_path = "memory" if status == "ready" else db_item.thumbnail_path
                        db_item.thumbnail_status = status
                        db_item.touch()
        except Exception as exc:
            logger.error("DB thumbnail worker flush failed for batch %s: %s", items, exc, exc_info=True)
            # Sleep briefly in case it's a transient lock/connection error
            await asyncio.sleep(1.0)

    while True:
        try:
            item = _thumb_update_queue.get(timeout=0.1)
        except _queue.Empty:
            if batch:
                submit_and_wait(_flush(batch))
                batch.clear()
            continue

        if item is None:  # poison pill
            break
        batch.append(item)
        if len(batch) >= 20:  # flush in batches of 20
            submit_and_wait(_flush(batch))
            batch.clear()

    if batch:
        submit_and_wait(_flush(batch))

logger = logging.getLogger(__name__)

# BLAKE3 import with fallback
_BLAKE3_AVAILABLE = False
try:
    import blake3 as _blake3

    _BLAKE3_AVAILABLE = True
except ImportError:
    pass

# Maximum number of times to retry a transient device read failure on Hop 1.
# This covers: momentary USB drop, WPD COM "device busy", AFC ECONNRESET,
# and iOS Live Photo coalescing delays.
HOP1_MAX_RETRIES: int = 2
HOP1_RETRY_BASE_DELAY: float = 1.0   # seconds; multiplied by attempt number

# Exception types (by name string) that are considered transient and safe to retry.
# Using name-matching to avoid hard importing platform-specific exception types
# (WPD COM errors, pymobiledevice3 AFC errors) that may not be present on all systems.
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset({
    "AFCError",
    "ConnectionResetError",
    "BrokenPipeError",
    "TimeoutError",
    "OSError",
    "IOError",
    "ConnectionError",
})


# Exception types (by name string) that indicate the device was disconnected
# (as opposed to a transient USB blip that is safe to retry).
_DISCONNECT_EXC_NAMES: frozenset[str] = frozenset({
    "AFCError", "ConnectionResetError", "BrokenPipeError",
    "DeviceDisconnectedError", "MuxError",
})


def _looks_like_disconnect(exc: BaseException) -> bool:
    """Return True if the exception pattern suggests the device was disconnected."""
    to_check: list[BaseException] = [exc]
    if exc.__cause__:
        to_check.append(exc.__cause__)
    for e in to_check:
        name = type(e).__name__
        if name in _DISCONNECT_EXC_NAMES:
            return True
        msg = str(e).lower()
        if any(kw in msg for kw in ("disconnected", "device not found", "no device", "connection refused", "broken pipe")):
            return True
    return False


def _is_transient_exc(exc: BaseException) -> bool:
    """Return True if the exception looks like a transient device/IO error safe to retry."""
    to_check: list[BaseException] = [exc]
    if exc.__cause__ is not None:
        to_check.append(exc.__cause__)
    if exc.__context__ is not None:
        to_check.append(exc.__context__)
    for e in to_check:
        if type(e).__name__ in _TRANSIENT_EXC_NAMES:
            return True
        # OSError subclasses (errno-based) and WPD COM errors often surface as
        # "COMError", "pywintypes.error", or "AFCError" — check by MRO name too
        for klass in type(e).__mro__:
            if klass.__name__ in _TRANSIENT_EXC_NAMES:
                return True
    return False


ProgressCallback = Optional[Callable[[int, int, str], None]]
FileProgressCallback = Optional[Callable[[int, int, str, int], Awaitable[None]]]


# ---------------------------------------------------------------------------
# Streaming copy + hash
# ---------------------------------------------------------------------------
async def _copy_and_hash(
    src,
    dst: Path,
    *,
    chunk_size: int = _HASH_CHUNK_SIZE,
    on_progress: ProgressCallback = None,
    file_index: int = 0,
    file_total: int = 0,
) -> str:
    """
    Copy *src* to *dst* reading in ``chunk_size`` buffers while computing
    a BLAKE3 (or SHA-256 fallback) hash of the *source* data.

    The hash is computed from the read buffer, not re-read from disk,
    so the copy and hash happen in a single pass.

    Parameters
    ----------
    src : Path | Any
        Source file path or an async reader with ``read(n)`` and a ``size`` property.

    Returns the hex digest of the source file.
    """
    if isinstance(src, Path):
        file_size = src.stat().st_size
        src_context = aiofiles.open(src, "rb")
    else:
        # iOS device file via AFCFileReader
        file_size = src.size
        src_context = _afc_reader_context(src)

    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()

    bytes_read = 0
    async with src_context as src_fh, aiofiles.open(dst, "wb") as dst_fh:
        while True:
            chunk = await src_fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            await dst_fh.write(chunk)
            bytes_read += len(chunk)
            if on_progress is not None:
                on_progress(bytes_read, file_size, str(src))

    return hasher.hexdigest()


class _afc_reader_context:
    """Async context manager wrapper for a reader with ``open()``/``close()``."""
    def __init__(self, reader):
        self._reader = reader

    async def __aenter__(self):
        await self._reader.open()
        return self._reader

    async def __aexit__(self, *args):
        await self._reader.close()


# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------
def get_cache_path(cache_dir: Path, source_path: str, file_name: str) -> Path:
    """Deterministic cache path for a media item.

    Ensures that both local files and iOS device sources get a consistent cache location,
    preventing Windows path resolution issues (e.g. resolve() prepending drive letters
    to custom URI schemes like ios://) from causing hash/directory mismatches.
    """
    if is_ios_source(source_path):
        prefix = hashlib.md5(source_path.encode()).hexdigest()[:2]
    else:
        # Standardise local path by resolving
        resolved_src = Path(source_path).resolve()
        prefix = hashlib.md5(str(resolved_src).encode()).hexdigest()[:2]
    return cache_dir / prefix / file_name


def _cache_path_for(cache_dir: Path, source_path: Path) -> Path:
    """Deterministic cache path: cache_dir / <source_hash_prefix> / filename."""
    return get_cache_path(cache_dir, str(source_path), source_path.name)


def _partial_path(clean: Path) -> Path:
    """Return the .partial variant of a cache path."""
    return clean.with_suffix(clean.suffix + PARTIAL_SUFFIX)


# ---------------------------------------------------------------------------
# Parallel thumbnail generation
# ---------------------------------------------------------------------------
async def _generate_batch_thumbnails(
    thumbnail_tasks: list[tuple[int, Path]],
    session_id: int | None = None,
) -> None:
    """Generate thumbnails for a batch of items in parallel using asyncio.gather."""
    if not thumbnail_tasks:
        return
    _ensure_thumb_worker()
    sem = asyncio.Semaphore(THUMB_CONCURRENCY)

    async def _gen(item_id: int, cached_path: Path) -> tuple[int, str]:
        async with sem:
            try:
                from backend.engines.thumbnail_cache import thumbnail_cache
                from backend.engines.thumbnailer import generate_thumbnail_bytes
                data = await asyncio.to_thread(generate_thumbnail_bytes, cached_path)
                if data:
                    thumbnail_cache.put(item_id, data)
                    _thumb_update_queue.put((item_id, "ready"))
                    return item_id, "ready"
            except Exception as exc:
                logger.warning("Thumbnail failed for item %d: %s", item_id, exc)
                _thumb_update_queue.put((item_id, "failed"))
            return item_id, "failed"

    tasks = [_gen(item_id, path) for item_id, path in thumbnail_tasks]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ready_ids = [r[0] for r in results if isinstance(r, tuple) and r[1] == "ready"]
    failed_ids = [r[0] for r in results if isinstance(r, tuple) and r[1] == "failed"]
    logger.debug(
        "Batch thumbnails: %d ready, %d failed out of %d",
        len(ready_ids), len(failed_ids), len(thumbnail_tasks),
    )


# ---------------------------------------------------------------------------
# Main hop-1 entry point
# ---------------------------------------------------------------------------
async def cache_batch(
    batch_id: int,
    *,
    cache_dir: Path = CACHE_DIR,
    on_progress: ProgressCallback = None,
    on_file_progress: FileProgressCallback = None,
    cancel_event: Event | None = None,
    session_id: int | None = None,
) -> int:
    """
    Process one batch through Hop 1 (source -> local cache).

    For each ``MediaItem`` in the batch:
    1. If a clean cached file exists whose hash matches ``source_hash``, skip.
    2. Otherwise stream-copy source -> ``.partial`` while computing the hash.
    3. On hash match, atomically rename ``.partial`` -> clean path.
    4. On mismatch, delete the ``.partial`` and mark the item FAILED.

    ``on_file_progress(processed, total, file_name)`` is called after each
    file completes so the caller can emit real-time WS progress events.

    Returns the number of successfully cached items.
    """
    items = await get_batch_items(batch_id)
    if not items:
        logger.warning("Batch %d has no items — skipping", batch_id)
        return 0

    await mark_batch_status(batch_id, BatchStatus.LOADING)

    cached_count = 0
    total = len(items)
    pending_thumbnails: list[tuple[int, Path]] = []

    for idx, item in enumerate(items):
        if cancel_event is not None and cancel_event.is_set():
            logger.info(
                "Batch %d interrupted (pause or cancel) at item %d/%d",
                batch_id, idx + 1, total,
            )
            break

        was_already_completed = item.hop1_status == HopStatus.COMPLETED.value
        success = False
        try:
            success, cached_path = await _cache_single_item(
                item,
                cache_dir=cache_dir,
                on_progress=on_progress,
                file_index=idx,
                file_total=total,
            )
            if success:
                cached_count += 1
                if cached_path is not None:
                    pending_thumbnails.append((item.id, cached_path))
        except Exception as exc:
            logger.error("Cache failed for item %d (%s): %s", item.id, item.source_path, exc)
            await _mark_item_hop1(item, HopStatus.FAILED, str(exc))

        # Only count toward cached_files if this is a *new* cache (not a
        # resume where the item was already cached and counted in a prior run).
        if session_id is not None and success and not was_already_completed:
            await increment_session_counter(session_id, "cached_files", 1)

        if on_file_progress is not None:
            await on_file_progress(idx + 1, total, item.file_name, item.id)

    # Generate thumbnails in parallel for all successfully cached items
    await _generate_batch_thumbnails(pending_thumbnails, session_id)

    async with session_scope() as session:
        db_batch = await session.get(TransferBatch, batch_id)
        if db_batch is not None:
            db_batch.completed_items = cached_count
            db_batch.failed_items = total - cached_count
            if cached_count == 0:
                db_batch.status = BatchStatus.FAILED.value
            elif cached_count < total:
                db_batch.status = BatchStatus.PARTIAL.value
            else:
                db_batch.status = BatchStatus.COMPLETED.value
            db_batch.touch()

    logger.info("Batch %d cached: %d/%d succeeded", batch_id, cached_count, total)
    return cached_count



# ---------------------------------------------------------------------------
# Single-item cache logic
# ---------------------------------------------------------------------------
async def _cache_single_item(
    item: MediaItem,
    *,
    cache_dir: Path,
    on_progress: ProgressCallback,
    file_index: int,
    file_total: int,
) -> tuple[bool, Path | None]:
    """
    Cache a single media item. Returns (True, cached_path) on success.

    - Skips if clean cache already matches source_hash.
    - Writes .partial, verifies hash, renames on match.
    - Deletes .partial on mismatch or crash residue.
    - Supports both local files and iOS device sources.
    """
    source_path = item.source_path
    is_ios = is_ios_source(source_path)

    if is_ios:
        serial, afc_path = parse_ios_source(source_path)
        src_filename = afc_path.rsplit("/", 1)[-1] if "/" in afc_path else afc_path
        dst = get_cache_path(cache_dir, source_path, src_filename)
        partial = _partial_path(dst)
        partial.unlink(missing_ok=True)

        # Skip if clean file already matches source_hash
        if dst.is_file() and item.source_hash:
            if _verify_cached_hash(dst, item.source_hash):
                logger.debug("Cache hit (hash match): %s", dst.name)
                await _mark_item_hop1(item, HopStatus.COMPLETED)
                return (True, dst)
            else:
                logger.info("Cache hash mismatch for %s — re-caching", dst.name)
                dst.unlink(missing_ok=True)

        # Retry transient device errors for the USB/AFC read
        dst.parent.mkdir(parents=True, exist_ok=True)
        from backend.device_backend import get_device_backend_manager
        backend_mgr = get_device_backend_manager()
        computed_hash = ""
        last_exc: BaseException | None = None
        for attempt in range(1, HOP1_MAX_RETRIES + 2):
            try:
                # Re-create the file_reader on every retry attempt — stale handles
                # can persist across a USB blip and must be recreated from scratch.
                file_reader = backend_mgr.create_file_reader(serial, afc_path)
                computed_hash = await _copy_and_hash(
                    file_reader, partial,
                    on_progress=on_progress,
                    file_index=file_index,
                    file_total=file_total,
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                partial.unlink(missing_ok=True)
                if not _is_transient_exc(exc) or attempt > HOP1_MAX_RETRIES:
                    logger.error(
                        "iOS device read failed for %s after %d attempt(s): %s",
                        source_path, attempt, exc,
                    )
                    await _mark_item_hop1(item, HopStatus.FAILED,
                                          f"iOS device read failed: {exc}")
                    return (False, None)
                delay = HOP1_RETRY_BASE_DELAY * attempt
                logger.warning(
                    "Transient iOS read error for %s (attempt %d/%d) — "
                    "retrying in %.1fs: %s",
                    source_path, attempt, HOP1_MAX_RETRIES + 1, delay, exc,
                )
                await asyncio.sleep(delay)
        if last_exc is not None:
            if _looks_like_disconnect(last_exc):
                logger.error(
                    "Device disconnected during Hop 1 cache of %s: %s — "
                    "item marked FAILED. Reconnect device and retry session.",
                    source_path, last_exc,
                )
                await _mark_item_hop1(item, HopStatus.FAILED,
                                      f"Device disconnected: {last_exc}")
            else:
                logger.error(
                    "iOS device read failed for %s after %d attempt(s): %s",
                    source_path, HOP1_MAX_RETRIES + 1, last_exc,
                )
                await _mark_item_hop1(item, HopStatus.FAILED,
                                      f"iOS device read failed: {last_exc}")
            return (False, None)
    else:
        # Local file path
        src = Path(source_path).resolve()
        if not src.is_file():
            logger.warning("Source missing: %s — skipping item %d", src, item.id)
            await _mark_item_hop1(item, HopStatus.FAILED, f"Source missing: {src}")
            return (False, None)

        dst = get_cache_path(cache_dir, source_path, item.file_name)
        partial = _partial_path(dst)
        partial.unlink(missing_ok=True)

        # Skip if clean file already matches source_hash
        if dst.is_file() and item.source_hash:
            if _verify_cached_hash(dst, item.source_hash):
                logger.debug("Cache hit (hash match): %s", dst.name)
                await _mark_item_hop1(item, HopStatus.COMPLETED)
                return (True, dst)
            else:
                logger.info("Cache hash mismatch for %s — re-caching", dst.name)
                dst.unlink(missing_ok=True)

        # Retry transient device errors for local/USB device reads
        dst.parent.mkdir(parents=True, exist_ok=True)
        last_exc = None
        computed_hash = ""
        for attempt in range(1, HOP1_MAX_RETRIES + 2):
            try:
                computed_hash = await _copy_and_hash(
                    src, partial,
                    on_progress=on_progress,
                    file_index=file_index,
                    file_total=file_total,
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                partial.unlink(missing_ok=True)
                if not _is_transient_exc(exc) or attempt > HOP1_MAX_RETRIES:
                    logger.error(
                        "Local file read failed for %s after %d attempt(s): %s",
                        source_path, attempt, exc,
                    )
                    await _mark_item_hop1(item, HopStatus.FAILED, str(exc))
                    return (False, None)
                delay = HOP1_RETRY_BASE_DELAY * attempt
                logger.warning(
                    "Transient local read error for %s (attempt %d/%d) — "
                    "retrying in %.1fs: %s",
                    source_path, attempt, HOP1_MAX_RETRIES + 1, delay, exc,
                )
                await asyncio.sleep(delay)
        if last_exc is not None:
            await _mark_item_hop1(item, HopStatus.FAILED,
                                  f"Read failed after all retries: {last_exc}")
            return (False, None)

    # --- Verify hash against recorded source_hash ---
    if item.source_hash and computed_hash != item.source_hash.lower():
        logger.warning(
            "Hash mismatch for %s: expected %s, got %s",
            source_path, item.source_hash, computed_hash,
        )
        partial.unlink(missing_ok=True)
        await _mark_item_hop1(item, HopStatus.FAILED, "Source hash mismatch")
        return (False, None)

    # --- Hash match (or no prior hash) — commit ---
    import os
    os.replace(str(partial), str(dst))

    # Store the computed hash for downstream verification
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item.id)
        if db_item is not None:
            db_item.source_hash = computed_hash
            db_item.touch()

    await _mark_item_hop1(item, HopStatus.COMPLETED)

    return (True, dst)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _schedule_hop1_thumbnail(item_id: int, cached_path: Path) -> None:
    """Fire-and-forget thumbnail generation. Enqueues the DB update to a
    shared worker thread instead of creating per-file event loops."""
    _ensure_thumb_worker()

    from backend.engines.thumbnail_cache import thumbnail_cache
    from backend.engines.thumbnailer import generate_thumbnail_bytes

    def _generate() -> None:
        try:
            data = generate_thumbnail_bytes(cached_path)
            if data:
                thumbnail_cache.put(item_id, data)
                _thumb_update_queue.put((item_id, "ready"))
            else:
                _thumb_update_queue.put((item_id, "failed"))
        except Exception as exc:
            logger.error("Thumbnail generation failed for item %d: %s", item_id, exc)
            _thumb_update_queue.put((item_id, "failed"))

    t = _threading.Thread(target=_generate, daemon=True, name=f"thumb-h1-{item_id}")
    t.start()


def _verify_cached_hash(file_path: Path, expected: str) -> bool:
    """Synchronously verify a cached file's hash against an expected digest."""
    if _BLAKE3_AVAILABLE:
        hasher = _blake3.blake3()  # type: ignore[union-attr]
    else:
        hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == expected.lower()


async def _mark_item_hop1(
    item: MediaItem,
    status: HopStatus,
    error: str | None = None,
) -> None:
    """Update a single item's hop1_status."""
    async with session_scope() as session:
        db_item = await session.get(MediaItem, item.id)
        if db_item is not None:
            db_item.hop1_status = status.value
            if error is not None:
                db_item.error_message = error
            db_item.touch()
