"""
Transfera v2 — Device Preview API
Fast in-place directory preview (no thumbnails generated during scan)
and lazy thumbnail generation with in-memory LRU cache.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import subprocess
import threading
from collections import OrderedDict

import io as _io
import tempfile

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend.ios_device import browse_device_directory, read_device_file

logger = logging.getLogger(__name__)


async def _read_device_file_partial(device_id: str, path: str, max_bytes: int) -> bytes | None:
    """
    Read only the first *max_bytes* bytes of a file on the iOS device via AFC.

    This is the key optimisation for iOS thumbnail generation: HEIC/JPEG files
    embed a small JPEG preview (~20–80 KB) in their EXIF header, which sits in
    the first ~128 KB of the file.  Reading 256 KB instead of the full 4–8 MB
    is ~20–30× faster over USB.

    Returns bytes on success, None on failure.
    """
    try:
        import asyncio
        from backend.ios_device import _get_afc_service

        afc, lockdown = await _get_afc_service(device_id)
        try:
            handle = await asyncio.to_thread(afc.fopen, path)
            try:
                data = await asyncio.to_thread(afc.fread, handle, max_bytes)
                return data if data else None
            finally:
                try:
                    await asyncio.to_thread(afc.fclose, handle)
                except Exception:
                    pass
        finally:
            try:
                afc.close()
            except Exception:
                pass
            try:
                lockdown.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("_read_device_file_partial failed for %s: %s", path, exc)
        return None

router = APIRouter(prefix="/api/device")

# Supported extensions for preview scanning
PREVIEW_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".heic", ".png", ".webp", ".dng", ".tiff", ".tif", ".bmp",
})
PREVIEW_VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".mts", ".wmv",
})
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
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=5,
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
    """Generate thumbnail for a photo using Pillow."""
    img = None
    try:
        from PIL import Image, ImageOps
        img = Image.open(path)
        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((size, size), Image.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def _generate_video_thumbnail(path: str, size: int) -> bytes | None:
    """Generate thumbnail for a video using ffmpeg."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", path, "-ss", "00:00:01", "-frames:v", "1",
             "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0 and len(result.stdout) > 100:
            return result.stdout
        result2 = subprocess.run(
            ["ffmpeg", "-i", path, "-frames:v", "1",
             "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=10,
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
        try:
            import subprocess as _sp
            import sys as _sys
            from backend.engines.metadata_extractor import _bootstrap_exiftool
            exe = _bootstrap_exiftool()
            if exe and len(data) >= 4096:  # Only worth trying on real data
                result = _sp.run(
                    [exe, "-b", "-ThumbnailImage", "-Charset", "utf8", "-"],
                    input=data,
                    capture_output=True,
                    timeout=5,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0 and result.stdout and len(result.stdout) > 500:
                    # Validate and resize the embedded thumbnail
                    thumb_img = Image.open(_io.BytesIO(result.stdout))
                    thumb_img = ImageOps.exif_transpose(thumb_img) or thumb_img
                    thumb_img.thumbnail((size, size), Image.LANCZOS)
                    if thumb_img.mode not in ("RGB",):
                        thumb_img = thumb_img.convert("RGB")
                    buf = _io.BytesIO()
                    thumb_img.save(buf, format="JPEG", quality=82)
                    thumb_img.close()
                    return buf.getvalue()
        except Exception:
            pass  # Fall through to full Pillow decode

        img = Image.open(_io.BytesIO(data))
        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((size, size), Image.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
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
    page_size: int = Query(100, ge=1, le=500),
    include_duration: bool = Query(False, description="Run ffprobe for video durations (slow)"),
    sort_by: str = Query("newest", pattern="^(newest|oldest|name_asc|name_desc|size_desc|size_asc)$"),
):
    abs_path = os.path.abspath(path)

    if not os.path.exists(abs_path):
        raise HTTPException(status_code=400, detail=f"Path does not exist: {path}")
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {path}")

    items: list[dict] = []
    total_photos = 0
    total_videos = 0
    total_size = 0

    try:
        scan_iter = os.scandir(abs_path)

        if recursive:
            # Recursive walk — collect first, then process
            all_entries: list[os.DirEntry] = []
            for root, dirs, files in os.walk(abs_path):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        all_entries.append(os.DirEntry(fpath))
                    except Exception:
                        continue

            for entry in all_entries:
                ext = os.path.splitext(entry.name)[1].lower() if hasattr(entry, "name") else os.path.splitext(entry)[1].lower()
                if ext not in PREVIEW_EXTENSIONS:
                    continue
                fpath = getattr(entry, "path", entry) if isinstance(entry, os.DirEntry) else entry
                if not os.path.isfile(fpath):
                    continue
                try:
                    stat = os.stat(fpath)
                except OSError:
                    continue
                item_type = "photo" if ext in PREVIEW_IMAGE_EXTENSIONS else "video"
                item = {
                    "id": _file_id(fpath),
                    "filename": os.path.basename(fpath),
                    "abs_path": fpath,
                    "type": item_type,
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "duration_s": None,
                    "thumbnail_ready": False,
                }
                if item_type == "video" and include_duration:
                    item["duration_s"] = _get_video_duration(fpath)
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
                item = {
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
        "newest":    (lambda x: x["mtime"],      True),
        "oldest":    (lambda x: x["mtime"],      False),
        "name_asc":  (lambda x: x["filename"].lower(), False),
        "name_desc": (lambda x: x["filename"].lower(), True),
        "size_desc": (lambda x: x["size_bytes"], True),
        "size_asc":  (lambda x: x["size_bytes"], False),
    }
    sort_key, sort_reverse = _SORT_KEYS.get(sort_by, _SORT_KEYS["newest"])
    items.sort(key=sort_key, reverse=sort_reverse)

    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    offset = (page - 1) * page_size
    page_items = items[offset:offset + page_size]

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
    size: int = Query(200, ge=32, le=1024),
):
    abs_path = os.path.abspath(path)

    if not os.path.exists(abs_path):
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

    # Offload CPU-bound Pillow decode + JPEG encode to the thread pool so the
    # event loop stays free for other requests during thumbnail generation.
    import asyncio
    if ext in PREVIEW_IMAGE_EXTENSIONS:
        jpeg_bytes = await asyncio.to_thread(_generate_photo_thumbnail, abs_path, size)
    else:
        jpeg_bytes = await asyncio.to_thread(_generate_video_thumbnail, abs_path, size)

    if jpeg_bytes is None:
        jpeg_bytes = _generate_gray_fallback()

    _put_thumb_cache(cache_key, jpeg_bytes)
    return Response(
        content=jpeg_bytes,
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
    page_size: int = Query(100, ge=1, le=500),
    sort_by: str = Query("newest", pattern="^(newest|oldest|name_asc|name_desc|size_desc|size_asc)$"),
):
    try:
        entries = await browse_device_directory(device_id, path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Device error: {exc}")

    items: list[dict] = []
    total_photos = 0
    total_videos = 0
    total_size = 0

    for entry in entries:
        if entry.is_dir:
            continue
        fname = entry.name
        ext = os.path.splitext(fname)[1].lower()
        if ext not in PREVIEW_EXTENSIONS:
            continue
        device_abs = f"{path.rstrip('/')}/{fname}"
        item_type = "photo" if ext in PREVIEW_IMAGE_EXTENSIONS else "video"
        size_bytes = entry.size
        mtime = entry.mtime
        items.append({
            "id": _file_id(f"{device_id}:{device_abs}"),
            "filename": fname,
            "abs_path": f"ios://{device_id}{device_abs}",
            "type": item_type,
            "size_bytes": size_bytes,
            "mtime": mtime,
            "duration_s": None,
            "thumbnail_ready": False,
        })
        total_size += size_bytes
        if item_type == "photo":
            total_photos += 1
        else:
            total_videos += 1

    reverse = sort_by in ("newest", "size_desc", "name_desc")
    key_fn = (lambda x: x["mtime"]) if sort_by in ("newest", "oldest") else \
             (lambda x: x["size_bytes"]) if sort_by in ("size_desc", "size_asc") else \
             (lambda x: x["filename"].lower())
    items.sort(key=key_fn, reverse=reverse)

    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    start_idx = (page - 1) * page_size
    page_items = items[start_idx: start_idx + page_size]

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
    size: int = Query(200, ge=32, le=800),
):
    cache_key = (f"ios:{device_id}:{path}", size)
    cached = _get_thumb_cache(cache_key)
    if cached is not None:
        return Response(
            content=cached,
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
                partial_bytes = await _read_device_file_partial(
                    device_id, path, max_bytes=PARTIAL_READ_BYTES
                )
            except Exception as exc:
                logger.debug("iOS partial read failed for %s: %s", path, exc)

            if partial_bytes and len(partial_bytes) >= 4096:
                jpeg_bytes = _generate_photo_thumbnail_from_bytes(partial_bytes, size)

            # Fall back to full read if partial thumbnail extraction failed
            if not jpeg_bytes:
                logger.debug(
                    "iOS partial-read thumbnail failed for %s (%d bytes) — falling back to full read",
                    path, len(partial_bytes) if partial_bytes else 0,
                )
                try:
                    file_bytes = await read_device_file(device_id, path)
                    jpeg_bytes = _generate_photo_thumbnail_from_bytes(file_bytes, size)
                except Exception as exc:
                    logger.debug("iOS full-read thumbnail error for %s: %s", path, exc)

        else:
            # Video: must download full file for ffmpeg frame extraction
            suffix = ext or ".mp4"
            try:
                file_bytes = await read_device_file(device_id, path)
            except Exception as exc:
                logger.debug("iOS video file read error for %s: %s", path, exc)
                file_bytes = None

            if file_bytes:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(file_bytes)
                    tmp_path = tmp.name
                try:
                    jpeg_bytes = _generate_video_thumbnail(tmp_path, size)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    except Exception as exc:
        logger.debug("iOS thumbnail error for %s: %s", path, exc)

    result = jpeg_bytes if (jpeg_bytes and len(jpeg_bytes) > 10) else _generate_gray_fallback()
    _put_thumb_cache(cache_key, result)
    return Response(
        content=result,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )

