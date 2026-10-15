"""
Transfera v2 — Device Preview API
Fast in-place directory preview (no thumbnails generated during scan)
and lazy thumbnail generation with in-memory LRU cache.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io as _io
import json
import logging
import math
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from backend.api.auth import require_local_token, require_local_token_or_query
from backend.device_backend import DeviceLockedError, DeviceNotTrustedError, DeviceStatus, WpdDeviceAccessDenied
from backend.ios_device import drop_afc_slots, ensure_afc_warm
from backend.tier2_manager import get_device_manager

_PREVIEW_MAX_FILES = 5000
_PREVIEW_MAX_DEPTH = 8

logger = logging.getLogger(__name__)

# HEIF opener for Pillow decodes below (mirrors thumbnailer/metadata_extractor).
# Registration is process-global and deliberately LAZY: importing pillow_heif
# (+ its PIL cascade) at module top added ~40 ms to engine cold start while
# the "Starting engine…" splash is up. The first thumbnail request pays for
# it instead via _ensure_heif_opener() — preview endpoints must not depend
# on import order, only on the opener being registered before decode.
_HEIF_AVAILABLE = False
_heif_attempted = False
_heif_lock = threading.Lock()


def _ensure_heif_opener() -> bool:
    """Register the pillow-heif opener on first use (idempotent, thread-safe).

    Returns True when HEIC/HEIF decode is available. Import failure is
    cached so repeated thumbnail calls never retry the import.
    """
    global _HEIF_AVAILABLE, _heif_attempted
    if _heif_attempted:
        return _HEIF_AVAILABLE
    with _heif_lock:
        if _heif_attempted:
            return _HEIF_AVAILABLE
        try:
            from pillow_heif import register_heif_opener as _register_heif_opener

            _register_heif_opener()
            _HEIF_AVAILABLE = True
        except Exception as _heif_exc:
            _HEIF_AVAILABLE = False
            logger.debug(
                "pillow-heif unavailable — HEIF preview thumbnails will fall back to gray: %s",
                _heif_exc,
            )
        _heif_attempted = True
        return _HEIF_AVAILABLE


def _is_transient_thumb_error(exc: BaseException | None) -> bool:
    """True when *exc* looks like a transient USB/timeout/lock failure.

    Such failures deserve a 503 + Retry-After so the frontend can retry,
    instead of a grey-fallback 200 which looks like a successful load.
    """
    if exc is None:
        return False
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    try:
        import concurrent.futures as _cf

        if isinstance(exc, _cf.TimeoutError):
            return True
    except Exception:
        pass
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    for marker in (
        "timeout",
        "timed out",
        "temporarily",
        "try again",
        "busy",
        "locked",
        "lock",
        "not trusted",
        "trust",
        "pair",
        "disconnected",
        "unreachable",
        "usb",
        "stalled",
        "slot dropped",
        "econnreset",
        "broken pipe",
    ):
        if marker in text:
            return True
    return False


_LOOKUP_FAILURE_MARKERS = (
    "all backends failed",
    "no backends",
    "not available",
    "not installed",
    "not found",
    "no such device",
)
_TRANSIENT_IO_MARKERS = (
    "timeout",
    "timed out",
    "busy",
    "stalled",
    "slot dropped",
    "econnreset",
    "broken pipe",
    "locked",
    "not trusted",
)


def _is_lookup_failure(exc: BaseException | None) -> bool:
    """True when *exc* is a device/backend lookup failure, not a read failure.

    The unified manager aggregates per-tier errors into
    ``RuntimeError("All backends failed ...")`` when no tier can see the
    device (unknown serial, helper missing, bridge down). Such errors must
    degrade to the grey fallback — never 503 — while genuine I/O failures
    (timeout, stalled USB stream, broken pipe) stay transient even when
    wrapped in the same aggregate.
    """
    if exc is None:
        return False
    import re as _re

    text = f"{type(exc).__name__}: {exc}".lower()
    if not any(marker in text for marker in _LOOKUP_FAILURE_MARKERS):
        return False
    # "stalled" is a substring of "installed" ("pymobiledevice3 not
    # installed", "wpd_helper not installed"): only count it as an I/O
    # marker on a word boundary so missing-dependency aggregates stay
    # lookup failures.
    _io_markers = [m for m in _TRANSIENT_IO_MARKERS if m != "stalled"]
    if any(marker in text for marker in _io_markers):
        return False
    if _re.search(r"\bstalled\b", text):
        return False
    return True


async def _read_device_file_partial(device_id: str, path: str, max_bytes: int) -> bytes | None:
    """
    Read only the first *max_bytes* bytes of a file on the iOS device via AFC.

    This is the key optimisation for iOS thumbnail generation: HEIC/JPEG files
    embed a small JPEG preview (~20–80 KB) in their EXIF header, which sits in
    the first ~128 KB of the file.  Reading 256 KB instead of the full 4–8 MB
    is ~20–30× faster over USB.

    Routes through the unified manager so Tier 1 (pooled AFC), WPD (helper
    --offset/--length) and Tier 2 (full-read fallback) are all handled.
    Returns bytes on success, None on failure. Genuine I/O failures
    (USB timeout / stalled stream / broken pipe) are re-raised so thumbnail
    callers can map them to 503 + Retry-After via _is_transient_thumb_error
    instead of degrading to a grey fallback or 404/500; lookup-style
    failures (unknown device / no backend available) still return None.
    """
    try:
        return await get_device_manager().read_device_file_partial(device_id, path, max_bytes)
    except Exception as exc:
        if _is_transient_thumb_error(exc) and not _is_lookup_failure(exc):
            raise
        logger.debug("_read_device_file_partial failed for %s: %s", path, exc)
        return None


router = APIRouter(prefix="/api/device")

# Supported extensions for preview scanning. Image set mirrors the iPhone-native
# formats the scanner imports (config.IMAGE_EXTENSIONS): notably .heif, which
# iPhones emit and Pillow decodes via pillow-heif (registered above).
PREVIEW_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".heic",
        ".heif",
        ".png",
        ".webp",
        ".dng",
        ".tiff",
        ".tif",
        ".bmp",
    }
)
PREVIEW_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".3gp",
        ".m4v",
        ".mts",
        ".wmv",
    }
)
PREVIEW_EXTENSIONS: frozenset[str] = PREVIEW_IMAGE_EXTENSIONS | PREVIEW_VIDEO_EXTENSIONS

# ---------------------------------------------------------------------------
# LRU thumbnail cache for preview thumbnails (keyed by (path, size))
# ---------------------------------------------------------------------------
_THUMB_CACHE_MAX = 500
_thumb_cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_thumb_cache_lock = threading.Lock()


# Fallback JPEG — generated once at import time via Pillow (never a tuple)
def _make_gray_fallback() -> bytes:
    try:
        from PIL import Image

        img = Image.new("RGB", (4, 4), (128, 128, 128))
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return buf.getvalue()
    except Exception:
        return b"\xff\xd8\xff\xd9"


# Fallback JPEG — generated lazily on first use via Pillow (never a tuple).
# Building it at import time pulled PIL into engine cold start; the first
# fallback response pays for it instead (~10 ms, once per process).
_GRAY_FALLBACK_JPEG_CACHE: bytes | None = None


def _gray_fallback_jpeg() -> bytes:
    """Return the cached gray fallback JPEG, generating it on first use."""
    global _GRAY_FALLBACK_JPEG_CACHE
    if _GRAY_FALLBACK_JPEG_CACHE is None:
        _GRAY_FALLBACK_JPEG_CACHE = _make_gray_fallback()
    return _GRAY_FALLBACK_JPEG_CACHE


def __getattr__(name: str) -> bytes:
    # Backwards-compat: external code may read
    # ``device_preview._GRAY_FALLBACK_JPEG`` directly (PEP 562).
    if name == "_GRAY_FALLBACK_JPEG":
        return _gray_fallback_jpeg()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _get_thumb_cache_size() -> int:
    return sum(len(v) for v in _thumb_cache.values())


def _put_thumb_cache(key: tuple[str, int], data: bytes) -> None:
    with _thumb_cache_lock:
        if key in _thumb_cache:
            _thumb_cache.move_to_end(key)
            return
        if len(_thumb_cache) >= _THUMB_CACHE_MAX:
            _thumb_cache.popitem(last=False)
        _thumb_cache[key] = data


# ---------------------------------------------------------------------------
# On-disk thumbnail cache (L2 behind the in-memory LRU above)
# ---------------------------------------------------------------------------
# Device thumbnails cost a USB round trip each; without persistence every app
# restart (and every LRU overflow) re-downloads them all. Files are keyed by
# device + path + size + file size/mtime (exact invalidation, zero extra
# round trips — the listing already provides size/mtime). Best-effort: cache
# failures never fail the request. Cap ~500 MB with oldest-first eviction.
_DEVICE_THUMB_DISK_MAX_BYTES = 500 * 1024 * 1024
_DEVICE_THUMB_DISK_MAX_FILES = 20000


def _device_thumb_disk_dir() -> Path | None:
    try:
        from backend.config import CACHE_DIR

        d = Path(CACHE_DIR) / "device_thumbs"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def _device_thumb_disk_key(
    device_id: str, path: str, size: int, file_size: int | None, file_mtime: float | None
) -> str:
    raw = f"{device_id}\x00{path}\x00{size}\x00{file_size}\x00{file_mtime}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest() + ".jpg"


def _read_disk_thumb(key: str) -> bytes | None:
    try:
        d = _device_thumb_disk_dir()
        if d is None:
            return None
        p = d / key
        if not p.is_file():
            return None
        data = p.read_bytes()
        if len(data) <= 10:
            return None
        try:
            p.touch()
        except OSError:
            pass
        return data
    except Exception:
        return None


def _write_disk_thumb(key: str, data: bytes) -> None:
    try:
        d = _device_thumb_disk_dir()
        if d is None or not data or len(data) <= 10:
            return
        (d / key).write_bytes(data)
        _sweep_disk_thumbs(d)
    except Exception:
        pass


def _sweep_disk_thumbs(d: Path) -> None:
    """Oldest-first eviction when over count/size caps. Best-effort."""
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in d.glob("*.jpg") if p.is_file()]
    except OSError:
        return
    if len(files) <= _DEVICE_THUMB_DISK_MAX_FILES:
        total = sum(s for _, s, _ in files)
        if total <= _DEVICE_THUMB_DISK_MAX_BYTES:
            return
    files.sort(key=lambda t: t[0])
    total = sum(s for _, s, _ in files)
    for _, _, p in files:
        if len(files) <= _DEVICE_THUMB_DISK_MAX_FILES and total <= _DEVICE_THUMB_DISK_MAX_BYTES:
            break
        try:
            total -= p.stat().st_size
            p.unlink()
            files.pop(0)
        except OSError:
            break


def _get_thumb_cache(key: tuple[str, int]) -> bytes | None:
    with _thumb_cache_lock:
        if key not in _thumb_cache:
            return None
        _thumb_cache.move_to_end(key)
        return _thumb_cache[key]


# ---------------------------------------------------------------------------
# Server-side listing cache for GET /api/device/ios-preview
# ---------------------------------------------------------------------------
# The recursive /DCIM walk fans out over AFC/Tier-1 (thousands of files, one
# USB round trip per folder) and dominated page-1 latency. This cache stores
# the enumerated + sorted item list per request shape so page 1 serves
# without a full walk.
#
# Key: (device_id, path, recursive, sort_by).
# Validation: exact snapshot of a single cheap top-level browse
#   (count + sha256 of name/size/mtime/is_dir rows) — no extra round trips
#   beyond that one browse. A matching snapshot means the cached items are
#   current even past the TTL.
# Freshness: short TTL (_LISTING_TTL_S) + stale-while-revalidate — a stale
#   hit serves the cached page immediately and triggers one background
#   refresh (deduped via _listing_refresh_inflight).
# Invalidation: entries for devices absent from list_devices() are purged
#   on every request (covers disconnect; a reconnect then misses and walks
#   fresh). A background refresh re-checks presence before storing.
# Memory: each list is capped at _PREVIEW_MAX_FILES items and the cache
#   holds at most _LISTING_MAX_KEYS keys (oldest-first eviction).
# Threading: all cache/inflight access under _listing_cache_lock, mirroring
#   the _thumb_cache_lock pattern.
_LISTING_TTL_S = 45.0
_LISTING_MAX_KEYS = 16


# ---------------------------------------------------------------------------
# iOS thumbnail concurrency gate (listing priority)
# ---------------------------------------------------------------------------
# The AFC pool has 4 slots; recursive walks and thumbnail reads compete for
# them. Thumbnail device reads take this semaphore (3 permits) while the
# listing/browse path (_listing_quick_snapshot, _fetch_ios_entries browse)
# deliberately does NOT — so a page turn's single snapshot browse never
# queues behind a page of in-flight thumbnail reads.
_IOS_THUMB_MAX_CONCURRENT = 3
_IOS_THUMB_SEMAPHORE = asyncio.Semaphore(_IOS_THUMB_MAX_CONCURRENT)

# Device video prefix: first N bytes fetched for single-frame extraction.
# 4 MB halves USB cost vs 8 MB; faststart moov-up-front files still resolve,
# while moov-at-EOF prefixes keep 503 + Retry-After semantics (never grey).
_VIDEO_PREFIX_BYTES = 4 * 1024 * 1024


class _ListingCacheEntry:
    __slots__ = (
        "items",
        "photos",
        "videos",
        "total_size",
        "snapshot",
        "cached_at",
        "scan_id",
        "likely_duplicate_paths",
        "new_paths",
    )

    def __init__(
        self,
        items: list,
        photos: int,
        videos: int,
        total_size: int,
        snapshot: tuple | None,
        cached_at: float,
        scan_id: str | None = None,
        likely_duplicate_paths: list[str] | None = None,
        new_paths: list[str] | None = None,
    ) -> None:
        self.items = items
        self.photos = photos
        self.videos = videos
        self.total_size = total_size
        self.snapshot = snapshot
        self.cached_at = cached_at
        self.scan_id = scan_id
        self.likely_duplicate_paths = likely_duplicate_paths
        self.new_paths = new_paths


_listing_cache: OrderedDict[tuple, _ListingCacheEntry] = OrderedDict()
_listing_cache_lock = threading.Lock()
_listing_refresh_inflight: set[tuple] = set()


def _listing_cache_key(device_id: str, path: str, recursive: bool, sort_by: str) -> tuple:
    return (device_id, path, recursive, sort_by)


def _listing_snapshot_of(entries: list) -> tuple[int, str]:
    """Exact, cheap snapshot of one directory listing level.

    Computed from already-fetched entries (no extra round trips): any
    rename/add/remove/size/mtime change alters the digest.
    """
    try:
        rows: list[str] = []
        for e in entries:
            try:
                rows.append(
                    f"{getattr(e, 'name', '')}\x00{getattr(e, 'size', 0)}"
                    f"\x00{getattr(e, 'mtime', 0)}\x00{int(bool(getattr(e, 'is_dir', False)))}"
                )
            except Exception:
                continue
        rows.sort()
        digest = hashlib.sha256("\x01".join(rows).encode("utf-8", errors="replace")).hexdigest()
        return (len(rows), digest)
    except Exception:
        return (0, "")


async def _listing_quick_snapshot(manager: Any, device_id: str, path: str) -> tuple[int, str] | None:
    """Single top-level browse used to validate a cached recursive listing.

    One AFC round trip vs a full recursive walk. Returns None when the
    browse itself fails (caller treats the cache as unvalidated, not wrong).
    """
    try:
        top_entries = await manager.browse_device(device_id, path)
    except Exception as exc:
        logger.debug("ios-preview snapshot browse failed for %s:%s: %s", device_id, path, exc)
        return None
    return _listing_snapshot_of(top_entries)


def _listing_cache_get(key: tuple) -> _ListingCacheEntry | None:
    with _listing_cache_lock:
        return _listing_cache.get(key)


def _listing_touch(key: tuple, now: float) -> None:
    with _listing_cache_lock:
        ent = _listing_cache.get(key)
        if ent is not None:
            ent.cached_at = now
            _listing_cache.move_to_end(key)


def _listing_store(
    key: tuple,
    items: list,
    photos: int,
    videos: int,
    total_size: int,
    snapshot: tuple[int, str] | None,
    now: float,
) -> str:
    """Store a walk result; totals are frozen per scan ID from this point on.

    Returns the stable scan ID for this walk (derived from device + root +
    snapshot digest): every page/sort/filter sliced from this entry reports
    identical totals under the same scan ID — totals never drift mid-scan.
    """
    try:
        device_id = key[0] if len(key) > 0 else ""
        path = key[1] if len(key) > 1 else ""
        recursive = bool(key[2]) if len(key) > 2 else False
        scan_id = _scan_id_for(device_id, path, recursive, snapshot)
    except Exception:
        scan_id = ""
    with _listing_cache_lock:
        while len(_listing_cache) >= _LISTING_MAX_KEYS and key not in _listing_cache:
            _listing_cache.popitem(last=False)
        _listing_cache[key] = _ListingCacheEntry(items, photos, videos, total_size, snapshot, now, scan_id)
        _listing_cache.move_to_end(key)
    try:
        _listing_l2_store(key, items, photos, videos, total_size, snapshot, scan_id)
    except Exception:
        pass
    return scan_id


def _scan_id_for(device_id: str, path: str, recursive: bool, snapshot: tuple[int, str] | None) -> str:
    """Stable per-scan identity: device + root + snapshot digest.

    A rescan that observes any add/remove/rename/size/mtime change yields
    a new snapshot digest and therefore a new scan ID; pagination, sort
    and filter variations of the same walk share one ID with frozen
    totals (files count + total bytes).
    """
    try:
        count = snapshot[0] if snapshot else 0
        digest = snapshot[1] if snapshot else ""
        raw = f"{device_id}\x00{path}\x00{int(recursive)}\x00{count}\x00{digest}"
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    except Exception:
        return ""


def _listing_drop(key: tuple) -> None:
    with _listing_cache_lock:
        _listing_cache.pop(key, None)


def _listing_purge_disconnected(current_serials: set) -> None:
    """Drop cached listings for devices no longer present.

    Called on every ios-preview request: a disconnect purges immediately,
    so a later reconnect misses and walks fresh.
    """
    with _listing_cache_lock:
        stale_keys = [k for k in _listing_cache if k[0] not in current_serials]
        for k in stale_keys:
            _listing_cache.pop(k, None)
    if stale_keys:
        logger.debug("ios-preview listing cache purged %d entries for disconnected devices", len(stale_keys))
    try:
        _listing_l2_purge_devices({k[0] for k in stale_keys if isinstance(k, tuple) and k and isinstance(k[0], str)})
    except Exception:
        pass


def purge_listing_cache_for_serial(serial: str) -> int:
    """Remove listing-cache entries belonging to *serial*.

    Matches keys where the device id equals the serial, equals its
    canonical form, or contains the serial/canonical as a substring
    (covers UDIDs embedded in WPD PnP paths).  Returns the number of
    keys purged.  Preview logic is otherwise untouched.
    """
    try:
        from backend.ios_device import canonical_device_serial as _canon
    except Exception:
        _canon = None  # type: ignore[assignment]

    candidates: set[str] = {serial}
    if _canon is not None:
        try:
            candidates.add(_canon(serial))
        except Exception:
            pass
    lowered = {c.lower() for c in candidates if c}

    purged = 0
    with _listing_cache_lock:
        for k in list(_listing_cache.keys()):
            if not isinstance(k, tuple) or not k or not isinstance(k[0], str):
                continue
            key_device = k[0]
            key_lower = key_device.lower()
            if key_device in candidates or any(c in key_lower for c in lowered if c):
                _listing_cache.pop(k, None)
                purged += 1
    if purged:
        logger.info("ios-preview listing cache purged %d key(s) for serial %s", purged, serial)
    try:
        _listing_l2_purge_matching(candidates, lowered)
    except Exception:
        pass
    return purged


def find_listing_by_scan_id(scan_id: str) -> dict | None:
    """Look up a cached preview walk by its scan ID (snapshot-based reuse).

    Session creation passes the ``scan_id`` the frontend received from
    ``GET /api/device/ios-preview`` so the transfer can reuse the frozen
    enumeration (files + total bytes) instead of re-walking AFC. Returns
    ``{"device_id", "path", "recursive", "total", "photos", "videos",
    "total_size_bytes", "age_s"}`` or None when the entry aged out /
    was never cached (caller falls back to normal behavior).
    """
    if not scan_id:
        return None
    with _listing_cache_lock:
        for key, ent in list(_listing_cache.items()):
            if getattr(ent, "scan_id", None) != scan_id:
                continue
            try:
                age_s = time.monotonic() - ent.cached_at
            except Exception:
                age_s = -1.0
            return {
                "device_id": key[0] if len(key) > 0 else "",
                "path": key[1] if len(key) > 1 else "",
                "recursive": bool(key[2]) if len(key) > 2 else False,
                "total": len(ent.items),
                "photos": ent.photos,
                "videos": ent.videos,
                "total_size_bytes": ent.total_size,
                "age_s": age_s,
            }
    try:
        return _listing_l2_find_by_scan_id(scan_id)
    except Exception:
        pass
    return None


def get_listing_items_by_scan_id(scan_id: str) -> list[dict] | None:
    """Look up the full items list for a cached preview walk by its scan ID."""
    if not scan_id:
        return None
    with _listing_cache_lock:
        for _key, ent in list(_listing_cache.items()):
            if getattr(ent, "scan_id", None) == scan_id:
                return list(ent.items)
    try:
        conn = _listing_l2_connect()
        try:
            cur = conn.execute(
                "SELECT items_json FROM device_listings WHERE scan_id=? LIMIT 1",
                (scan_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                loaded = json.loads(row[0])
                if isinstance(loaded, list):
                    return loaded
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# SQLite L2 listing store (survives restarts; L1 _listing_cache stays L1)
# ---------------------------------------------------------------------------
# Same key shape as L1 (device_id, path, recursive, sort_by) plus snapshot
# digest + scan_id columns. Reuse is ALWAYS snapshot-gated: a stored row is
# only served when its snapshot digest equals a fresh single-browse
# snapshot, so a restart costs one cheap browse instead of a full AFC
# rewalk. Partial walks are never stored (callers only store successful
# walks — PartialWalkError paths return before _listing_store).
_LISTING_L2_DB_NAME = "device_listings.db"
_LISTING_L2_MAX_JSON_BYTES = 8 * 1024 * 1024
_LISTING_L2_PRUNE_AFTER_S = 30 * 24 * 3600

# Persistent negative cache for truly-undecodable thumbnails: fallback
# reasons keyed by device + path + file size/mtime, TTL 24 h. Transient
# USB failures are NEVER stored here (they return 503 before reaching the
# grey-fallback tail that writes this cache).
_THUMB_FAIL_TTL_S = 24 * 3600
_PERSISTENT_FAIL_REASONS = frozenset({"undecodable", "no-pillow-heif"})


def _listing_l2_db_path() -> Path | None:
    try:
        from backend.config import CACHE_DIR

        d = Path(CACHE_DIR)
        d.mkdir(parents=True, exist_ok=True)
        return d / _LISTING_L2_DB_NAME
    except Exception:
        return None


def _listing_l2_connect() -> Any:
    db_path = _listing_l2_db_path()
    if db_path is None:
        raise OSError("no cache dir")
    conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS device_listings ("
        " device_id TEXT NOT NULL, path TEXT NOT NULL, recursive INTEGER NOT NULL,"
        " sort_by TEXT NOT NULL, snapshot_count INTEGER NOT NULL DEFAULT 0,"
        " snapshot_digest TEXT NOT NULL DEFAULT '', scan_id TEXT NOT NULL DEFAULT '',"
        " photos INTEGER NOT NULL DEFAULT 0, videos INTEGER NOT NULL DEFAULT 0,"
        " total_size INTEGER NOT NULL DEFAULT 0, items_json TEXT NOT NULL DEFAULT '[]',"
        " cached_at REAL NOT NULL DEFAULT 0,"
        " PRIMARY KEY (device_id, path, recursive, sort_by))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS thumb_fail_cache ("
        " fail_key TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT 'undecodable',"
        " cached_at REAL NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn


def _listing_l2_store(
    key: tuple,
    items: list,
    photos: int,
    videos: int,
    total_size: int,
    snapshot: tuple[int, str] | None,
    scan_id: str | None,
) -> None:
    """Best-effort L2 write. Never raises; never stores partial walks."""
    try:
        if not isinstance(key, tuple) or len(key) < 4:
            return
        device_id, path, recursive, sort_by = key[0], key[1], int(bool(key[2])), str(key[3])
        count = snapshot[0] if snapshot else 0
        digest = snapshot[1] if snapshot else ""
        payload = json.dumps(items)
        if len(payload) > _LISTING_L2_MAX_JSON_BYTES:
            return
        conn = _listing_l2_connect()
        try:
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO device_listings"
                " (device_id, path, recursive, sort_by, snapshot_count, snapshot_digest,"
                " scan_id, photos, videos, total_size, items_json, cached_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    device_id,
                    path,
                    recursive,
                    sort_by,
                    count,
                    digest,
                    scan_id or "",
                    photos,
                    videos,
                    total_size,
                    payload,
                    now,
                ),
            )
            try:
                conn.execute(
                    "DELETE FROM device_listings WHERE cached_at < ?",
                    (now - _LISTING_L2_PRUNE_AFTER_S,),
                )
            except Exception:
                pass
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("ios-preview L2 store skipped: %s", exc)


def _listing_l2_row_to_dict(row: tuple) -> dict | None:
    try:
        (device_id, path, recursive, sort_by, count, digest, scan_id, photos, videos, total, payload, cached_at) = row
        items = json.loads(payload) if payload else []
        if not isinstance(items, list):
            return None
        return {
            "device_id": device_id,
            "path": path,
            "recursive": bool(recursive),
            "sort_by": sort_by,
            "snapshot": (int(count), str(digest)),
            "scan_id": str(scan_id),
            "photos": int(photos),
            "videos": int(videos),
            "total_size": int(total),
            "items": items,
            "cached_at": float(cached_at),
        }
    except Exception:
        return None


def _listing_l2_find_reusable(
    device_id: str, path: str, recursive: bool, snapshot: tuple[int, str] | None
) -> dict | None:
    """Any L2 row for the same walk whose snapshot digest matches exactly."""
    if snapshot is None:
        return None
    try:
        conn = _listing_l2_connect()
        try:
            cur = conn.execute(
                "SELECT device_id, path, recursive, sort_by, snapshot_count, snapshot_digest,"
                " scan_id, photos, videos, total_size, items_json, cached_at"
                " FROM device_listings WHERE device_id=? AND path=? AND recursive=?",
                (device_id, path, int(recursive)),
            )
            for row in cur.fetchall():
                d = _listing_l2_row_to_dict(row)
                if d is not None and d["snapshot"] == (snapshot[0], snapshot[1]):
                    return d
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("ios-preview L2 lookup skipped: %s", exc)
    return None


def _listing_l2_reused_entry(
    device_id: str, path: str, recursive: bool, snapshot: tuple[int, str] | None
) -> tuple[tuple, _ListingCacheEntry] | None:
    """Wrap an L2 reusable row as an L1-style (key, entry) pair.

    Lets the endpoint reuse the exact L1 code path (re-sort + _listing_store
    promotion back to L1) with a minimal diff. Returns None on any miss.
    """
    try:
        d = _listing_l2_find_reusable(device_id, path, recursive, snapshot)
        if d is None:
            return None
        key = _listing_cache_key(device_id, path, recursive, d["sort_by"])
        ent = _ListingCacheEntry(
            d["items"],
            d["photos"],
            d["videos"],
            d["total_size"],
            d["snapshot"],
            time.monotonic(),
            d["scan_id"],
        )
        return key, ent
    except Exception:
        return None


def _listing_l2_find_by_scan_id(scan_id: str) -> dict | None:
    if not scan_id:
        return None
    try:
        conn = _listing_l2_connect()
        try:
            cur = conn.execute(
                "SELECT device_id, path, recursive, sort_by, snapshot_count, snapshot_digest,"
                " scan_id, photos, videos, total_size, items_json, cached_at"
                " FROM device_listings WHERE scan_id=? LIMIT 1",
                (scan_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            d = _listing_l2_row_to_dict(row)
            if d is None:
                return None
            try:
                age_s = time.time() - d["cached_at"]
            except Exception:
                age_s = -1.0
            return {
                "device_id": d["device_id"],
                "path": d["path"],
                "recursive": d["recursive"],
                "total": len(d["items"]),
                "photos": d["photos"],
                "videos": d["videos"],
                "total_size_bytes": d["total_size"],
                "age_s": age_s,
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return None


def _listing_l2_purge_devices(device_ids: set) -> None:
    if not device_ids:
        return
    try:
        conn = _listing_l2_connect()
        try:
            conn.executemany("DELETE FROM device_listings WHERE device_id=?", [(d,) for d in device_ids])
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("ios-preview L2 purge skipped: %s", exc)


def _listing_l2_purge_matching(candidates: set, lowered: set) -> None:
    """Mirror purge_listing_cache_for_serial substring semantics in SQLite."""
    if not candidates and not lowered:
        return
    try:
        conn = _listing_l2_connect()
        try:
            cur = conn.execute("SELECT DISTINCT device_id FROM device_listings")
            ids = [r[0] for r in cur.fetchall() if r and isinstance(r[0], str)]
            drop = [i for i in ids if i in candidates or any(c in i.lower() for c in lowered if c)]
            for i in drop:
                conn.execute("DELETE FROM device_listings WHERE device_id=?", (i,))
            if drop:
                conn.commit()
                logger.info("ios-preview L2 purged %d device(s)", len(drop))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("ios-preview L2 purge skipped: %s", exc)


# ---------------------------------------------------------------------------
# Inline LQIP (low-quality image placeholders) — zero USB round trips
# ---------------------------------------------------------------------------
# Listing path must never touch AFC: _build_ios_preview_items only consults
# the in-memory LQIP dict (populated opportunistically when thumbnails are
# generated + loaded from the tiny on-disk LQIP store at page-slice time).
# Field name is additive-only: every preview item carries ``lqip`` (a
# data: URL or "").
_LQIP_MEM_MAX = 2000
_lqip_mem: OrderedDict[str, str] = OrderedDict()
_lqip_mem_lock = threading.Lock()


def _lqip_key(device_id: str, dev_path: str, fsize: Any, mtime: Any) -> str:
    return f"{device_id}\x00{dev_path}\x00{fsize}\x00{mtime}"


def _lqip_mem_get(key: str) -> str:
    with _lqip_mem_lock:
        v = _lqip_mem.get(key)
        if v is not None:
            _lqip_mem.move_to_end(key)
            return v
    return ""


def _lqip_mem_put(key: str, url: str) -> None:
    if not url:
        return
    with _lqip_mem_lock:
        if key in _lqip_mem:
            _lqip_mem.move_to_end(key)
            return
        while len(_lqip_mem) >= _LQIP_MEM_MAX:
            _lqip_mem.popitem(last=False)
        _lqip_mem[key] = url


def _lqip_disk_path(key: str) -> Path | None:
    try:
        from backend.config import CACHE_DIR

        d = Path(CACHE_DIR) / "device_lqip"
        d.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest() + ".txt"
        return d / name
    except Exception:
        return None


def _make_lqip_data_url(jpeg_bytes: bytes) -> str:
    """Shrink already-fetched thumbnail bytes to a ~32px data URL."""
    try:
        if not jpeg_bytes or len(jpeg_bytes) <= 10:
            return ""
        from PIL import Image

        img = None
        try:
            img = Image.open(_io.BytesIO(jpeg_bytes))
            img.thumbnail((32, 32))
            if img.mode not in ("RGB",):
                img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=50)
            tiny = buf.getvalue()
        finally:
            if img is not None:
                try:
                    img.close()
                except Exception:
                    pass
        if not tiny:
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(tiny).decode("ascii")
    except Exception:
        return ""


def _store_lqip_for_thumb(device_id: str, dev_path: str, fsize: Any, mtime: Any, jpeg_bytes: bytes) -> None:
    """Opportunistic LQIP capture on the thumbnail success path (no USB)."""
    try:
        url = _make_lqip_data_url(jpeg_bytes)
        if not url:
            return
        key = _lqip_key(device_id, dev_path, fsize, mtime)
        _lqip_mem_put(key, url)
        try:
            p = _lqip_disk_path(key)
            if p is not None:
                p.write_text(url, encoding="ascii")
        except Exception:
            pass
    except Exception:
        pass


def _lqip_lookup_for_page(device_id: str, dev_path: str, fsize: Any, mtime: Any) -> str:
    """RAM first, then one tiny local text-file read. Never touches AFC."""
    key = _lqip_key(device_id, dev_path, fsize, mtime)
    hit = _lqip_mem_get(key)
    if hit:
        return hit
    try:
        p = _lqip_disk_path(key)
        if p is not None and p.is_file():
            url = p.read_text(encoding="ascii").strip()
            if url.startswith("data:image/"):
                _lqip_mem_put(key, url)
                return url
    except Exception:
        pass
    return ""


def _attach_lqip_to_page_items(device_id: str, page_items: list) -> None:
    """Fill empty ``lqip`` fields on the sliced page only (bounded IO)."""
    try:
        for it in page_items:
            try:
                if not isinstance(it, dict) or it.get("lqip"):
                    continue
                abs_path = str(it.get("abs_path", ""))
                prefix = f"ios://{device_id}"
                if not abs_path.startswith(prefix):
                    continue
                dev_path = abs_path[len(prefix) :] or "/"
                url = _lqip_lookup_for_page(device_id, dev_path, it.get("size_bytes"), it.get("mtime"))
                if url:
                    it["lqip"] = url
            except Exception:
                continue
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Persistent thumbnail fail cache (24 h TTL, size+mtime keyed)
# ---------------------------------------------------------------------------
def _thumb_fail_key(device_id: str, dev_path: str, fsize: Any, mtime: Any) -> str:
    raw = f"{device_id}\x00{dev_path}\x00{fsize}\x00{mtime}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _thumb_fail_get(device_id: str, dev_path: str, fsize: Any, mtime: Any) -> str | None:
    """Stored fallback reason, or None on miss/expiry. Never raises."""
    try:
        conn = _listing_l2_connect()
        try:
            cur = conn.execute(
                "SELECT reason, cached_at FROM thumb_fail_cache WHERE fail_key=?",
                (_thumb_fail_key(device_id, dev_path, fsize, mtime),),
            )
            row = cur.fetchone()
            if row is None:
                return None
            reason, cached_at = str(row[0]), float(row[1])
            if (time.time() - cached_at) > _THUMB_FAIL_TTL_S:
                try:
                    conn.execute(
                        "DELETE FROM thumb_fail_cache WHERE fail_key=?",
                        (_thumb_fail_key(device_id, dev_path, fsize, mtime),),
                    )
                    conn.commit()
                except Exception:
                    pass
                return None
            if reason not in _PERSISTENT_FAIL_REASONS:
                return None
            if reason in ("no-pillow-heif", "undecodable") and dev_path.lower().endswith((".heic", ".heif")):
                if _ensure_heif_opener():
                    try:
                        conn.execute(
                            "DELETE FROM thumb_fail_cache WHERE fail_key=?",
                            (_thumb_fail_key(device_id, dev_path, fsize, mtime),),
                        )
                        conn.commit()
                    except Exception:
                        pass
                    return None
            return reason
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return None


def _thumb_fail_put(device_id: str, dev_path: str, fsize: Any, mtime: Any, reason: str) -> None:
    """Persist a truly-undecodable reason. Never raises. Never caches transient."""
    try:
        if reason not in _PERSISTENT_FAIL_REASONS:
            return
        conn = _listing_l2_connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO thumb_fail_cache (fail_key, reason, cached_at) VALUES (?, ?, ?)",
                (_thumb_fail_key(device_id, dev_path, fsize, mtime), reason, time.time()),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("thumb fail-cache store skipped: %s", exc)


def _thumb_fail_clear(device_id: str, dev_path: str, fsize: Any, mtime: Any) -> None:
    """Drop a fail entry after a later success (e.g. codec installed)."""
    try:
        conn = _listing_l2_connect()
        try:
            conn.execute(
                "DELETE FROM thumb_fail_cache WHERE fail_key=?",
                (_thumb_fail_key(device_id, dev_path, fsize, mtime),),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass


def _sort_ios_preview_items(items: list, sort_by: str) -> list:
    """Sort preview items in place per sort_by; returns the same list.

    Extracted from _build_ios_preview_items so a cached walk result can be
    re-sorted for a different sort_by without re-walking the device.
    """
    reverse = sort_by in ("newest", "size_desc", "name_desc")
    key_fn = (
        (lambda x: x["mtime"])
        if sort_by in ("newest", "oldest")
        else (lambda x: x["size_bytes"])
        if sort_by in ("size_desc", "size_asc")
        else (lambda x: x["filename"].lower())
    )
    items.sort(key=key_fn, reverse=reverse)
    return items


def _build_ios_preview_items(
    entries: list, *, device_id: str, path: str, recursive: bool, sort_by: str
) -> tuple[list, int, int, int]:
    """Filter + shape + sort raw device entries into preview items.

    Semantics identical to the inline endpoint logic: skip dirs for flat
    listings, keep only supported media extensions, sort per sort_by.
    The item list is capped at _PREVIEW_MAX_FILES to bound memory (mirrors
    the local /preview cap); camera rolls below the cap are unaffected.
    Returns (items, photos, videos, total_size_bytes).
    """
    items: list[dict] = []
    total_photos = 0
    total_videos = 0
    total_size = 0

    for entry in entries:
        if not recursive and entry.is_dir:
            continue
        fname = entry.name
        ext = os.path.splitext(fname)[1].lower()
        if ext not in PREVIEW_EXTENSIONS:
            continue
        device_abs = f"{path.rstrip('/')}/{fname}" if not recursive else entry.path
        item_type = "photo" if ext in PREVIEW_IMAGE_EXTENSIONS else "video"
        size_bytes = entry.size
        mtime = entry.mtime
        # LQIP is RAM-cache-only here (zero IO, zero USB): page-slice time
        # backfills from the on-disk LQIP store for items still empty.
        try:
            _lqip_inline = _lqip_mem_get(_lqip_key(device_id, device_abs, size_bytes, mtime))
        except Exception:
            _lqip_inline = ""
        items.append(
            {
                "id": _file_id(f"{device_id}:{device_abs}"),
                "filename": fname,
                "abs_path": f"ios://{device_id}{device_abs}",
                "type": item_type,
                "size_bytes": size_bytes,
                "mtime": mtime,
                "duration_s": None,
                "thumbnail_ready": False,
                "lqip": _lqip_inline or "",
            }
        )
        total_size += size_bytes
        if item_type == "photo":
            total_photos += 1
        else:
            total_videos += 1
        if len(items) >= _PREVIEW_MAX_FILES:
            break

    _sort_ios_preview_items(items, sort_by)
    return items, total_photos, total_videos, total_size


def _listing_find_reusable(
    device_id: str, path: str, recursive: bool, snapshot: tuple[int, str] | None
) -> tuple[tuple, _ListingCacheEntry] | None:
    """Find any cached entry for the same walk regardless of sort_by.

    The cache key includes sort_by, but the walk result does not depend on
    it — when the snapshot matches, the caller can re-sort the cached items
    instead of re-walking the device. Returns (key, entry) or None.
    """
    if snapshot is None:
        return None
    with _listing_cache_lock:
        for k, ent in list(_listing_cache.items()):
            if len(k) < 3:
                continue
            if k[0] == device_id and k[1] == path and bool(k[2]) == recursive:
                if ent.snapshot is not None and ent.snapshot == snapshot:
                    return k, ent
    return None


def _purge_device_preview_state(device_id: str) -> None:
    """Drop cached listing + pooled AFC slots after a lock/trust failure.

    A preview-time lock means the device state flipped under us: the next
    device-list poll must re-probe trust fresh instead of serving a stale
    Ready row or a stale page from the listing cache. Best-effort only.
    """
    try:
        purge_listing_cache_for_serial(device_id)
    except Exception:
        pass
    try:
        drop_afc_slots(device_id)
    except Exception:
        pass


async def _fetch_ios_entries(manager: Any, device_id: str, path: str, recursive: bool) -> list:
    """Full device fetch (recursive strict walk or single browse) with HTTP error mapping.

    The recursive walk runs in strict mode: any per-subdirectory failure
    raises ``PartialWalkError`` (re-raised raw, never converted) so callers
    never cache a partial walk as complete. Only successful walks are ever
    cached — lock/trust failures purge the listing cache and drop pooled
    AFC slots (transient treatment) before raising, so no Ready state
    survives a preview-time lock. Transient USB failures (timeout, busy,
    stalled, slot dropped, econnreset, broken pipe — same markers as
    :func:`_is_transient_thumb_error`) map to 503 + Retry-After:2 with a
    ``transient`` status so the frontend retries, never 400.
    """
    try:
        if recursive:
            from backend.engines.scanner import _walk_ios_directory

            return await _walk_ios_directory(device_id, path, strict=True)
        return await manager.browse_device(device_id, path)
    except DeviceLockedError as exc:
        _purge_device_preview_state(device_id)
        raise HTTPException(
            status_code=423,
            detail={"status": "locked", "message": exc.message},
        )
    except DeviceNotTrustedError as exc:
        _purge_device_preview_state(device_id)
        raise HTTPException(
            status_code=403,
            detail={"status": "not_trusted", "message": exc.message},
        )
    except WpdDeviceAccessDenied as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "wpd_denied",
                "message": str(exc),
            },
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "status": "not_found",
                "message": f"Path not found on device: {path}",
            },
        )
    except Exception as exc:
        # Partial walks propagate raw: the caller serves stale or
        # partial-not-cached without ever storing them as complete.
        if type(exc).__name__ == "PartialWalkError":
            raise
        if _is_transient_thumb_error(exc):
            raise HTTPException(
                status_code=503,
                headers={"Retry-After": "2"},
                detail={
                    "status": "transient",
                    "message": f"Device temporarily unavailable while listing {path}: {exc}. "
                    "Reconnect the device and retry.",
                },
            )
        error_text = str(exc)
        exc_lower = error_text.lower()
        if "locked" in exc_lower or "lock" in exc_lower:
            _purge_device_preview_state(device_id)
            raise HTTPException(
                status_code=423,
                detail={
                    "status": "locked",
                    "message": "Your iPhone is locked. Please unlock it and "
                    "tap 'Trust This Computer' when prompted, then try again.",
                },
            )
        if "trust" in exc_lower or "pair" in exc_lower or "paired" in exc_lower:
            _purge_device_preview_state(device_id)
            raise HTTPException(
                status_code=403,
                detail={
                    "status": "not_trusted",
                    "message": "Please tap 'Trust This Computer' on your "
                    "iPhone and enter your passcode, then try again.",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": f"Could not list device folder {path}: {error_text}"},
        )


def _maybe_refresh_ios_listing(key: tuple, device_id: str, path: str, recursive: bool, sort_by: str) -> bool:
    """Schedule one deduped background refresh for a stale cache entry."""
    with _listing_cache_lock:
        if key in _listing_refresh_inflight:
            return False
        _listing_refresh_inflight.add(key)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with _listing_cache_lock:
            _listing_refresh_inflight.discard(key)
        return False
    loop.create_task(_refresh_ios_listing(key, device_id, path, recursive, sort_by))
    return True


async def _refresh_ios_listing(key: tuple, device_id: str, path: str, recursive: bool, sort_by: str) -> None:
    """Background re-walk for a stale entry; never raises, never caches partial walks."""
    try:
        mgr = get_device_manager()
        try:
            devices, _ = await mgr.list_devices()
            if device_id not in {getattr(d, "serial", None) for d in devices}:
                _listing_drop(key)
                return
        except Exception:
            pass
        if recursive:
            from backend.engines.scanner import PartialWalkError as _PartialWalkError
            from backend.engines.scanner import _walk_ios_directory

            try:
                entries = await _walk_ios_directory(device_id, path, strict=True)
            except _PartialWalkError as pw:
                logger.warning(
                    "ios-preview background refresh skipped store (partial walk): device=%s path=%s failures=%s/%s: %s",
                    device_id,
                    path,
                    getattr(pw, "failures", "?"),
                    getattr(pw, "total", "?"),
                    pw,
                )
                return
        else:
            try:
                entries = await mgr.browse_device(device_id, path)
            except Exception as exc:
                logger.debug("ios-preview background refresh browse failed: %s", exc)
                return
        snap = await _listing_quick_snapshot(mgr, device_id, path)
        if snap is None and not recursive:
            snap = _listing_snapshot_of(entries)
        items, photos, videos, total = _build_ios_preview_items(
            entries, device_id=device_id, path=path, recursive=recursive, sort_by=sort_by
        )
        _listing_store(key, items, photos, videos, total, snap, time.monotonic())
        logger.debug(
            "ios-preview background refresh done device=%s path=%s recursive=%s items=%d",
            device_id,
            path,
            recursive,
            len(items),
        )
    except Exception as exc:
        logger.debug("ios-preview background refresh failed for %s:%s: %s", device_id, path, exc)
    finally:
        with _listing_cache_lock:
            _listing_refresh_inflight.discard(key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_supported_media(ext: str) -> bool:
    return ext.lower() in PREVIEW_EXTENSIONS


def _is_photo(ext: str) -> bool:
    return ext.lower() in PREVIEW_IMAGE_EXTENSIONS


def _is_video(ext: str) -> bool:
    return ext.lower() in PREVIEW_VIDEO_EXTENSIONS


def _file_id(abs_path: str) -> str:
    return hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:12]


def _get_video_duration(path: str) -> float | None:
    """Run ffprobe to get video duration in seconds. Returns None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        import json as _json

        data = _json.loads(result.stdout)
        duration = data.get("format", {}).get("duration")
        if duration:
            return round(float(duration), 2)
        # Fallback: try streams
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return round(float(dur), 2)
    except Exception:
        pass
    return None


