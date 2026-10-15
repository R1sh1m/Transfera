"""
Transfera v2 — Strict Oldest-First Media Scanner
Walks directories (local or iOS device via AFC), detects Live Photo pairs,
enforces chronological insertion order. Supports optional mtime-based cutoff
for incremental imports from device sources.

Supports both raw path strings (legacy) and SourceRef typed references.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.source_types import SourceRef, SourceRefDevice, SourceRefLocal
from backend.config import (
    ALL_MEDIA_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from backend.database.manager import session_scope
from backend.database.models import HopStatus, MediaItem
from backend.engines.date_resolver import resolve_item_date
from backend.engines.metadata_extractor import (
    FileMetadata,
    _ts_to_datetime,
    extract_metadata,
    extract_metadata_batch,
)
from backend.ios_device import (
    IOS_SOURCE_PREFIX,
    canonical_device_serial,
    is_ios_source,
    parse_ios_source,
)

logger = logging.getLogger(__name__)

# Type alias for the progress callback
ProgressCallback = Optional[Callable[[int, int, str], None]]


# ---------------------------------------------------------------------------
# Live Photo detection helpers
# ---------------------------------------------------------------------------
def _normalise_stem(stem: str) -> str:
    """Lower-case, strip whitespace — for case-insensitive grouping."""
    return stem.strip().lower()


def _detect_live_photo_groups_from_entries(
    entries: list[FileMetadata],
) -> dict[str, str]:
    """
    Group files by (parent_dir, normalised stem). If a group contains at
    least one image extension AND one video extension, every MEDIA file in
    that group receives the same UUID string as its ``live_photo_group`` id.

    Works with both local Path-based and iOS device-based file entries.

    Returns a mapping ``{ source_path: live_photo_group_uuid }``.
    """
    media_exts = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    groups: dict[tuple[str, str], list[FileMetadata]] = defaultdict(list)

    for meta in entries:
        ext = (meta.extension or "").lower()
        if ext not in media_exts:
            continue
        # Extract parent directory from the file path
        parent = str(Path(meta.file_path).parent) if "/" in meta.file_path or "\\" in meta.file_path else ""
        key = (parent, _normalise_stem(Path(meta.file_path).stem))
        groups[key].append(meta)

    result: dict[str, str] = {}
    for key, members in groups.items():
        exts = {(m.extension or "").lower() for m in members}
        has_image = bool(exts & IMAGE_EXTENSIONS)
        has_video = bool(exts & VIDEO_EXTENSIONS)
        if has_image and has_video:
            group_id = str(uuid.uuid4())
            for m in members:
                result[m.file_path] = group_id
    return result


# ---------------------------------------------------------------------------
# Chronological sort key
# ---------------------------------------------------------------------------
def _sort_key(meta: FileMetadata) -> datetime:
    """
    Return the best available timestamp for chronological ordering.

    Uses the shared resolve_item_date() fallback chain with sanity checks.
    Falls back to the Unix epoch if no sane timestamp is available.
    """
    EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
    date, _source = resolve_item_date(
        date_taken=meta.date_taken,
        date_modified=meta.date_modified,
    )
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date or EPOCH


# ---------------------------------------------------------------------------
# iOS EXIF date extraction via partial AFC read
# ---------------------------------------------------------------------------

# Extensions we attempt EXIF extraction for on iOS devices
_IOS_EXIF_IMAGE_EXTS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".heic",
        ".heif",
        ".tiff",
        ".tif",
        ".webp",
        ".dng",
    }
)
_IOS_EXIF_VIDEO_EXTS = frozenset({".mov", ".mp4", ".m4v", ".3gp"})

# Bound for concurrent iOS EXIF partial reads (audit item 1): each read holds
# a pooled AFC slot (pool size 4 + ephemeral overflow), so 6-way fan-out with
# a semaphore keeps the walk + snapshot browse responsive. Pillow decode of
# the prefix bytes runs in threads (never on the event loop).
_IOS_EXIF_MAX_CONCURRENT = 6
_IOS_EXIF_READ_BYTES = 256 * 1024


async def _assert_device_trusted(serial: str) -> None:
    """Fast pre-walk trust gate (audit item 4): fail in one cheap listing.

    Raises DeviceLockedError / DeviceNotTrustedError before the recursive
    walk fans out, so a locked phone fails fast instead of producing N
    swallowed per-folder warnings + an empty walk that looks like "no
    files". Non-terminal errors pass through (the walk itself retries).
    """
    try:
        from backend.tier2_manager import get_device_manager

        devices, _tier = await get_device_manager().list_devices()
    except Exception:
        return
    for d in devices:
        if getattr(d, "serial", None) != serial:
            continue
        from backend.device_backend import DeviceLockedError, DeviceNotTrustedError

        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", "")))
        if status == "locked":
            raise DeviceLockedError(
                serial,
                "Your iPhone is locked. Please unlock it and tap 'Trust This Computer' when prompted, then try again.",
            )
        if status == "not_trusted":
            raise DeviceNotTrustedError(
                serial, "Please tap 'Trust This Computer' on your iPhone and enter your passcode, then try again."
            )
        return


def build_ios_entries_from_preview_items(
    preview_items: list[dict],
    serial: str,
) -> list[FileMetadata]:
    """Build scan entries from a frozen preview listing (audit item 1).

    Reuses the ``get_listing_items_by_scan_id`` snapshot (files + sizes +
    mtimes) so ``_phase_scan_and_create_batches`` skips the second full AFC
    walk. No device I/O, no EXIF here — dates resolve from mtime now and
    Hop 2 backfills real EXIF capture dates (same as the no-EXIF path in
    ``_upsert_media_item``). Yields no triplicated list copies: callers pass
    the snapshot list straight through.
    """
    entries: list[FileMetadata] = []
    prefix = f"{IOS_SOURCE_PREFIX}{serial}"
    for it in preview_items:
        try:
            if not isinstance(it, dict):
                continue
            abs_path = str(it.get("abs_path") or "")
            if not abs_path.startswith(prefix):
                continue
            fname = str(it.get("filename") or abs_path.rsplit("/", 1)[-1])
            ext = Path(fname).suffix.lower()
            if ext not in ALL_MEDIA_EXTENSIONS:
                continue
            try:
                size = int(it.get("size_bytes") or 0)
            except (TypeError, ValueError):
                size = 0
            try:
                mtime = float(it.get("mtime") or 0)
            except (TypeError, ValueError):
                mtime = 0.0
            dt = _ts_to_datetime(mtime) if mtime > 0 else None
            entries.append(
                FileMetadata(
                    file_path=abs_path,
                    file_name=fname,
                    file_size=size,
                    extension=ext,
                    date_created=dt,
                    date_modified=dt,
                )
            )
        except Exception:
            continue
    return entries


async def _extract_ios_file_date(
    afc,
    device_path: str,
    extension: str,
) -> datetime | None:
    """
    Extract EXIF DateTimeOriginal from an iOS device file via partial AFC read.

    Reads the first 256 KB of the file — enough for EXIF headers in HEIC, JPEG,
    TIFF, WebP, and fast-start MOV/MP4. Uses Pillow in-process for images (fast,
    no subprocess), then falls back to ExifTool via a temp file for videos and
    exotic formats.

    Returns the capture datetime, or None if the file is unreadable or contains
    no EXIF metadata.
    """
    ext = extension.lower()
    if ext not in _IOS_EXIF_IMAGE_EXTS and ext not in _IOS_EXIF_VIDEO_EXTS:
        return None

    import asyncio
    import io
    import tempfile
    from pathlib import Path

    try:
        handle = await asyncio.to_thread(afc.fopen, device_path)
        try:
            data = await asyncio.to_thread(afc.fread, handle, 64 * 1024)
        finally:
            try:
                await asyncio.to_thread(afc.fclose, handle)
            except Exception:
                pass

        if not data:
            return None

        # Strategy 1 — Pillow in-process (fast, no temp file, no subprocess)
        if ext in _IOS_EXIF_IMAGE_EXTS:
            try:
                from PIL import Image

                with Image.open(io.BytesIO(data)) as img:
                    exif = img.getexif()
                    raw = exif.get(36867) or exif.get(36868) or exif.get(306)
                    if raw and str(raw).strip():
                        from backend.engines.metadata_extractor import _parse_exif_datetime

                        dt = _parse_exif_datetime(str(raw).strip())
                        if dt is not None:
                            return dt
            except Exception:
                pass

        # Strategy 2 — ExifTool via temp file (handles anything Pillow cannot)
        try:
            suffix = ext if ext.startswith(".") else f".{ext}"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                meta = extract_metadata(Path(tmp_path))
                if meta and meta.date_taken:
                    return meta.date_taken
            finally:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

        return None
    except Exception as exc:
        logger.debug("Failed to extract EXIF from iOS %s: %s", device_path, exc)
        return None


# ---------------------------------------------------------------------------
# Selective-match helpers (case-insensitive, serial-canonical)
# ---------------------------------------------------------------------------
def _split_device_uri(uri: str) -> tuple[str, str, str] | None:
    """Split an ``ios://``/``wpd://``/``afc://`` URI into (scheme, canon_serial, path_lower).

    Returns None for non-device URIs. The on-device path is lowercased so old
    rows persisted with a lowercased ``/dcim`` still match a live ``/DCIM``
    walk, and the serial is canonicalised so Tier 1 <-> WPD id flips
    (UDID vs PnP id embedding the same UDID) still match.
    """
    lowered = uri.replace("\\", "/")
    for scheme in ("ios://", "wpd://", "afc://"):
        if lowered.lower().startswith(scheme):
            rest = lowered[len(scheme) :]
            parts = rest.split("/", 1)
            serial = parts[0]
            path = f"/{parts[1]}" if len(parts) > 1 else "/"
            try:
                canon = canonical_device_serial(serial)
            except Exception:
                canon = serial.lower()
            return (scheme, canon, path.lower())
    return None


def _build_allowed_match_set(allowed_paths: frozenset[str] | None) -> tuple[set[str], set[tuple[str, str, str]]] | None:
    """Build (plain_lower, device_canonical) match sets from *allowed_paths*.

    Plain URIs/paths compare lowercased; device URIs additionally compare by
    (scheme, canonical serial, lowercased device path) so serial-case flips
    and ``/dcim``-vs-``/DCIM`` drift never eat an explicit selection.
    """
    if allowed_paths is None:
        return None
    plain: set[str] = set()
    device: set[tuple[str, str, str]] = set()
    for p in allowed_paths:
        plain.add(p.replace("\\", "/").lower())
        split = _split_device_uri(p)
        if split is not None:
            device.add(split)
    return (plain, device)


def _is_selected_source(source_path: str, match_set: tuple[set[str], set[tuple[str, str, str]]] | None) -> bool:
    """Return True when *source_path* is in the explicit selection (case-insensitive)."""
    if match_set is None:
        return False
    plain, device = match_set
    if source_path.replace("\\", "/").lower() in plain:
        return True
    split = _split_device_uri(source_path)
    return split is not None and split in device


# ---------------------------------------------------------------------------
# iOS device scanner
# ---------------------------------------------------------------------------


async def _scan_ios_device(
    serial: str,
    afc_path: str,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
    allowed_paths: frozenset[str] | None = None,
    reuse_entries: list[FileMetadata] | None = None,
    reuse_walk_total: int | None = None,
) -> tuple[list[int], int, int]:
    """
    Walk an iOS device's DCIM directory via AFC, collecting media files.

    Files explicitly listed in *allowed_paths* (user-selected) are exempt
    from the *cutoff_datetime* incremental filter — the user asked for those
    files by name, so skipping them as "already imported" would turn an
    explicit re-transfer into a false 0-match failure. The cutoff still
    applies to full (non-selective) scans.

    Parameters
    ----------
    serial : str
        Device UDID/serial number.
    afc_path : str
        Absolute path on the device (e.g. "/DCIM").
    session_id : int | None
        Optional session FK to attach to newly created rows.
    on_progress : callback | None
        ``callback(processed, total, current_file)`` invoked per file.
    cutoff_datetime : datetime | None
        If provided (for non-selective device scans), files with mtime <=
        this datetime (already adjusted for safety overlap) are skipped.
    allowed_paths : frozenset[str] | None
        If set (selective import), only these explicit ``ios://`` URIs are
        processed; matching is case-insensitive and checked BEFORE the
        cutoff so selected files are never eaten by it.

    Returns
    -------
    tuple[list[int], int, int]
        ``(item_ids, skipped_by_cutoff, walk_total)`` where ``item_ids``
        is the ordered list of ``media_items.id`` values (oldest ->
        newest), ``skipped_by_cutoff`` counts files dropped by the
        incremental filter, and ``walk_total`` is the raw AFC walk file
        count (0 means the browse itself came back empty).
    """
    # 1. Recursively collect all media files from the device.
    #    When *reuse_entries* is supplied (frozen preview snapshot), the AFC
    #    walk is skipped entirely — triple-walk elimination (audit item 1).
    #    A fast trust gate runs first either way so lock/trust fails fast
    #    with a terminal error instead of an empty walk (audit item 4).
    logger.info(
        "Scanning iOS device %s at %s (cutoff=%s)",
        serial,
        afc_path,
        cutoff_datetime.isoformat() if cutoff_datetime else "none",
    )

    await _assert_device_trusted(serial)

    if reuse_entries is not None:
        # Snapshot reuse path: no AFC walk, no EXIF round trips. Dates come
        # from listing mtimes; Hop 2 backfills real EXIF capture dates.
        # Selective-cutoff exemption + prefix-intersect abort are applied by
        # the caller / below unchanged.
        device_files: list = []
        walk_total = reuse_walk_total if reuse_walk_total is not None else len(reuse_entries)
        snapshot_entries: list[FileMetadata] = list(reuse_entries)
    else:
        device_files = await _walk_ios_directory(serial, afc_path)
        logger.info("Found %d files on device %s under %s", len(device_files), serial, afc_path)
        walk_total = len(device_files)
        snapshot_entries = []

    # 2. Filter by media extension, selection, then cutoff.
    #    Order matters: the allowed_paths (explicit user selection) check
    #    runs BEFORE the incremental cutoff so selected files are exempt.
    #    (Selective-cutoff exemption preserved on both walk + reuse paths.)
    entries: list[FileMetadata] = []
    skipped_by_cutoff = 0

    allowed_lower = {p.lower() for p in allowed_paths} if allowed_paths is not None else None
    # Canonical-aware match set (serial-canonical + path-lowercased) so old
    # lowercased rows (/dcim) and Tier 1 <-> WPD serial flips still match.
    allowed_match = _build_allowed_match_set(allowed_paths)

    def _passes_selection_and_cutoff(source_path: str, mtime: float) -> tuple[bool, bool]:
        """Return (keep, is_selected) applying selection-first, cutoff-second."""
        is_selected = False
        if allowed_match is not None:
            if not _is_selected_source(source_path, allowed_match):
                return False, False
            is_selected = True
        if cutoff_datetime is not None and not is_selected and mtime > 0:
            file_mtime_dt = _ts_to_datetime(mtime)
            if file_mtime_dt is not None:
                _c_dt = cutoff_datetime if cutoff_datetime.tzinfo is not None else cutoff_datetime.replace(tzinfo=UTC)
                _f_dt = file_mtime_dt if file_mtime_dt.tzinfo is not None else file_mtime_dt.replace(tzinfo=UTC)
                if _f_dt <= _c_dt:
                    return False, is_selected
        return True, is_selected

    if reuse_entries is not None:
        # Snapshot reuse: filter only (no AFC I/O at all).
        for meta in snapshot_entries:
            # Re-derive mtime from stored date for cutoff comparison.
            _mtime = 0.0
            try:
                if meta.date_modified is not None:
                    _mtime = meta.date_modified.timestamp()
            except Exception:
                _mtime = 0.0
            keep, _sel = _passes_selection_and_cutoff(meta.file_path, _mtime)
            if not keep:
                if cutoff_datetime is not None and not _sel and _mtime > 0:
                    skipped_by_cutoff += 1
                continue
            entries.append(meta)
    else:
        # Normal walk path: build entries, then bounded concurrent EXIF.
        pending: list[tuple[FileMetadata, str]] = []
        for fi in device_files:
            if fi.is_dir:
                continue

            # Check extension
            ext = Path(fi.name).suffix.lower()
            if ext not in ALL_MEDIA_EXTENSIONS:
                continue

            # Build the ios:// source path (device path case preserved)
            source_path = f"{IOS_SOURCE_PREFIX}{serial}{fi.path}"

            keep, _sel = _passes_selection_and_cutoff(source_path, fi.mtime)
            if not keep:
                if _sel is False and allowed_match is not None and cutoff_datetime is not None:
                    # Selection miss — not a cutoff drop; fall through to
                    # cutoff accounting only for non-selective drops.
                    pass
                if cutoff_datetime is not None and not _sel and fi.mtime > 0:
                    skipped_by_cutoff += 1
                continue

            # Build FileMetadata from AFC stat info
            file_mtime_dt = _ts_to_datetime(fi.mtime) if fi.mtime > 0 else None

            meta = FileMetadata(
                file_path=source_path,
                file_name=fi.name,
                file_size=fi.size,
                extension=ext,
                date_created=file_mtime_dt,
                date_modified=file_mtime_dt,
            )
            pending.append((meta, fi.path))

        # Bounded concurrent EXIF via pooled partial reads (audit item 1):
        # one cheap trust-gated snapshot already ran; each EXIF read holds
        # a pooled slot briefly, semaphore keeps snapshot browses responsive.
        # Pillow decode runs in threads. Failures fall back to mtime (Hop 2
        # backfills), never fail the scan.
        if pending:
            import asyncio as _asyncio

            _exif_sem = _asyncio.Semaphore(_IOS_EXIF_MAX_CONCURRENT)

            async def _exif_one(meta: FileMetadata, dev_path: str) -> None:
                async with _exif_sem:
                    try:
                        from backend.tier2_manager import get_device_manager as _gdm

                        data = await _gdm().read_device_file_partial(serial, dev_path, _IOS_EXIF_READ_BYTES)
                    except Exception:
                        return
                    if not data:
                        return
                    ext = (meta.extension or "").lower()
                    if ext in _IOS_EXIF_IMAGE_EXTS:
                        try:
                            from PIL import Image as _PILImage

                            def _decode() -> datetime | None:
                                import io as _io

                                try:
                                    with _PILImage.open(_io.BytesIO(data)) as img:
                                        exif = img.getexif()
                                        raw = exif.get(36867) or exif.get(36868) or exif.get(306)
                                        if raw and str(raw).strip():
                                            from backend.engines.metadata_extractor import (
                                                _parse_exif_datetime as _parse_dt,
                                            )

                                            meta.date_taken = _parse_dt(str(raw).strip())
                                        # IFD Make (271) / Model (272) — same
                                        # if-None pattern: never overwrite.
                                        try:
                                            _make = exif.get(271)
                                            _model = exif.get(272)
                                            if _make and not meta.camera_make:
                                                meta.camera_make = str(_make).strip() or None
                                            if _model and not meta.camera_model:
                                                meta.camera_model = str(_model).strip() or None
                                        except Exception:
                                            pass
                                        # Pillow image dims as fallback when
                                        # ExifTool is unavailable.
                                        try:
                                            _w, _h = img.size
                                            if meta.width is None and _w:
                                                meta.width = int(_w)
                                                meta.height = int(_h)
                                        except Exception:
                                            pass
                                        return meta.date_taken
                                except Exception:
                                    return None
                                return None

                            dt = await _asyncio.to_thread(_decode)
                            if dt is not None:
                                return
                        except Exception:
                            pass
                    # Video / Pillow-miss fallback: ExifTool via temp file.
                    try:
                        import tempfile as _tf

                        suffix = ext if ext.startswith(".") else f".{ext}"

                        def _et() -> datetime | None:
                            with _tf.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                                tmp.write(data)
                                tmp_path = tmp.name
                            try:
                                m2 = extract_metadata(Path(tmp_path))
                                if m2 is not None:
                                    if m2.date_taken and meta.date_taken is None:
                                        meta.date_taken = m2.date_taken
                                    # Copy camera/dims/gps/duration via
                                    # if-None pattern (never overwrite).
                                    if meta.camera_make is None and m2.camera_make:
                                        meta.camera_make = m2.camera_make
                                    if meta.camera_model is None and m2.camera_model:
                                        meta.camera_model = m2.camera_model
                                    if meta.width is None and m2.width is not None:
                                        meta.width = m2.width
                                        meta.height = m2.height
                                    if meta.gps_lat is None and m2.gps_lat is not None:
                                        meta.gps_lat = m2.gps_lat
                                        meta.gps_lon = m2.gps_lon
                                    if meta.duration_s is None and m2.duration_s is not None:
                                        meta.duration_s = m2.duration_s
                                    return meta.date_taken
                            finally:
                                try:
                                    Path(tmp_path).unlink(missing_ok=True)
                                except Exception:
                                    pass
                            return None

                        dt2 = await _asyncio.to_thread(_et)
                        if dt2 is not None:
                            meta.date_taken = dt2
                    except Exception:
                        pass

            # Only pay for EXIF on files that can carry it; others skip I/O.
            _exif_tasks = [
                _exif_one(meta, dev_path)
                for meta, dev_path in pending
                if (meta.extension or "").lower() in (_IOS_EXIF_IMAGE_EXTS | _IOS_EXIF_VIDEO_EXTS)
            ]
            if _exif_tasks:
                await _asyncio.gather(*_exif_tasks, return_exceptions=True)
            entries.extend(meta for meta, _dp in pending)

    if skipped_by_cutoff > 0:
        logger.info(
            "Skipped %d files at or before cutoff from device %s",
            skipped_by_cutoff,
            serial,
        )

    total = len(entries)
    if total == 0:
        logger.info(
            "No media files to process from device %s after filtering (walk_total=%d skipped_by_cutoff=%d selective=%s)",
            serial,
            walk_total,
            skipped_by_cutoff,
            allowed_match is not None,
        )
        return [], skipped_by_cutoff, walk_total

    logger.info("Processing %d media files from device %s", total, serial)

    # 3. Detect Live Photo groups
    lp_groups = _detect_live_photo_groups_from_entries(entries)

    # 4. Sort chronologically (oldest first)
    entries.sort(key=_sort_key)
    logger.info("Entries sorted chronologically (oldest -> newest).")

    # 5. Insert / upsert into database
    inserted_ids: list[int] = []

    async with session_scope() as session:
        for idx, meta in enumerate(entries):
            lp_id = lp_groups.get(meta.file_path)
            row_id = await _upsert_media_item(
                session,
                meta=meta,
                live_photo_group=lp_id,
                session_id=session_id,
            )
            inserted_ids.append(row_id)
            if on_progress is not None:
                on_progress(idx + 1, total, meta.file_path)

    logger.info("iOS scan complete — %d items persisted.", len(inserted_ids))
    return inserted_ids, skipped_by_cutoff, walk_total


async def _walk_ios_directory(
    serial: str,
    path: str,
    _sem: asyncio.Semaphore | None = None,
    *,
    strict: bool = False,
) -> list:
    """
    Recursively walk an iOS device directory via unified manager.

    Returns a flat list of DeviceFileInfo for all files found.

    Subdirectories fan out concurrently, bounded by a semaphore (2, leaving
    headroom in the 4-slot AFC pool for listing snapshot browses and gated
    thumbnail reads alongside in-flight walks): unbounded fan-out would
    stampede usbmuxd with one handshake per folder and starve the transfer
    streaming off the device. Thumbnail reads take their own gate
    (``_IOS_THUMB_SEMAPHORE`` = 3 in device_preview) and never the walk
    semaphore, so a page-turn snapshot browse never queues behind 25
    thumbnails.

    When *strict* is True, any per-subdirectory failure raises
    :class:`PartialWalkError` (carrying the partial file list) instead of
    being swallowed, and a top-level browse failure re-raises instead of
    returning ``[]`` — so preview callers never cache a partial walk as
    complete (which drifts totals and empties latter pages). The default
    ``strict=False`` preserves the legacy best-effort behavior for the
    import scan path.
    """
    from backend.ios_device import DeviceFileInfo
    from backend.tier2_manager import get_device_manager

    if _sem is None:
        _sem = asyncio.Semaphore(2)

    all_files: list[DeviceFileInfo] = []
    manager = get_device_manager()
    try:
        async with _sem:
            entries = await manager.browse_device(serial, path)
    except Exception as exc:
        logger.warning("Failed to browse %s on device %s: %s", path, serial, exc)
        if strict:
            raise
        return all_files

    subdirs: list[str] = []
    for entry in entries:
        if entry.is_dir:
            # Recurse into subdirectories (skip "." and "..")
            if entry.name in (".", ".."):
                continue
            subdirs.append(f"{path.rstrip('/')}/{entry.name}")
        else:
            all_files.append(entry)

    if subdirs:
        tasks = [_walk_ios_directory(serial, sd, _sem, strict=strict) for sd in subdirs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = 0
        first_err: BaseException | None = None
        for res in results:
            if isinstance(res, BaseException):
                failures += 1
                if first_err is None:
                    first_err = res
                logger.warning("Subdirectory walk failed: %s", res)
            elif isinstance(res, list):
                all_files.extend(res)

        if failures and strict:
            raise PartialWalkError(
                f"Partial walk of {path} on device {serial}: "
                f"{failures}/{len(results)} subdirectories failed "
                f"(first: {first_err})",
                partial=all_files,
                failures=failures,
                total=len(results),
            ) from first_err

    return all_files


class PartialWalkError(RuntimeError):
    """A strict recursive walk finished with ≥1 failed subdirectory.

    Carries the partial file list so callers can decide (serve stale,
    serve partial with ``cache_status="partial-not-cached"``, or 503)
    without ever storing the partial result as a complete listing.
    """

    def __init__(
        self,
        message: str,
        *,
        partial: list | None = None,
        failures: int = 0,
        total: int = 0,
    ) -> None:
        super().__init__(message)
        self.partial = partial if partial is not None else []
        self.failures = failures
        self.total = total


# ---------------------------------------------------------------------------
# Local filesystem scanner (existing behavior)
# ---------------------------------------------------------------------------
async def _scan_local_files(
    source: Path,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    allowed_paths: frozenset[str] | None = None,
) -> list[int]:
    """
    Walk a local directory, extract metadata, detect Live Photo pairs,
    sort chronologically, and insert/update media_items rows.

    This is the original scan logic, refactored into its own function.
    """
    # 1. Collect all media files
    if source.is_file():
        if source.suffix.lower() in ALL_MEDIA_EXTENSIONS:
            media_files = [source]
        else:
            logger.info("Skipping non-media file: %s", source)
            media_files = []
    elif source.is_dir():
        if allowed_paths is not None:
            # Selective mode: only process explicitly listed paths
            media_files = sorted(
                Path(p) for p in allowed_paths if Path(p).is_file() and Path(p).suffix.lower() in ALL_MEDIA_EXTENSIONS
            )
            logger.info(
                "Selective scan: %d pre-selected files (skipped rglob of %s)",
                len(media_files),
                source,
            )
        else:
            # Full scan
            media_files = sorted(
                p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in ALL_MEDIA_EXTENSIONS
            )
    else:
        logger.error("Source path does not exist: %s", source)
        return []

    total = len(media_files)
    logger.info("Found %d media files under %s", total, source)

    if total == 0:
        return []

    # 2. Detect Live Photo groups
    lp_groups = _detect_live_photo_groups_from_paths(media_files)

    # 3. Extract metadata for all files in one ExifTool round-trip (batch mode).
    #    For a folder with N files this avoids N subprocess spawns; instead a
    #    single persistent ExifTool process handles everything via stdin/stdout.
    entries: list[tuple[Path, FileMetadata, str | None]] = []
    try:
        batch_results = extract_metadata_batch(media_files)
    except Exception as exc:
        logger.warning("Batch metadata extraction failed, falling back to per-file: %s", exc)
        batch_results = {}

    for idx, fpath in enumerate(media_files):
        resolved = str(fpath.resolve())
        meta = batch_results.get(resolved)
        if meta is None:
            try:
                meta = extract_metadata(fpath)
            except Exception as exc2:
                logger.warning("Failed to extract metadata for %s: %s", fpath, exc2)
                stat = fpath.stat()
                meta = FileMetadata(
                    file_path=resolved,
                    file_name=fpath.name,
                    file_size=stat.st_size,
                    extension=fpath.suffix.lower(),
                    date_created=_ts_to_datetime(getattr(stat, "st_birthtime", getattr(stat, "st_ctime", 0.0))),
                    date_modified=_ts_to_datetime(stat.st_mtime),
                )
        lp_id = lp_groups.get(resolved)
        entries.append((fpath, meta, lp_id))
        if on_progress is not None:
            on_progress(idx + 1, total, str(fpath))

    # 4. Sort chronologically (oldest first)
    entries.sort(key=lambda e: _sort_key(e[1]))
    logger.info("Entries sorted chronologically (oldest -> newest).")

    # 5. Insert / upsert into database
    inserted_ids: list[int] = []

    async with session_scope() as session:
        for fpath, meta, lp_group_id in entries:
            row_id = await _upsert_media_item(
                session,
                meta=meta,
                live_photo_group=lp_group_id,
                session_id=session_id,
            )
            inserted_ids.append(row_id)

    # 6. Pre-generate thumbnails for local files (off the transfer critical
    #    path).  The original source file is already on disk, so there's no
    #    reason to wait for any copy hop.  Run in a background thread so the
    #    scan itself isn't blocked.
    if inserted_ids:
        _schedule_local_thumbnails(inserted_ids, entries)

    logger.info("Scan complete — %d items persisted.", len(inserted_ids))
    return inserted_ids


def _schedule_local_thumbnails(
    item_ids: list[int],
    entries: list[tuple[Path, FileMetadata, str | None]],
) -> None:
    """Fire-and-forget thumbnail generation for local source files.

    Runs a pool of daemon threads (up to min(8, cpu_count) workers) so Pillow
    decoding and JPEG re-encoding are distributed across CPU cores.  Thumbnails
    are stored in the in-memory LRU cache (no disk writes).  DB updates are
    batched in a single async pass after all workers complete.
    """
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from backend.engines.thumbnail_cache import thumbnail_cache
    from backend.engines.thumbnailer import generate_thumbnail_bytes

    id_to_path = {row_id: fpath for row_id, (fpath, _meta, _lp) in zip(item_ids, entries)}

    # Use up to 8 workers, capped at the number of logical CPUs.  Pillow image
    # decode + JPEG re-encode is CPU-bound and benefits linearly from cores.
    # We leave at least one core free for the uvicorn event loop.
    worker_count = min(8, max(1, (os.cpu_count() or 2) - 1))

    def _generate_one(row_id: int, fpath: Path) -> int | None:
        """Generate thumbnail for one file. Returns row_id on success."""
        try:
            data = generate_thumbnail_bytes(fpath)
            if data:
                thumbnail_cache.put(row_id, data)
                return row_id
        except Exception as exc:
            try:
                logger.warning("Pre-scan thumbnail failed for item %d: %s", row_id, exc)
            except Exception:
                pass
        return None

    def _generate_all() -> None:
        from backend.engines.cache_manager import _ensure_thumb_worker, _thumb_update_queue

        _ensure_thumb_worker()

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="prescan-thumb") as pool:
            futures = {pool.submit(_generate_one, rid, fp): rid for rid, fp in id_to_path.items()}
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    _thumb_update_queue.put((result, "ready"))
                else:
                    _thumb_update_queue.put((futures[future], "failed"))

        logger.debug(
            "Pre-scan thumbnail batch complete: %d total (%d workers)",
            len(id_to_path),
            worker_count,
        )

    t = threading.Thread(target=_generate_all, daemon=True, name="pre-scan-thumbnails")
    t.start()


def _detect_live_photo_groups_from_paths(
    files: list[Path],
) -> dict[str, str]:
    """
    Group local Path files by (parent_dir, normalised stem).
    Returns a mapping ``{ resolved_path_str: live_photo_group_uuid }``.
    """
    media_exts = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for p in files:
        if p.suffix.lower() not in media_exts:
            continue
        key = (str(p.parent), _normalise_stem(p.stem))
        groups[key].append(p)

    result: dict[str, str] = {}
    for key, members in groups.items():
        exts = {m.suffix.lower() for m in members}
        has_image = bool(exts & IMAGE_EXTENSIONS)
        has_video = bool(exts & VIDEO_EXTENSIONS)
        if has_image and has_video:
            group_id = str(uuid.uuid4())
            for m in members:
                result[str(m.resolve())] = group_id
    return result


# ---------------------------------------------------------------------------
# Main scanner entry point
# ---------------------------------------------------------------------------
async def scan(
    source_path: str | Path,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> list[int]:
    """
    Walk *source_path*, extract metadata, detect Live Photo pairs, sort
    chronologically (oldest first), and insert/update ``media_items`` rows.

    For iOS device sources (``ios://`` prefix), uses AFC-based DCIM browsing.
    For local filesystem sources, uses ``Path.rglob()`` or ``allowed_paths``.

    Parameters
    ----------
    source_path : str | Path
        Root directory (single file, local path, or ``ios://`` device path)
        to scan.
    session_id : int | None
        Optional session FK to attach to newly created rows.
    on_progress : callback | None
        ``callback(processed, total, current_file)`` invoked per file.
    cutoff_datetime : datetime | None
        If provided (for device sources only), files with mtime at or before
        this datetime are skipped. This is a performance optimization and
        does NOT replace hash-based duplicate detection.
    allowed_paths : frozenset[str] | None
        If set (selective import), only process these specific file paths
        instead of walking the entire source tree recursively.

    Returns
    -------
    list[int]
        Ordered list of ``media_items.id`` values (oldest -> newest).
        Use :func:`scan_with_stats` when cutoff/walk diagnostics are needed.
    """
    item_ids, _skipped, _walk_total = await scan_with_stats(
        source_path,
        session_id=session_id,
        on_progress=on_progress,
        cutoff_datetime=cutoff_datetime,
        allowed_paths=allowed_paths,
    )
    return item_ids


async def scan_with_stats(
    source_path: str | Path,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
    allowed_paths: frozenset[str] | None = None,
    reuse_entries: list[FileMetadata] | None = None,
    reuse_walk_total: int | None = None,
) -> tuple[list[int], int, int]:
    """
    Same as :func:`scan`, but also returns scan diagnostics.

    *reuse_entries* (built via :func:`build_ios_entries_from_preview_items`)
    skips the AFC walk entirely — the frozen preview snapshot is filtered +
    persisted directly. Purely advisory; None falls back to a normal walk.

    Returns
    -------
    tuple[list[int], int, int]
        ``(item_ids, skipped_by_cutoff, walk_total)``. For local sources
        both counters are 0 and ``walk_total`` is the queued item count.
    """
    source_str = str(source_path)

    # --- iOS device source ---
    if is_ios_source(source_str):
        serial, afc_path = parse_ios_source(source_str)
        return await _scan_ios_device(
            serial,
            afc_path,
            session_id=session_id,
            on_progress=on_progress,
            cutoff_datetime=cutoff_datetime,
            allowed_paths=allowed_paths,
            reuse_entries=reuse_entries,
            reuse_walk_total=reuse_walk_total,
        )

    # --- Local filesystem source ---
    source = Path(source_str).resolve()
    item_ids = await _scan_local_files(
        source,
        session_id=session_id,
        on_progress=on_progress,
        allowed_paths=allowed_paths,
    )
    return item_ids, 0, len(item_ids)


async def scan_from_ref_with_stats(
    source_ref: SourceRef,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> tuple[list[int], int, int]:
    """Same as :func:`scan_from_ref`, but returns ``(item_ids, skipped_by_cutoff, walk_total)``."""
    if isinstance(source_ref, SourceRefLocal):
        source = Path(source_ref.path).resolve()
        item_ids = await _scan_local_files(
            source,
            session_id=session_id,
            on_progress=on_progress,
            allowed_paths=allowed_paths,
        )
        return item_ids, 0, len(item_ids)
    elif isinstance(source_ref, SourceRefDevice):
        return await _scan_ios_device(
            source_ref.device_id,
            source_ref.device_path,
            session_id=session_id,
            on_progress=on_progress,
            cutoff_datetime=cutoff_datetime,
            allowed_paths=allowed_paths,
        )
    else:
        raise ValueError(f"Unknown source ref type: {type(source_ref)}")


async def scan_from_ref(
    source_ref: SourceRef,
    *,
    session_id: int | None = None,
    on_progress: ProgressCallback = None,
    cutoff_datetime: datetime | None = None,
    allowed_paths: frozenset[str] | None = None,
) -> list[int]:
    """
    Walk a typed SourceRef, extract metadata, detect Live Photo pairs, sort
    chronologically (oldest first), and insert/update ``media_items`` rows.

    This is the preferred entry point for new code. The dispatch to the
    correct backend (local filesystem or device AFC) happens here at a
    single boundary.

    Parameters
    ----------
    source_ref : SourceRef
        Discriminated union: SourceRefLocal or SourceRefDevice.
    session_id : int | None
        Optional session FK to attach to newly created rows.
    on_progress : callback | None
        ``callback(processed, total, current_file)`` invoked per file.
    cutoff_datetime : datetime | None
        If provided (for device sources only), files with mtime at or before
        this datetime are skipped.
    allowed_paths : frozenset[str] | None
        If set (selective import), only process these specific file paths
        instead of walking the entire source tree recursively.

    Returns
    -------
    list[int]
        Ordered list of ``media_items.id`` values (oldest -> newest).
        Use :func:`scan_from_ref_with_stats` when diagnostics are needed.
    """
    item_ids, _skipped, _walk_total = await scan_from_ref_with_stats(
        source_ref,
        session_id=session_id,
        on_progress=on_progress,
        cutoff_datetime=cutoff_datetime,
        allowed_paths=allowed_paths,
    )
    return item_ids


# ---------------------------------------------------------------------------
# Upsert logic (dedup by source_path)
# ---------------------------------------------------------------------------
async def _upsert_media_item(
    session: AsyncSession,
    *,
    meta: FileMetadata,
    live_photo_group: str | None,
    session_id: int | None,
) -> int:
    """
    If ``source_path`` already exists with a non-FAILED status, return its
    existing id. Otherwise insert a new row and return the new id.

    Resolves the item's date using the shared fallback chain and stores it
    along with provenance (date_source).

    Dedup matching is case-insensitive on ``source_path``: iOS device paths
    (``/DCIM`` vs ``/dcim``) and Windows local paths must hit the same row
    regardless of the case used at selection vs scan time.
    """
    lookup = select(MediaItem.id, MediaItem.final_status).where(
        func.lower(MediaItem.source_path) == meta.file_path.lower()
    )
    if session_id is not None:
        lookup = lookup.where(MediaItem.session_id == session_id)
    result = await session.execute(lookup)
    existing = result.first()

    if existing is not None:
        existing_id, status = existing
        if status != HopStatus.FAILED.value:
            # Reuse existing row — but update session_id so the item is
            # associated with the *current* session, not a stale one from
            # an earlier scan that happened to share the same source_path.
            # Also backfill NULL camera/dims/gps/duration when the fresh
            # scan meta now carries values (if-None pattern).
            if session_id is not None:
                stmt = update(MediaItem).where(MediaItem.id == existing_id).values(session_id=session_id)
                await session.execute(stmt)
            try:
                _reuse_updates: dict = {}
                _cur = await session.get(MediaItem, existing_id)
                if _cur is not None:
                    if getattr(_cur, "camera_make", None) is None and meta.camera_make:
                        _reuse_updates["camera_make"] = meta.camera_make
                    if getattr(_cur, "camera_model", None) is None and meta.camera_model:
                        _reuse_updates["camera_model"] = meta.camera_model
                    if getattr(_cur, "width", None) is None and meta.width is not None:
                        _reuse_updates["width"] = meta.width
                        _reuse_updates["height"] = meta.height
                    if getattr(_cur, "gps_lat", None) is None and meta.gps_lat is not None:
                        _reuse_updates["gps_lat"] = meta.gps_lat
                        _reuse_updates["gps_lon"] = meta.gps_lon
                    if getattr(_cur, "duration_s", None) is None and meta.duration_s is not None:
                        _reuse_updates["duration_s"] = meta.duration_s
                    if getattr(_cur, "date_taken", None) is None and meta.date_taken is not None:
                        _reuse_updates["date_taken"] = meta.date_taken
                if _reuse_updates:
                    await session.execute(
                        update(MediaItem).where(MediaItem.id == existing_id).values(**_reuse_updates)
                    )
            except Exception:
                pass
            logger.debug("Reusing existing row id=%d for %s", existing_id, meta.file_path)
            return existing_id

        # Existing item with final_status=FAILED — reset it for retry
        # rather than trying to INSERT a duplicate (which would violate
        # the UNIQUE constraint on source_path).
        is_ios = meta.file_path.startswith("ios://")
        if is_ios and meta.date_taken is None:
            # iOS device and EXIF was not available at scan time;
            # leave date_taken as None so Hop 2's extraction fills it in.
            resolved_date = None
            date_source = None
        else:
            resolved_date, date_source = resolve_item_date(
                date_taken=meta.date_taken,
                date_modified=meta.date_modified,
            )
        stmt = (
            update(MediaItem)
            .where(MediaItem.id == existing_id)
            .values(
                hop1_status=HopStatus.SCANNED.value,
                hop2_status=HopStatus.PENDING.value,
                final_status=HopStatus.PENDING.value,
                session_id=session_id,
                error_message=None,
                retry_count=0,
                live_photo_group=live_photo_group,
                date_taken=resolved_date,
                date_source=date_source,
                camera_make=meta.camera_make,
                camera_model=meta.camera_model,
                width=meta.width,
                height=meta.height,
                duration_s=meta.duration_s,
                gps_lat=meta.gps_lat,
                gps_lon=meta.gps_lon,
            )
        )
        await session.execute(stmt)
        logger.debug("Resetting failed row id=%d for retry: %s", existing_id, meta.file_path)
        return existing_id

    # Resolve date using shared fallback chain
    is_ios = meta.file_path.startswith("ios://")
    if is_ios and meta.date_taken is None:
        # iOS device and EXIF was not available at scan time;
        # leave date_taken as None so Hop 2's extraction fills it in.
        resolved_date = None
        date_source = None
    else:
        resolved_date, date_source = resolve_item_date(
            date_taken=meta.date_taken,
            date_modified=meta.date_modified,
        )

    item = MediaItem(
        source_path=meta.file_path,
        file_name=meta.file_name,
        file_size=meta.file_size,
        extension=meta.extension,
        mime_type=meta.mime_type,
        hop1_status=HopStatus.SCANNED.value,
        hop2_status=HopStatus.PENDING.value,
        final_status=HopStatus.PENDING.value,
        session_id=session_id,
        live_photo_group=live_photo_group,
        date_taken=resolved_date,
        date_source=date_source,
        camera_make=meta.camera_make,
        camera_model=meta.camera_model,
        width=meta.width,
        height=meta.height,
        duration_s=meta.duration_s,
        gps_lat=meta.gps_lat,
        gps_lon=meta.gps_lon,
    )
    session.add(item)
    await session.flush()

    return item.id  # type: ignore[return-value]
