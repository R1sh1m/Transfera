"""
Transfera v2 — Thumbnail Generator (memory-only)
Generates JPEG thumbnail bytes using a multi-strategy approach:
1. ExifTool fast-path: extract embedded JPEG thumbnails from EXIF data
   (skipped for formats that cannot structurally contain embedded thumbnails)
2. Pillow decode + resize: full image decode with EXIF-orientation-aware resize
3. ffmpeg frame extraction: extract a single frame from video files
4. rawpy: RAW format decoding (if available)

Returns raw bytes. Never writes to disk.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
THUMBNAIL_MAX_SIZE = 400  # longest edge in pixels
THUMBNAIL_QUALITY = 82   # JPEG quality for generated thumbnails


# ---------------------------------------------------------------------------
# Pillow + pillow-heif initialisation
# ---------------------------------------------------------------------------
_PILLOW_READY = False
_PILLOW_HEIF_READY = False
try:
    from PIL import Image, ImageOps

    _PILLOW_READY = True

    # Pillow 10+ moved Resampling to Image.Resampling.LANCZOS
    _LANCZOS: Any = getattr(Image, "Resampling", Image).LANCZOS  # type: ignore

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _PILLOW_HEIF_READY = True
    except ImportError:
        pass
except ImportError:
    _LANCZOS: Any = 1  # Fallback for typing


# ---------------------------------------------------------------------------
# ffmpeg detection (cached, silent — no warning on every call)
# ---------------------------------------------------------------------------
_FFMPEG_PATH: str | None = None
_ffmpeg_checked = False


def _find_ffmpeg() -> str | None:
    global _FFMPEG_PATH, _ffmpeg_checked
    if _ffmpeg_checked:
        return _FFMPEG_PATH
    _ffmpeg_checked = True
    _FFMPEG_PATH = shutil.which("ffmpeg")
    return _FFMPEG_PATH


# ---------------------------------------------------------------------------
# RawPy detection
# ---------------------------------------------------------------------------
_RAWPY_READY = False
try:
    import rawpy  # type: ignore # noqa: F401
    _RAWPY_READY = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Image extension sets
# ---------------------------------------------------------------------------
_IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".tiff", ".tif", ".webp", ".heic", ".heif",
    ".svg", ".ico", ".cr2", ".cr3", ".nef",
    ".arw", ".dng", ".orf", ".rw2", ".raw",
})

_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
    ".3gp", ".wmv", ".flv", ".mpg", ".mpeg",
})

# Formats that structurally cannot contain an embedded JPEG thumbnail in EXIF
# metadata. Running ExifTool's -ThumbnailImage on these is guaranteed to return
# nothing, so we skip that subprocess entirely for a significant speed win.
# JPEG, HEIC, CR2/CR3, NEF, ARW, DNG — these CAN have embedded thumbnails.
# PNG, BMP, GIF, WebP, TIFF, SVG, ICO — these cannot.
_NO_EMBEDDED_THUMB_EXTS = frozenset({
    ".png", ".bmp", ".gif", ".webp",
    ".tiff", ".tif", ".svg", ".ico",
})


# ---------------------------------------------------------------------------
# ExifTool fast-path: extract embedded thumbnail
# ---------------------------------------------------------------------------
def _extract_embedded_thumbnail(file_path: Path) -> bytes | None:
    """
    Try to extract an embedded JPEG thumbnail from EXIF data using ExifTool.
    Many JPEG and HEIC files contain a small preview image in their EXIF
    metadata. Extracting it is much faster than decoding the full image.

    Only called for formats that can structurally contain embedded thumbnails
    (JPEG, HEIC, RAW formats) — never for PNG/BMP/GIF/WebP/TIFF.
    """
    from backend.engines.metadata_extractor import _bootstrap_exiftool

    exe = _bootstrap_exiftool()
    if not exe:
        return None

    try:
        result = subprocess.run(
            [
                exe,
                "-b",
                "-ThumbnailImage",
                "-Charset", "utf8",
                str(file_path),
            ],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass

    return None


# ---------------------------------------------------------------------------
# Image thumbnail via Pillow
# ---------------------------------------------------------------------------
def _generate_image_thumbnail(file_path: Path) -> bytes | None:
    """
    Generate JPEG thumbnail bytes from an image file using Pillow.
    Applies EXIF orientation before resizing. Returns JPEG bytes.
    """
    if not _PILLOW_READY:
        return None

    try:
        img = Image.open(file_path)
    except Image.UnidentifiedImageError:
        logger.warning("Corrupt or unsupported image: %s", file_path)
        return None
    except Exception as exc:
        logger.debug("Pillow cannot open %s: %s", file_path.name, exc)
        return None

    try:
        img = ImageOps.exif_transpose(img) or img

        if img.mode not in ("RGB", "RGBA", "L", "P"):
            img = img.convert("RGB")
        elif img.mode in ("RGBA", "P"):
            # Flatten transparency onto white background
            bg = Image.new("RGB", img.size, (255, 255, 255))
            mask = img.split()[3] if img.mode == "RGBA" else None
            bg.paste(img.convert("RGB"), mask=mask)
            img = bg
        elif img.mode == "L":
            img = img.convert("RGB")

        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), _LANCZOS)

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception as exc:
        logger.debug("Thumbnail generation failed for %s: %s", file_path.name, exc)
        return None
    finally:
        img.close()


# ---------------------------------------------------------------------------
# Video thumbnail via ffmpeg (pipe to stdout, no temp files)
# ---------------------------------------------------------------------------
import threading as _threading

_video_thumb_semaphore = _threading.BoundedSemaphore(3)


def _generate_video_thumbnail(file_path: Path) -> bytes | None:
    """
    Extract a single frame from a video file using ffmpeg piped to stdout.
    Uses 10 % of duration as seek point. Returns JPEG bytes or None.
    """
    # The caller (generate_thumbnail_bytes) already checked is_file(), but
    # the file may be deleted between that check and this function running
    # (e.g. a background thread racing with test teardown).  Re-check here
    # to avoid spurious ffmpeg error logs for files that vanished.
    if not file_path.is_file():
        return None

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None

    with _video_thumb_semaphore:
        try:
            duration_result = subprocess.run(
                [
                    ffmpeg,
                    "-i", str(file_path),
                    "-f", "null",
                    "-",
                ],
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stderr = duration_result.stderr.decode("utf-8", errors="replace")
            duration_sec = None
            for line in stderr.splitlines():
                if "Duration:" in line:
                    parts = line.split("Duration:")[1].split(",")[0].strip().split(":")
                    if len(parts) == 3:
                        duration_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                    break

            seek_time = "0.5"
            if duration_sec and duration_sec > 0:
                seek_time = str(max(0.5, duration_sec * 0.1))

            result = subprocess.run(
                [
                    ffmpeg,
                    "-ss", seek_time,
                    "-i", str(file_path),
                    "-vframes", "1",
                    "-vf", f"scale={THUMBNAIL_MAX_SIZE}:{THUMBNAIL_MAX_SIZE}:force_original_aspect_ratio=decrease",
                    "-f", "mjpeg",
                    "-q:v", "3",
                    "-",
                ],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
                return result.stdout

            stderr = result.stderr.decode("utf-8", errors="replace")[:2000]
            logger.error(
                "ffmpeg thumbnail failed for %s (returncode=%d, stdout=%d bytes): %s",
                file_path, result.returncode, len(result.stdout or b""), stderr,
            )
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg thumbnail timed out for %s", file_path)
        except OSError as exc:
            logger.error("ffmpeg thumbnail OS error for %s: %s", file_path, exc)

        return None


# ---------------------------------------------------------------------------
# RAW thumbnail via rawpy (if available)
# ---------------------------------------------------------------------------
def _generate_raw_thumbnail(file_path: Path) -> bytes | None:
    """Decode a raw image and return JPEG thumbnail bytes, or None."""
    if not _RAWPY_READY:
        return None

    try:
        import rawpy  # type: ignore
        with rawpy.imread(str(file_path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                half_size=True,
                no_auto_bright=False,
                output_bps=8,
            )
        from PIL import Image as _PilImage
        img = _PilImage.fromarray(rgb)
        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), _LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
        img.close()
        return buf.getvalue()
    except Exception as exc:
        logger.debug("Raw thumbnail failed for %s: %s", file_path.name, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_thumbnail_bytes(source_path: Path) -> bytes | None:
    """
    Generate a JPEG thumbnail from *source_path*.
    Returns raw JPEG bytes on success, None on failure.
    Never writes to disk.

    Strategy:
    1. For formats with EXIF support (JPEG, HEIC, RAW): try embedded thumbnail
       extraction first (fast) — skipped for formats that can't have them.
    2. Full Pillow decode + resize (works for all raster formats).
    3. ffmpeg frame extraction for video files.
    4. rawpy for RAW camera files.
    """
    path = source_path.resolve()
    if not path.is_file():
        return None

    ext = path.suffix.lower()

    if ext in _IMAGE_EXTENSIONS:
        if ext in (".heic", ".heif") and not _PILLOW_HEIF_READY:
            return None

        # Embedded thumbnail fast path — only for formats that can have one.
        # Skipping PNG/BMP/GIF/WebP eliminates hundreds of wasted ExifTool
        # subprocesses when processing screenshot folders.
        if ext not in _NO_EMBEDDED_THUMB_EXTS:
            embedded = _extract_embedded_thumbnail(path)
            if embedded:
                try:
                    from PIL import Image as _TestImg
                    test = _TestImg.open(io.BytesIO(embedded))
                    test.verify()
                    return embedded
                except Exception:
                    pass

        if _PILLOW_READY:
            result = _generate_image_thumbnail(path)
            if result:
                return result

    if ext in _VIDEO_EXTENSIONS:
        result = _generate_video_thumbnail(path)
        if result:
            return result

    if ext in (".cr2", ".cr3", ".arw", ".nef", ".dng", ".orf", ".rw2", ".raw"):
        result = _generate_raw_thumbnail(path)
        if result:
            return result

    return None
