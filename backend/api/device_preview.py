"""
Transfera v2 — Device Preview API
Fast in-place directory preview (no thumbnails generated during scan)
and lazy thumbnail generation with in-memory LRU cache.
"""

from __future__ import annotations

import hashlib
import io as _io
import logging
import math
import os
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from backend.api.auth import require_local_token, require_local_token_or_query
from backend.device_backend import DeviceLockedError, DeviceNotTrustedError, DeviceStatus, WpdDeviceAccessDenied
from backend.ios_device import read_device_file
from backend.tier2_manager import get_device_manager

_PREVIEW_MAX_FILES = 5000
_PREVIEW_MAX_DEPTH = 8

logger = logging.getLogger(__name__)

# HEIF opener for Pillow decodes below (mirrors thumbnailer/metadata_extractor).
# Registration is process-global; other modules usually beat us to it, but the
# preview endpoints must not depend on import order.
try:
    from pillow_heif import register_heif_opener as _register_heif_opener

    _register_heif_opener()
except Exception:
    logger.debug("pillow-heif unavailable — HEIF preview thumbnails will fall back to gray")


async def _read_device_file_partial(device_id: str, path: str, max_bytes: int) -> bytes | None:
    """
    Read only the first *max_bytes* bytes of a file on the iOS device via AFC.

    This is the key optimisation for iOS thumbnail generation: HEIC/JPEG files
    embed a small JPEG preview (~20–80 KB) in their EXIF header, which sits in
    the first ~128 KB of the file.  Reading 256 KB instead of the full 4–8 MB
    is ~20–30× faster over USB.

    Routes through the unified manager so Tier 1 (pooled AFC), WPD (helper
    --offset/--length) and Tier 2 (full-read fallback) are all handled.
    Returns bytes on success, None on failure.
    """
    try:
        return await get_device_manager().read_device_file_partial(device_id, path, max_bytes)
    except Exception as exc:
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


_GRAY_FALLBACK_JPEG: bytes = _make_gray_fallback()


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
    return _GRAY_FALLBACK_JPEG