def _generate_gray_fallback() -> bytes:
    """Return a 1x1 gray JPEG pixel as fallback."""
    return _gray_fallback_jpeg()


def _generate_photo_thumbnail(path: str, size: int) -> bytes | None:
    """Generate thumbnail for a photo using EXIF embedded preview fast-path, then Pillow."""
    _ensure_heif_opener()
    ext = os.path.splitext(path)[1].lower()

    # Fast path 1: Extract embedded JPEG thumbnail from EXIF (JPEG, HEIC, RAW files)
    if ext in (".jpg", ".jpeg", ".heic", ".heif", ".cr2", ".cr3", ".nef", ".arw", ".dng"):
        try:
            from backend.engines.metadata_extractor import extract_embedded_thumbnail_bytes

            embedded = extract_embedded_thumbnail_bytes(Path(path))
            if embedded and len(embedded) > 500:
                from PIL import Image, ImageOps
                from PIL.Image import Resampling

                thumb_img = Image.open(_io.BytesIO(embedded))
                thumb_img = ImageOps.exif_transpose(thumb_img) or thumb_img
                thumb_img.thumbnail((size, size), Resampling.BILINEAR)
                if thumb_img.mode not in ("RGB",):
                    thumb_img = thumb_img.convert("RGB")
                buf = _io.BytesIO()
                thumb_img.save(buf, format="JPEG", quality=80)
                thumb_img.close()
                return buf.getvalue()
        except Exception:
            pass

    # Fast path 2: Pillow decode with draft mode for JPEG and fast bilinear resampling
    img = None
    try:
        from PIL import Image, ImageOps
        from PIL.Image import Resampling

        img = Image.open(path)
        if ext in (".jpg", ".jpeg"):
            try:
                img.draft("RGB", (size * 2, size * 2))
            except Exception:
                pass
        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((size, size), Resampling.BILINEAR)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return None
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def _extract_frame_from_bytes(data: bytes, size: int, suffix: str) -> bytes | None:
    """Extract one JPEG frame from in-memory video bytes (multi-strategy ffmpeg spawn).

    Used for device video prefixes: fast input seek, accurate seek, output seek.
    Returns None when the prefix lacks decodable frames (e.g. moov at EOF).
    """
    if not data or len(data) <= 100:
        return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".mp4", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        # Fast path 1: Check for embedded preview / poster frame in video container
        if len(data) >= 4096:
            try:
                from backend.engines.metadata_extractor import extract_embedded_thumbnail_bytes

                embedded = extract_embedded_thumbnail_bytes(Path(tmp_path))
                if embedded and len(embedded) > 500:
                    from PIL import Image as _Image
                    from PIL import ImageOps as _ImageOps
                    from PIL.Image import Resampling as _Resampling

                    thumb = _Image.open(_io.BytesIO(embedded))
                    try:
                        thumb = _ImageOps.exif_transpose(thumb) or thumb
                        thumb.thumbnail((size, size), _Resampling.BILINEAR)
                        if thumb.mode not in ("RGB",):
                            thumb = thumb.convert("RGB")
                        buf = _io.BytesIO()
                        thumb.save(buf, format="JPEG", quality=80)
                        return buf.getvalue()
                    finally:
                        try:
                            thumb.close()
                        except Exception:
                            pass
            except Exception:
                pass

        seek_commands = [
            [
                "ffmpeg",
                "-ss",
                "0.5",
                "-i",
                tmp_path,
                "-vframes",
                "1",
                "-vf",
                f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                "-f",
                "mjpeg",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            [
                "ffmpeg",
                "-ss",
                "00:00:00",
                "-accurate_seek",
                "-i",
                tmp_path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                "-f",
                "mjpeg",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            [
                "ffmpeg",
                "-i",
                tmp_path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                "-f",
                "mjpeg",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
        ]

        for cmd in seek_commands:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
                    return result.stdout
            except subprocess.TimeoutExpired:
                raise
            except (OSError, Exception) as exc:
                logger.debug("ffmpeg frame extraction attempt failed: %s", exc)

    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("ffmpeg frame extraction failed: %s", exc)
    except Exception as exc:
        logger.debug("ffmpeg frame extraction error: %s", exc)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return None


def _generate_video_thumbnail(path: str, size: int) -> bytes | None:
    """Generate thumbnail for a video using ffmpeg with fast input seeking."""
    try:
        # Fast input-seeking (-ss BEFORE -i) seeks directly to keyframe in milliseconds
        result = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                "00:00:01",
                "-noaccurate_seek",
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and len(result.stdout) > 100:
            return result.stdout
        result2 = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                "00:00:00",
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result2.returncode == 0 and len(result2.stdout) > 100:
            return result2.stdout
    except FileNotFoundError:
        logger.debug("ffmpeg not found on PATH; video thumbnails unavailable")
    except subprocess.TimeoutExpired:
        logger.debug("ffmpeg timed out for %s", path)
    except Exception as exc:
        logger.debug("ffmpeg error for %s: %s", path, exc)
    return None


def _generate_photo_thumbnail_from_bytes(data: bytes, size: int, suffix: str = ".heic") -> bytes | None:
    """Generate thumbnail from raw bytes (for iOS/remote files).

    Tries an embedded-EXIF thumbnail first (fast path for JPEG/HEIC).
    Falls back to full decode with Pillow.
    """
    _ensure_heif_opener()
    img = None
    try:
        from PIL import Image, ImageOps

        # Fast path: try to extract embedded thumbnail from partial byte buffer.
        # Most iOS HEIC/JPEG files embed a ~30-80 KB JPEG preview in their EXIF
        # header, which is present in the first 128 KB of the file.
        # Uses the persistent binary ExifTool session (no per-file spawn);
        # the prefix bytes go through a small temp file (~256 KB write).
        try:
            from backend.engines.metadata_extractor import extract_embedded_thumbnail_bytes

            embedded: bytes | None = None
            if data and len(data) >= 4096:
                import tempfile as _tempfile

                _tmp_path = ""
                try:
                    with _tempfile.NamedTemporaryFile(suffix=suffix or ".heic", delete=False) as _tmp:
                        _tmp.write(data)
                        _tmp_path = _tmp.name
                    embedded = extract_embedded_thumbnail_bytes(_tmp_path)
                finally:
                    if _tmp_path:
                        try:
                            os.unlink(_tmp_path)
                        except OSError:
                            pass
            if embedded and len(embedded) > 500:
                # Validate and resize the embedded thumbnail
                from PIL.Image import Resampling

                thumb_img = Image.open(_io.BytesIO(embedded))
                thumb_img = ImageOps.exif_transpose(thumb_img) or thumb_img
                thumb_img.thumbnail((size, size), Resampling.BILINEAR)
                if thumb_img.mode not in ("RGB",):
                    thumb_img = thumb_img.convert("RGB")
                buf = _io.BytesIO()
                thumb_img.save(buf, format="JPEG", quality=80)
                thumb_img.close()
                return buf.getvalue()
        except Exception:
            pass  # Fall through to full Pillow decode

        from PIL.Image import Resampling

        img = Image.open(_io.BytesIO(data))
        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((size, size), Resampling.BILINEAR)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return None
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Out-of-process thumbnail decode (backend/engines/thumb_worker.py)
# ---------------------------------------------------------------------------
# Pillow native codecs (bad HEIC) and ffmpeg (hung spawn) run in a fresh
# child process per job — the macOS Quick Look XPC / Windows dllhost
# provider pattern. The event loop only blocks a threadpool thread on a
# bounded wait; a segfault or hang dies with the child. Mapping preserves
# the existing endpoint semantics with no API shape change:
#   * photo bytes/path crash/timeout -> None (partial stage falls back to
#     the full read; full/local stage falls into the grey-fallback tail).
#   * video frame crash -> None (existing prefix-no-frame 503 path);
#     worker timeout -> re-raised as subprocess.TimeoutExpired so the
#     existing `except subprocess.TimeoutExpired` 503 ffmpeg-timeout branch
#     fires unchanged.
# LRU + L2 disk cache, the persistent fail-cache and LQIP capture stay here
# in the parent untouched.


async def _thumb_photo_path_worker(path: str, size: int) -> bytes | None:
    """Local photo decode out-of-process; crash/timeout -> None (grey tail)."""
    try:
        from backend.engines import thumb_worker as _tw

        return await asyncio.to_thread(_tw.generate_photo_from_path, path, size)
    except Exception as exc:
        logger.debug("thumb worker photo-path failed for %s: %s", path, exc)
        return None


async def _thumb_video_path_worker(path: str, size: int) -> bytes | None:
    """Local video frame extraction out-of-process; crash/timeout -> None."""
    try:
        from backend.engines import thumb_worker as _tw

        return await asyncio.to_thread(_tw.generate_video_from_path, path, size)
    except Exception as exc:
        logger.debug("thumb worker video-path failed for %s: %s", path, exc)
        return None


async def _thumb_photo_bytes_worker(data: bytes, size: int, suffix: str = ".heic") -> bytes | None:
    """iOS photo decode out-of-process; crash/timeout -> None (fall through)."""
    try:
        from backend.engines import thumb_worker as _tw

        return await asyncio.to_thread(_tw.generate_photo_from_bytes, data, size, suffix)
    except Exception as exc:
        logger.debug("thumb worker photo-bytes failed (%d bytes): %s", len(data) if data else 0, exc)
        return None


async def _thumb_frame_bytes_worker(data: bytes, size: int, suffix: str) -> bytes | None:
    """iOS video prefix frame extraction out-of-process.

    Worker timeouts re-raise as subprocess.TimeoutExpired to preserve the
    existing 503 ffmpeg-timeout branch; crashes -> None (prefix-no-frame).
    """
    try:
        from backend.engines import thumb_worker as _tw

        return await asyncio.to_thread(_tw.extract_frame_from_bytes, data, size, suffix)
    except subprocess.TimeoutExpired:
        raise
    except TimeoutError as exc:
        # ThumbWorkerTimeout is a TimeoutError: convert so the caller's
        # `except subprocess.TimeoutExpired` 503 branch fires unchanged.
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=25) from exc
    except Exception as exc:
        logger.debug("thumb worker frame-bytes failed (%d bytes): %s", len(data) if data else 0, exc)
        return None


# ---------------------------------------------------------------------------
# Endpoint A: GET /api/device/preview
# ---------------------------------------------------------------------------


@router.get("/preview")
async def preview_directory(
    path: str = Query(..., description="Absolute path to the source directory"),
    recursive: bool = Query(False, description="Scan subdirectories recursively"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=5000),
    include_duration: bool = Query(False, description="Run ffprobe for video durations (slow)"),
    sort_by: str = Query("newest", pattern="^(newest|oldest|name_asc|name_desc|size_desc|size_asc)$"),
    _: None = Depends(require_local_token),
):
    abs_path = os.path.abspath(path)

    if not os.path.exists(abs_path):
        raise HTTPException(status_code=400, detail="Path does not exist")
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="Path is not a directory")
    if os.path.ismount(abs_path) or abs_path in ("C:\\", "C:/", "/"):
        # Allow drive roots explicitly but cap recursive walks below
        pass

    items: list[dict] = []
    total_photos = 0
    total_videos = 0
    total_size = 0

    try:
        scan_iter = os.scandir(abs_path)

        if recursive:
            # Recursive walk — collect first, then process (capped)
            all_entries: list[str] = []
            base_depth = abs_path.rstrip(os.sep).count(os.sep)
            for root, dirs, files in os.walk(abs_path):
                depth = root.count(os.sep) - base_depth
                if depth > _PREVIEW_MAX_DEPTH:
                    dirs[:] = []
                    continue
                # Prune symlinked dirs to avoid escape/loops
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for fname in files:
                    fpath = os.path.join(root, fname)
                    all_entries.append(fpath)
                    if len(all_entries) >= _PREVIEW_MAX_FILES:
                        break
                if len(all_entries) >= _PREVIEW_MAX_FILES:
                    dirs[:] = []
                    break

            for entry in all_entries:
                ext = (
                    os.path.splitext(entry.name)[1].lower()
                    if hasattr(entry, "name")
                    else os.path.splitext(entry)[1].lower()
                )
                if ext not in PREVIEW_EXTENSIONS:
                    continue
                fpath = getattr(entry, "path", entry) if isinstance(entry, os.DirEntry) else entry
                fpath_str = str(fpath)
                if not os.path.isfile(fpath_str):
                    continue
                try:
                    stat = os.stat(fpath_str)
                except OSError:
                    continue
                item_type = "photo" if ext in PREVIEW_IMAGE_EXTENSIONS else "video"
                item: dict[str, Any] = {
                    "id": _file_id(fpath_str),
                    "filename": os.path.basename(fpath_str),
                    "abs_path": fpath_str,
                    "type": item_type,
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "duration_s": None,
                    "thumbnail_ready": False,
                }
                if item_type == "video" and include_duration:
                    item["duration_s"] = _get_video_duration(fpath_str)
                items.append(item)
                total_size += stat.st_size
                if item_type == "photo":
                    total_photos += 1
                else:
                    total_videos += 1
        else:
            for entry in scan_iter:
                if entry.is_dir(follow_symlinks=False):
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in PREVIEW_EXTENSIONS:
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                item_type = "photo" if ext in PREVIEW_IMAGE_EXTENSIONS else "video"
                item: dict[str, Any] = {
                    "id": _file_id(entry.path),
                    "filename": entry.name,
                    "abs_path": entry.path,
                    "type": item_type,
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "duration_s": None,
                    "thumbnail_ready": False,
                }
                if item_type == "video" and include_duration:
                    item["duration_s"] = _get_video_duration(entry.path)
                items.append(item)
                total_size += stat.st_size
                if item_type == "photo":
                    total_photos += 1
                else:
                    total_videos += 1
    except PermissionError:
        raise HTTPException(status_code=400, detail=f"Permission denied: {path}")

    # Sort based on sort_by parameter
    _SORT_KEYS = {
        "newest": (lambda x: x["mtime"], True),
        "oldest": (lambda x: x["mtime"], False),
        "name_asc": (lambda x: x["filename"].lower(), False),
        "name_desc": (lambda x: x["filename"].lower(), True),
        "size_desc": (lambda x: x["size_bytes"], True),
        "size_asc": (lambda x: x["size_bytes"], False),
    }
    sort_key, sort_reverse = _SORT_KEYS.get(sort_by, _SORT_KEYS["newest"])
    items.sort(key=sort_key, reverse=sort_reverse)

    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    offset = (page - 1) * page_size
    page_items = items[offset : offset + page_size]

    likely_dup_paths: list[str] = []
    new_paths: list[str] = []
    try:
        from backend.engines.duplicate_detector import prescan_against_library

        prescan_candidates = [
            {
                "abs_path": it.get("abs_path") or "",
                "filename": it.get("filename") or "",
                "size_bytes": it.get("size_bytes") or 0,
            }
            for it in items
        ]
        prescan_res = await prescan_against_library(prescan_candidates)
        likely_dup_paths = prescan_res.get("likely_duplicate_paths") or []
        new_paths = prescan_res.get("new_paths") or []
    except Exception as p_err:
        logger.debug("Inline preview duplicate prescan skipped: %s", p_err)

    return {
        "total": total,
        "photos": total_photos,
        "videos": total_videos,
        "total_size_bytes": total_size,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "items": page_items,
        "likely_duplicate_paths": likely_dup_paths,
        "likely_duplicate_count": len(likely_dup_paths),
        "new_paths": new_paths,
        "new_count": len(new_paths),
    }


# ---------------------------------------------------------------------------
# Endpoint B: GET /api/device/thumbnail
# ---------------------------------------------------------------------------


@router.get("/thumbnail")
async def device_thumbnail(
    path: str = Query(..., description="Absolute path of the source file"),
    size: int = Query(200, ge=32, le=512),
    file_size: int | None = Query(None, description="Source file size (cache validation)"),
    file_mtime: float | None = Query(None, description="Source file mtime (cache validation)"),
    retry: int | None = Query(
        None, description="Client retry counter — ignored for cache keys so retries hit disk/RAM cache"
    ),
    cache: str | None = Query(None, description="Cache policy: 'only' returns 404 on miss without decode IO"),
    _token_auth: None = Depends(require_local_token_or_query),
):
    _ignored_retry = retry  # explicitly ignored: must not bust the disk/RAM cache keys below
    abs_path = os.path.abspath(path)

    if not os.path.exists(abs_path):
        return Response(
            content=_generate_gray_fallback(),
            media_type="image/jpeg",
            headers={"X-Thumb-Status": "not-found"},
        )

    try:
        if file_size is None or file_mtime is None:
            st = os.stat(abs_path)
            if file_size is None:
                file_size = st.st_size
            if file_mtime is None:
                file_mtime = st.st_mtime
    except OSError as exc:
        logger.debug("local thumbnail stat failed for %s: %s", abs_path, exc)
        return Response(
            content=_generate_gray_fallback(),
            media_type="image/jpeg",
            headers={"X-Thumb-Status": "not-found"},
        )

    if file_size and file_size > 200 * 1024 * 1024:
        return Response(
            content=_generate_gray_fallback(),
            media_type="image/jpeg",
            headers={"X-Thumb-Status": "file-too-large"},
        )

    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in PREVIEW_EXTENSIONS:
        return Response(
            content=_generate_gray_fallback(),
            media_type="image/jpeg",
            headers={"X-Thumb-Status": "unsupported-type"},
        )

    if ext in (".heic", ".heif"):
        _ensure_heif_opener()

    cache_key = (abs_path, size)
    cached = _get_thumb_cache(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable", "X-Thumb-Status": "hit"},
        )

    disk_key = _device_thumb_disk_key("local", abs_path, size, file_size, file_mtime)
    disk_cached = await asyncio.to_thread(_read_disk_thumb, disk_key)
    if disk_cached is not None:
        _put_thumb_cache(cache_key, disk_cached)
        return Response(
            content=disk_cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable", "X-Thumb-Status": "hit"},
        )

    if cache == "only":
        return Response(
            status_code=404,
            headers={"X-Thumb-Status": "miss"},
        )

    # Persistent negative cache (24 h TTL): skip re-decoding files already
    # proven undecodable this session or a previous one. Header unchanged.
    try:
        _local_fail = _thumb_fail_get("local", abs_path, file_size, file_mtime)
    except Exception:
        _local_fail = None
    if _local_fail is not None:
        return Response(
            content=_generate_gray_fallback(),
            media_type="image/jpeg",
            headers={"X-Thumb-Status": "fallback-undecodable"},
        )

    # Out-of-process decode (thumb_worker): a bad image/ffmpeg hang dies
    # with the child; crash/timeout folds into the grey-fallback tail below.
    if ext in PREVIEW_IMAGE_EXTENSIONS:
        jpeg_bytes = await _thumb_photo_path_worker(abs_path, size)
    else:
        jpeg_bytes = await _thumb_video_path_worker(abs_path, size)

    is_real = bool(jpeg_bytes and len(jpeg_bytes) > 10)
    if not is_real:
        logger.debug(
            "local thumbnail undecodable for %s (ext=%s heif_available=%s) — grey fallback",
            abs_path,
            ext,
            _HEIF_AVAILABLE,
        )
    if is_real and jpeg_bytes is not None:
        result: bytes = jpeg_bytes
    else:
        result = _generate_gray_fallback()

    _put_thumb_cache(cache_key, result)
    if is_real:
        await asyncio.to_thread(_write_disk_thumb, disk_key, result)
        try:
            _thumb_fail_clear("local", abs_path, file_size, file_mtime)
        except Exception:
            pass
        try:
            _store_lqip_for_thumb("local", abs_path, file_size, file_mtime, result)
        except Exception:
            pass
    else:
        try:
            _thumb_fail_put("local", abs_path, file_size, file_mtime, "undecodable")
        except Exception:
            pass

    return Response(
        content=result,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "X-Thumb-Status": "ok" if is_real else "fallback-undecodable",
        },
    )


# ---------------------------------------------------------------------------
# Endpoint C: GET /api/device/ios-preview
# ---------------------------------------------------------------------------


@router.get("/ios-preview")
async def ios_preview_directory(
    request: Request,
    device_id: str = Query(..., description="iOS device serial"),
    path: str = Query(..., description="Virtual path on device, e.g. /DCIM/100APPLE"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=5000),
    sort_by: str = Query("newest", pattern="^(newest|oldest|name_asc|name_desc|size_desc|size_asc)$"),
    recursive: bool = Query(False, description="Recursively scan subdirectories"),
    _: None = Depends(require_local_token),
):
    manager = get_device_manager()

    # Verify device is connected and ready via unified manager
    devices, _tier = await manager.list_devices()
    device = next((d for d in devices if d.serial == device_id), None)

    if device is None:
        raise HTTPException(
            status_code=404,
            detail={
                "status": "disconnected",
                "message": "Device is not connected or not reachable. "
                "Ensure the device is plugged in via USB and unlocked.",
            },
        )

    # Early-exit for terminal device states that no backend can work around.
    # A preview-time lock also purges cached pages/slots so the next poll
    # re-probes trust fresh (lock is transient, never cached as Ready).
    if device.status == DeviceStatus.LOCKED:
        _purge_device_preview_state(device_id)
        raise HTTPException(
            status_code=423,
            detail={
                "status": "locked",
                "message": "Your iPhone is locked. Please unlock it and tap "
                "'Trust This Computer' when prompted, then try again.",
            },
        )
    if device.status == DeviceStatus.NOT_TRUSTED:
        _purge_device_preview_state(device_id)
        raise HTTPException(
            status_code=403,
            detail={
                "status": "not_trusted",
                "message": "Please tap 'Trust This Computer' on your iPhone and enter your passcode, then try again.",
            },
        )

    req_t0 = time.perf_counter()
    cache_key = _listing_cache_key(device_id, path, recursive, sort_by)
    try:
        _listing_purge_disconnected({getattr(d, "serial", None) for d in devices})
    except Exception:
        pass

    # AFC keep-alive: reuse a warm pooled slot when one was used within
    # ~20s, else one cheap bounded NOOP (never holds the lock beyond one
    # op). Best-effort with a short cap — a slow device must not stall
    # the listing; the normal path below proceeds regardless.
    warm_ms = 0.0
    try:
        _wm0 = time.perf_counter()
        await asyncio.wait_for(ensure_afc_warm(device_id), timeout=5.0)
        warm_ms = (time.perf_counter() - _wm0) * 1000.0
    except Exception as exc:
        logger.debug("ios-preview AFC warmup skipped for %s: %s", device_id, exc)

    items: list[dict] | None = None
    total_photos = 0
    total_videos = 0
    total_size = 0
    cache_status = "miss"
    scan_id: str | None = None
    walk_ms = 0.0
    snap_ms = 0.0

    if recursive:
        # Stale-while-revalidate: ANY cached entry (fresh or aged) serves
        # its page immediately with zero synchronous AFC round trips, and a
        # deduped background refresh revalidates it. Only a total cache
        # miss pays for a synchronous snapshot check + full walk, and a
        # snapshot failure with no cache to serve falls through to the
        # walk (whose own transient mapping yields 503 + Retry-After).
        # Pagination/sort/filter always slice the cached list; totals stay
        # frozen per scan ID. Partial walks are never stored.
        from backend.engines.scanner import PartialWalkError

        cached = _listing_cache_get(cache_key)
        now = time.monotonic()
        if cached is not None:
            items, total_photos, total_videos, total_size = (
                cached.items,
                cached.photos,
                cached.videos,
                cached.total_size,
            )
            scan_id = cached.scan_id
            if (now - cached.cached_at) <= _LISTING_TTL_S:
                cache_status = "hit"
            else:
                cache_status = "stale"
            _listing_touch(cache_key, now)
            _maybe_refresh_ios_listing(cache_key, device_id, path, True, sort_by)
        else:
            # Total miss — abort promptly when the frontend already went
            # away (15 s abort) so rapid page turns don't pile up walks.
            try:
                if await request.is_disconnected():
                    raise HTTPException(
                        status_code=499,
                        detail={"status": "client-closed", "message": "Client disconnected."},
                    )
            except HTTPException:
                raise
            except Exception:
                pass
            _s0 = time.perf_counter()
            snap = await _listing_quick_snapshot(manager, device_id, path)
            snap_ms = (time.perf_counter() - _s0) * 1000.0
            reused = _listing_find_reusable(device_id, path, True, snap)
            if reused is None:
                # L2 (SQLite): same snapshot digest survives restarts, so a
                # restart costs one browse instead of a full AFC rewalk.
                reused = _listing_l2_reused_entry(device_id, path, True, snap)
            if reused is not None:
                reuse_key, reuse_ent = reused
                items = _sort_ios_preview_items(list(reuse_ent.items), sort_by)
                total_photos, total_videos, total_size = (
                    reuse_ent.photos,
                    reuse_ent.videos,
                    reuse_ent.total_size,
                )
                cache_status = "hit" if reuse_key == cache_key else "hit-sort-reuse"
                scan_id = _listing_store(
                    cache_key, items, total_photos, total_videos, total_size, snap, time.monotonic()
                )
            else:
                try:
                    if await request.is_disconnected():
                        raise HTTPException(
                            status_code=499,
                            detail={"status": "client-closed", "message": "Client disconnected."},
                        )
                except HTTPException:
                    raise
                except Exception:
                    pass
                _w0 = time.perf_counter()
                try:
                    entries = await _fetch_ios_entries(manager, device_id, path, True)
                except PartialWalkError as pw:
                    walk_ms = (time.perf_counter() - _w0) * 1000.0
                    logger.warning(
                        "ios-preview partial walk NOT cached device=%s path=%s failures=%s/%s walk_ms=%.1f: %s",
                        device_id,
                        path,
                        getattr(pw, "failures", "?"),
                        getattr(pw, "total", "?"),
                        walk_ms,
                        pw,
                    )
                    if _is_transient_thumb_error(pw) or _is_transient_thumb_error(getattr(pw, "__cause__", None)):
                        raise HTTPException(
                            status_code=503,
                            headers={"Retry-After": "2"},
                            detail={
                                "status": "transient",
                                "message": f"Device temporarily unavailable while listing {path}: {pw}. "
                                "Reconnect the device and retry.",
                            },
                        )
                    # Non-transient partial with nothing cached: serve what
                    # we got but NEVER store it (totals may drift on retry,
                    # the next miss re-walks fresh).
                    partial_entries = getattr(pw, "partial", []) or []
                    items, total_photos, total_videos, total_size = _build_ios_preview_items(
                        partial_entries, device_id=device_id, path=path, recursive=True, sort_by=sort_by
                    )
                    cache_status = "partial-not-cached"
                    scan_id = _scan_id_for(device_id, path, True, None)
                else:
                    walk_ms = (time.perf_counter() - _w0) * 1000.0
                    items, total_photos, total_videos, total_size = _build_ios_preview_items(
                        entries, device_id=device_id, path=path, recursive=True, sort_by=sort_by
                    )
                    _s0 = time.perf_counter()
                    snap = await _listing_quick_snapshot(manager, device_id, path)
                    snap_ms += (time.perf_counter() - _s0) * 1000.0
                    scan_id = _listing_store(
                        cache_key, items, total_photos, total_videos, total_size, snap, time.monotonic()
                    )
    else:
        # Flat fast path: a fresh cache entry (age <= TTL) serves the page
        # with zero AFC round trips — pagination/sort/filter slice the
        # cached list and totals stay frozen per scan ID. Only aged/missing
        # entries pay for the single browse, which doubles as the snapshot
        # for validation and cross-sort reuse.
        cached = _listing_cache_get(cache_key)
        now = time.monotonic()
        if cached is not None and (now - cached.cached_at) <= _LISTING_TTL_S:
            items, total_photos, total_videos, total_size = (
                cached.items,
                cached.photos,
                cached.videos,
                cached.total_size,
            )
            scan_id = cached.scan_id
            cache_status = "hit"
            _listing_touch(cache_key, now)
        else:
            _w0 = time.perf_counter()
            entries = await _fetch_ios_entries(manager, device_id, path, False)
            walk_ms = (time.perf_counter() - _w0) * 1000.0
            snap = _listing_snapshot_of(entries)
            if cached is not None and cached.snapshot == snap:
                items, total_photos, total_videos, total_size = (
                    cached.items,
                    cached.photos,
                    cached.videos,
                    cached.total_size,
                )
                scan_id = cached.scan_id
                cache_status = "hit"
                _listing_touch(cache_key, time.monotonic())
            else:
                # Same-walk, different-sort entry: re-sort instead of rebuilding.
                reused = _listing_find_reusable(device_id, path, False, snap)
                if reused is None:
                    reused = _listing_l2_reused_entry(device_id, path, False, snap)
                if reused is not None:
                    _reuse_key, reuse_ent = reused
                    items = _sort_ios_preview_items(list(reuse_ent.items), sort_by)
                    total_photos, total_videos, total_size = (
                        reuse_ent.photos,
                        reuse_ent.videos,
                        reuse_ent.total_size,
                    )
                    cache_status = "hit-sort-reuse"
                    scan_id = _listing_store(
                        cache_key, items, total_photos, total_videos, total_size, snap, time.monotonic()
                    )
                else:
                    items, total_photos, total_videos, total_size = _build_ios_preview_items(
                        entries, device_id=device_id, path=path, recursive=False, sort_by=sort_by
                    )
                    scan_id = _listing_store(
                        cache_key, items, total_photos, total_videos, total_size, snap, time.monotonic()
                    )

    assert items is not None
    if not scan_id:
        # Defensive: every branch above stores or reuses, so this only
        # fires if a future branch forgets — totals still frozen per key.
        scan_id = _scan_id_for(device_id, path, recursive, None)
    total_ms = (time.perf_counter() - req_t0) * 1000.0
    logger.debug(
        "ios-preview device=%s path=%s recursive=%s sort=%s page=%d cache=%s scan=%s "
        "warm_ms=%.1f walk_ms=%.1f snapshot_ms=%.1f total_ms=%.1f items=%d",
        device_id,
        path,
        recursive,
        sort_by,
        page,
        cache_status,
        scan_id,
        warm_ms,
        walk_ms,
        snap_ms,
        total_ms,
        len(items),
    )

    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    start_idx = (page - 1) * page_size
    page_items = items[start_idx : start_idx + page_size]
    # Inline LQIP backfill for the sliced page only (local cache IO, no USB).
    _attach_lqip_to_page_items(device_id, page_items)

    cached_ent = None
    with _listing_cache_lock:
        cached_ent = _listing_cache.get(cache_key)

    likely_dup_paths: list[str] = []
    new_paths: list[str] = []
    if cached_ent and cached_ent.likely_duplicate_paths is not None and cached_ent.new_paths is not None:
        likely_dup_paths = cached_ent.likely_duplicate_paths
        new_paths = cached_ent.new_paths
    else:
        try:
            from backend.engines.duplicate_detector import prescan_against_library

            prescan_candidates = [
                {
                    "abs_path": it.get("abs_path") or "",
                    "filename": it.get("filename") or "",
                    "size_bytes": it.get("size_bytes") or 0,
                }
                for it in items
            ]
            prescan_res = await prescan_against_library(prescan_candidates)
            likely_dup_paths = prescan_res.get("likely_duplicate_paths") or []
            new_paths = prescan_res.get("new_paths") or []
            if cached_ent:
                cached_ent.likely_duplicate_paths = likely_dup_paths
                cached_ent.new_paths = new_paths
        except Exception as p_err:
            logger.debug("Inline ios-preview duplicate prescan skipped: %s", p_err)

    return {
        "total": total,
        "photos": total_photos,
        "videos": total_videos,
        "total_size_bytes": total_size,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "items": page_items,
        "scan_id": scan_id,
        "cache_status": cache_status,
        "likely_duplicate_paths": likely_dup_paths,
        "likely_duplicate_count": len(likely_dup_paths),
        "new_paths": new_paths,
        "new_count": len(new_paths),
    }