def _generate_photo_thumbnail(path: str, size: int) -> bytes | None:
    """Generate thumbnail for a photo using EXIF embedded preview fast-path, then Pillow."""
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
    """Extract one JPEG frame from in-memory video bytes (single ffmpeg spawn).

    Used for device video prefixes: fast input seek, no duration probe.
    Returns None when the prefix lacks decodable frames (e.g. moov at EOF).
    """
    if not data or len(data) <= 100:
        return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        result = subprocess.run(
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
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
            return result.stdout
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


def _generate_photo_thumbnail_from_bytes(data: bytes, size: int) -> bytes | None:
    """Generate thumbnail from raw bytes (for iOS/remote files).

    Tries an embedded-EXIF thumbnail first (fast path for JPEG/HEIC).
    Falls back to full decode with Pillow.
    """
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
                    with _tempfile.NamedTemporaryFile(suffix=".heic", delete=False) as _tmp:
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

    return {
        "total": total,
        "photos": total_photos,
        "videos": total_videos,
        "total_size_bytes": total_size,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "items": page_items,
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
    _: None = Depends(require_local_token_or_query),
):
    abs_path = os.path.abspath(path)

    if not os.path.exists(abs_path):
        return Response(content=_generate_gray_fallback(), media_type="image/jpeg")

    try:
        if file_size is None or file_mtime is None:
            st = os.stat(abs_path)
            if file_size is None:
                file_size = st.st_size
            if file_mtime is None:
                file_mtime = st.st_mtime
    except OSError:
        return Response(content=_generate_gray_fallback(), media_type="image/jpeg")

    if file_size and file_size > 200 * 1024 * 1024:
        return Response(content=_generate_gray_fallback(), media_type="image/jpeg")

    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in PREVIEW_EXTENSIONS:
        return Response(content=_generate_gray_fallback(), media_type="image/jpeg")

    cache_key = (abs_path, size)
    cached = _get_thumb_cache(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    disk_key = _device_thumb_disk_key("local", abs_path, size, file_size, file_mtime)
    disk_cached = _read_disk_thumb(disk_key)
    if disk_cached is not None:
        _put_thumb_cache(cache_key, disk_cached)
        return Response(
            content=disk_cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    # Offload CPU-bound decode + encode to the thread pool so the
    # event loop stays free for other requests during thumbnail generation.
    import asyncio

    if ext in PREVIEW_IMAGE_EXTENSIONS:
        jpeg_bytes = await asyncio.to_thread(_generate_photo_thumbnail, abs_path, size)
    else:
        jpeg_bytes = await asyncio.to_thread(_generate_video_thumbnail, abs_path, size)

    is_real = bool(jpeg_bytes and len(jpeg_bytes) > 10)
    result = jpeg_bytes if is_real else _generate_gray_fallback()

    _put_thumb_cache(cache_key, result)
    if is_real:
        _write_disk_thumb(disk_key, result)

    return Response(
        content=result,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


# ---------------------------------------------------------------------------
# Endpoint C: GET /api/device/ios-preview
# ---------------------------------------------------------------------------


@router.get("/ios-preview")
async def ios_preview_directory(
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
                "message": f"Device {device_id} is not connected or not reachable. "
                "Ensure the device is plugged in via USB and unlocked.",
            },
        )

    # Early-exit for terminal device states that no backend can work around
    if device.status == DeviceStatus.LOCKED:
        raise HTTPException(
            status_code=423,
            detail={
                "status": "locked",
                "message": "Your iPhone is locked. Please unlock it and tap "
                "'Trust This Computer' when prompted, then try again.",
            },
        )
    if device.status == DeviceStatus.NOT_TRUSTED:
        raise HTTPException(
            status_code=403,
            detail={
                "status": "not_trusted",
                "message": "Please tap 'Trust This Computer' on your iPhone and enter your passcode, then try again.",
            },
        )

    try:
        if recursive:
            from backend.engines.scanner import _walk_ios_directory

            entries = await _walk_ios_directory(device_id, path)
        else:
            entries = await manager.browse_device(device_id, path)
    except DeviceLockedError as exc:
        raise HTTPException(
            status_code=423,
            detail={"status": "locked", "message": exc.message},
        )
    except DeviceNotTrustedError as exc:
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
        error_text = str(exc)
        exc_lower = error_text.lower()
        if "locked" in exc_lower or "lock" in exc_lower:
            raise HTTPException(
                status_code=423,
                detail={
                    "status": "locked",
                    "message": "Your iPhone is locked. Please unlock it and "
                    "tap 'Trust This Computer' when prompted, then try again.",
                },
            )
        if "trust" in exc_lower or "pair" in exc_lower or "paired" in exc_lower:
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
            }
        )
        total_size += size_bytes
        if item_type == "photo":
            total_photos += 1
        else:
            total_videos += 1

    reverse = sort_by in ("newest", "size_desc", "name_desc")
    key_fn = (
        (lambda x: x["mtime"])
        if sort_by in ("newest", "oldest")
        else (lambda x: x["size_bytes"])
        if sort_by in ("size_desc", "size_asc")
        else (lambda x: x["filename"].lower())
    )
    items.sort(key=key_fn, reverse=reverse)

    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    start_idx = (page - 1) * page_size
    page_items = items[start_idx : start_idx + page_size]

    return {
        "total": total,
        "photos": total_photos,
        "videos": total_videos,
        "total_size_bytes": total_size,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "items": page_items,
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
    _: None = Depends(require_local_token_or_query),
):
    if len(path) > 1024 or ".." in path.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail="Invalid device path")
    cache_key = (f"ios:{device_id}:{path}", size)
    cached = _get_thumb_cache(cache_key)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )
    disk_key = _device_thumb_disk_key(device_id, path, size, file_size, file_mtime)
    disk_cached = _read_disk_thumb(disk_key)
    if disk_cached is not None:
        _put_thumb_cache(cache_key, disk_cached)
        return Response(
            content=disk_cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    ext = os.path.splitext(path)[1].lower()
    jpeg_bytes: bytes | None = None

    try:
        if ext in PREVIEW_IMAGE_EXTENSIONS:
            # ----------------------------------------------------------------
            # FAST PATH: Read only the first 256 KB of the file.
            # iOS HEIC/JPEG files embed a small JPEG preview (typically 20-80 KB)
            # in the EXIF header, which sits in the first ~128 KB of the file.
            # Downloading 256 KB vs a full 5-8 MB file is ~20-30x faster.
            # If the partial read doesn't contain a usable thumbnail, fall back
            # to a full read.
            # ----------------------------------------------------------------
            PARTIAL_READ_BYTES = 256 * 1024  # 256 KB

            partial_bytes: bytes | None = None
            try:
                partial_bytes = await _read_device_file_partial(device_id, path, max_bytes=PARTIAL_READ_BYTES)
            except Exception as exc:
                logger.debug("iOS partial read failed for %s: %s", path, exc)

            if partial_bytes and len(partial_bytes) >= 4096:
                jpeg_bytes = _generate_photo_thumbnail_from_bytes(partial_bytes, size)

            # Fall back to full read if partial thumbnail extraction failed
            if not jpeg_bytes:
                logger.debug(
                    "iOS partial-read thumbnail failed for %s (%d bytes) — falling back to full read",
                    path,
                    len(partial_bytes) if partial_bytes else 0,
                )
                try:
                    file_bytes = await read_device_file(device_id, path)
                    jpeg_bytes = _generate_photo_thumbnail_from_bytes(file_bytes, size)
                except Exception as exc:
                    logger.debug("iOS full-read thumbnail error for %s: %s", path, exc)

        else:
            # Video fast path: read only the first 8 MB and extract one
            # frame with a single input-seek ffmpeg spawn (no duration
            # probe, no full download). Phone videos with the moov box up
            # front (faststart) resolve immediately; others fall back to
            # the video badge instead of downloading hundreds of MB.
            _VIDEO_PREFIX_BYTES = 8 * 1024 * 1024
            prefix_bytes: bytes | None = None
            try:
                prefix_bytes = await _read_device_file_partial(device_id, path, max_bytes=_VIDEO_PREFIX_BYTES)
            except Exception as exc:
                logger.debug("iOS video prefix read failed for %s: %s", path, exc)
            if prefix_bytes and len(prefix_bytes) > 4096:
                jpeg_bytes = _extract_frame_from_bytes(prefix_bytes, size, ext or ".mp4")

    except Exception as exc:
        logger.debug("iOS thumbnail error for %s: %s", path, exc)

    result = jpeg_bytes if (jpeg_bytes and len(jpeg_bytes) > 10) else _generate_gray_fallback()
    _put_thumb_cache(cache_key, result)
    # Persist real thumbnails (not the gray fallback) for instant revisits.
    if jpeg_bytes and len(jpeg_bytes) > 10:
        _write_disk_thumb(disk_key, jpeg_bytes)
    return Response(
        content=result,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