# ---------------------------------------------------------------------------
# Endpoint D: GET /api/device/ios-thumbnail
# ---------------------------------------------------------------------------


@router.get("/ios-thumbnail")
async def ios_thumbnail(
    device_id: str = Query(...),
    path: str = Query(..., description="Virtual path on device, e.g. /DCIM/100APPLE/IMG_0042.HEIC"),
    size: int = Query(200, ge=32, le=512),
    file_size: int | None = Query(None, description="Source file size (cache validation, from listing)"),
    file_mtime: float | None = Query(None, description="Source file mtime (cache validation, from listing)"),
    retry: int | None = Query(
        None, description="Client retry counter — ignored for cache keys so retries hit disk/RAM cache"
    ),
    cache: str | None = Query(None, description="Cache policy: 'only' returns 404 on miss without USB/decode IO"),
    _token_auth: None = Depends(require_local_token_or_query),
):
    _ignored_retry = retry  # explicitly ignored: must not bust the disk/RAM cache keys below
    if len(path) > 1024 or ".." in path.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail="Invalid device path")
    # Stat-failure rows (browse_device fallback) report size=0/mtime=0 —
    # using those as disk-cache key components would bust L2 (a later
    # listing with real stat values would miss, and vice versa). Treat
    # zero/negative values as unknown so the key stays stable.
    if not file_size:
        file_size = None
    if not file_mtime:
        file_mtime = None
    cache_key = (f"ios:{device_id}:{path}", size)
    cached = _get_thumb_cache(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable", "X-Thumb-Status": "hit"},
        )
    disk_key = _device_thumb_disk_key(device_id, path, size, file_size, file_mtime)
    disk_cached = await asyncio.to_thread(_read_disk_thumb, disk_key)
    if disk_cached is not None:
        _put_thumb_cache(cache_key, disk_cached)
        return Response(
            content=disk_cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable", "X-Thumb-Status": "hit"},
        )

    if cache == "only":
        return Response(
            status_code=404,
            headers={"X-Thumb-Status": "miss"},
        )

    ext = os.path.splitext(path)[1].lower()
    if ext in (".heic", ".heif"):
        _ensure_heif_opener()
    jpeg_bytes: bytes | None = None
    transient_exc: BaseException | None = None
    fail_reason = "unknown"

    # Persistent negative cache: truly-undecodable files (24 h TTL,
    # size+mtime keyed) skip Pillow/ffmpeg AND USB entirely.
    if ext in PREVIEW_IMAGE_EXTENSIONS:
        try:
            _fail_hit = _thumb_fail_get(device_id, path, file_size, file_mtime)
        except Exception:
            _fail_hit = None
        if _fail_hit is not None:
            return Response(
                content=_generate_gray_fallback(),
                media_type="image/jpeg",
                headers={"X-Thumb-Status": f"fallback-{_fail_hit}"},
            )

    try:
        if ext in PREVIEW_IMAGE_EXTENSIONS:
            # Reuse a warm pooled AFC slot when one was used recently (same
            # keep-alive as the listing path); bounded and best-effort so a
            # slow device never stalls the thumbnail.
            try:
                await asyncio.wait_for(ensure_afc_warm(device_id), timeout=5.0)
            except Exception as exc:
                logger.debug("ios-thumbnail AFC warmup skipped for %s: %s", device_id, exc)
            # ----------------------------------------------------------------
            # TWO-STAGE FAST PATH: Read only the first 256 KB of the file.
            # iOS HEIC/JPEG files embed a small JPEG preview (typically 20-80 KB)
            # in the EXIF header, which sits in the first ~128 KB of the file.
            # Downloading 256 KB vs a full 5-8 MB file is ~20-30x faster.
            # Stage 1 (partial-prefix embedded-EXIF) runs first; only on
            # failure do we pay for Stage 2 (full read). Blocking Pillow
            # decodes run in the threadpool so the event loop never blocks.
            # ----------------------------------------------------------------
            PARTIAL_READ_BYTES = 256 * 1024  # 256 KB

            partial_bytes: bytes | None = None
            partial_exc: BaseException | None = None
            try:
                # Gated: listing/snapshot browses never take this semaphore,
                # so thumbnail reads yield AFC slots to page-turn listings.
                async with _IOS_THUMB_SEMAPHORE:
                    partial_bytes = await _read_device_file_partial(device_id, path, max_bytes=PARTIAL_READ_BYTES)
            except Exception as exc:
                partial_exc = exc
                logger.debug("iOS partial read raised for %s: %s", path, exc)

            if partial_bytes and len(partial_bytes) >= 4096:
                # Out-of-process: EXIF-extract + Pillow decode run in the
                # worker child, never on the event loop or its threads.
                jpeg_bytes = await _thumb_photo_bytes_worker(partial_bytes, size, ext)
                if jpeg_bytes and len(jpeg_bytes) > 10:
                    logger.debug("iOS partial-read thumbnail hit for %s (%d bytes)", path, len(partial_bytes))
                else:
                    logger.debug(
                        "iOS partial-prefix decode failed for %s (%d bytes, "
                        "heif_available=%s, partial_truncated=%s) — falling back to full read",
                        path,
                        len(partial_bytes),
                        _ensure_heif_opener(),
                        len(partial_bytes) < PARTIAL_READ_BYTES,
                    )
                    fail_reason = (
                        "no-pillow-heif"
                        if (ext in (".heic", ".heif") and not _ensure_heif_opener())
                        else "partial-truncated-no-exif"
                    )
                    jpeg_bytes = None
            else:
                logger.debug(
                    "iOS partial read unusable for %s (bytes=%s, heif_available=%s) — falling back to full read",
                    path,
                    len(partial_bytes) if partial_bytes else 0,
                    _ensure_heif_opener(),
                )
                fail_reason = "partial-truncated"
                if partial_exc is not None and _is_transient_thumb_error(partial_exc):
                    transient_exc = partial_exc

            # Stage 2: full-read fallback before ever returning grey.
            if not jpeg_bytes or len(jpeg_bytes) <= 10:
                jpeg_bytes = None
                if file_size and file_size > 50 * 1024 * 1024:
                    fail_reason = "file-too-large"
                else:
                    try:
                        async with _IOS_THUMB_SEMAPHORE:
                            file_bytes = await get_device_manager().read_device_file(device_id, path)
                        if file_bytes and len(file_bytes) > 100:
                            jpeg_bytes = await _thumb_photo_bytes_worker(file_bytes, size, ext)
                            if jpeg_bytes and len(jpeg_bytes) > 10:
                                logger.debug("iOS full-read thumbnail hit for %s (%d bytes)", path, len(file_bytes))
                            else:
                                logger.debug(
                                    "iOS full-read decode failed for %s (%d bytes, ext=%s, "
                                    "heif_available=%s) — truly undecodable",
                                    path,
                                    len(file_bytes),
                                    ext,
                                    _ensure_heif_opener(),
                                )
                                fail_reason = (
                                    "no-pillow-heif"
                                    if (ext in (".heic", ".heif") and not _ensure_heif_opener())
                                    else "undecodable"
                                )
                        else:
                            logger.debug("iOS full read empty for %s — transient?", path)
                            fail_reason = "full-read-empty"
                    except FileNotFoundError as exc:
                        logger.debug("iOS thumbnail not-found for %s: %s", path, exc)
                        return Response(
                            content=b"",
                            media_type="image/jpeg",
                            status_code=404,
                            headers={"X-Thumb-Status": "not-found"},
                        )
                    except Exception as exc:
                        logger.debug("iOS full-read thumbnail error for %s: %s (%s)", path, exc, fail_reason)
                        if _is_transient_thumb_error(exc) and not _is_lookup_failure(exc):
                            transient_exc = exc
                            return Response(
                                content=b"",
                                media_type="image/jpeg",
                                status_code=503,
                                headers={
                                    "Retry-After": "2",
                                    "X-Thumb-Status": "transient-read-failed",
                                },
                            )
                        fail_reason = f"full-read-failed: {exc}"

        else:
            # Video fast path: read only the first 4 MB and extract one
            # frame with a single input-seek ffmpeg spawn (no duration
            # probe, no full download). Phone videos with the moov box up
            # front (faststart) resolve immediately; others resolve to a
            # retryable 503 (not a grey 200) so the frontend shows a badge
            # and can retry instead of thinking the thumbnail loaded.
            # ffmpeg runs in the threadpool (blocking subprocess).
            # NOTE: the prefix read below takes _IOS_THUMB_SEMAPHORE (the
            # listing/browse path never does) so thumbnails yield AFC slots
            # to page-turn snapshot browses.
            try:
                await asyncio.wait_for(ensure_afc_warm(device_id), timeout=5.0)
            except Exception as exc:
                logger.debug("ios-thumbnail AFC warmup skipped for %s: %s", device_id, exc)
            prefix_bytes: bytes | None = None
            try:
                async with _IOS_THUMB_SEMAPHORE:
                    prefix_bytes = await _read_device_file_partial(device_id, path, max_bytes=_VIDEO_PREFIX_BYTES)
            except Exception as exc:
                logger.debug("iOS video prefix read raised for %s: %s", path, exc)
                if _is_transient_thumb_error(exc):
                    return Response(
                        content=b"",
                        media_type="image/jpeg",
                        status_code=503,
                        headers={"Retry-After": "2", "X-Thumb-Status": "transient-read-failed"},
                    )
            if prefix_bytes and len(prefix_bytes) > 4096:
                try:
                    jpeg_bytes = await _thumb_frame_bytes_worker(prefix_bytes, size, ext or ".mp4")
                except subprocess.TimeoutExpired as exc:
                    logger.debug("iOS ffmpeg timeout for %s: %s", path, exc)
                    return Response(
                        content=b"",
                        media_type="image/jpeg",
                        status_code=503,
                        headers={"Retry-After": "2", "X-Thumb-Status": "ffmpeg-timeout"},
                    )
                if not jpeg_bytes or len(jpeg_bytes) <= 100:
                    logger.debug(
                        "iOS video prefix has no decodable frame for %s (%d bytes) — "
                        "likely moov-at-EOF/truncated prefix",
                        path,
                        len(prefix_bytes),
                    )
                    jpeg_bytes = None
                    fail_reason = "prefix-no-frame"
            else:
                logger.debug(
                    "iOS video prefix read unusable for %s (bytes=%s)",
                    path,
                    len(prefix_bytes) if prefix_bytes else 0,
                )
                fail_reason = "prefix-read-failed"

            # Stage 2 for video: If prefix extraction yields no frame (e.g. MOOV at EOF in Live Photos or
            # non-faststart videos) and file size is <= 16 MB, perform full read fallback.
            if not jpeg_bytes and file_size and file_size <= 16 * 1024 * 1024:
                try:
                    logger.debug(
                        "iOS video prefix had no frame for %s (%d bytes) — attempting full read fallback",
                        path,
                        file_size,
                    )
                    async with _IOS_THUMB_SEMAPHORE:
                        full_video_bytes = await get_device_manager().read_device_file(device_id, path)
                    if full_video_bytes and len(full_video_bytes) > 4096:
                        jpeg_bytes = await _thumb_frame_bytes_worker(full_video_bytes, size, ext or ".mp4")
                        if jpeg_bytes and len(jpeg_bytes) > 10:
                            fail_reason = "ok"
                except Exception as v_err:
                    logger.debug("iOS full video read thumbnail attempt failed for %s: %s", path, v_err)

    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("iOS thumbnail error for %s: %s", path, exc)
        if _is_transient_thumb_error(exc) and not _is_lookup_failure(exc):
            return Response(
                content=b"",
                media_type="image/jpeg",
                status_code=503,
                headers={"Retry-After": "2", "X-Thumb-Status": "transient-read-failed"},
            )
        fail_reason = f"error: {exc}"

    # --- Semantics: grey 200 ONLY for truly undecodable ---------------------
    if jpeg_bytes and len(jpeg_bytes) > 10:
        _put_thumb_cache(cache_key, jpeg_bytes)
        await asyncio.to_thread(_write_disk_thumb, disk_key, jpeg_bytes)
        try:
            _thumb_fail_clear(device_id, path, file_size, file_mtime)
        except Exception:
            pass
        try:
            _store_lqip_for_thumb(device_id, path, file_size, file_mtime, jpeg_bytes)
        except Exception:
            pass
        return Response(
            content=jpeg_bytes,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable", "X-Thumb-Status": "ok"},
        )

    # Video prefix with no decodable frame is NOT truly undecodable (the full
    # file might decode) — signal retryable so the frontend shows a badge.
    if ext not in PREVIEW_IMAGE_EXTENSIONS and fail_reason in ("prefix-no-frame", "prefix-read-failed"):
        return Response(
            content=_generate_gray_fallback(),
            media_type="image/jpeg",
            status_code=503,
            headers={"Retry-After": "2", "X-Thumb-Status": fail_reason},
        )
    if transient_exc is not None:
        return Response(
            content=_generate_gray_fallback(),
            media_type="image/jpeg",
            status_code=503,
            headers={"Retry-After": "2", "X-Thumb-Status": "transient-read-failed"},
        )
    # Truly undecodable (full bytes present but Pillow/ffmpeg cannot decode,
    # unsupported type, or no-heif-codec): grey 200 with a reason header so
    # the frontend can distinguish fallback from a real thumbnail.
    logger.debug("iOS thumbnail grey fallback for %s: reason=%s", path, fail_reason)
    if ext in PREVIEW_IMAGE_EXTENSIONS and fail_reason in _PERSISTENT_FAIL_REASONS:
        try:
            _thumb_fail_put(device_id, path, file_size, file_mtime, fail_reason)
        except Exception:
            pass
    grey = _generate_gray_fallback()
    return Response(
        content=grey,
        media_type="image/jpeg",
        headers={"X-Thumb-Status": f"fallback-{fail_reason}"},
    )
