"""
Transfera v2 — Out-of-process thumbnail worker.

Isolates Pillow/ffmpeg thumbnail decoding in a short-lived child process so a
malformed image (e.g. a bad HEIC hitting native codec code) or a hung ffmpeg
spawn can never crash or stall the API event loop.

Pattern mirrors the OS-native approaches:
  * macOS Quick Look — thumbnails render in a separate XPC service
    (``com.apple.quicklook.ThumbnailsAgent``); a bad file kills the agent,
    never the host app.
  * Windows Explorer — thumbnail providers run in an isolated surrogate host
    (``dllhost.exe``); a crashing provider is terminated, Explorer survives.

Each decode job spawns one fresh child (spawn context, no pool reuse — a
segfault dies with its process), waits with a bounded timeout, then kills a
hung child (terminate -> kill). Results map onto the existing endpoint
semantics in ``backend/api/device_preview.py`` with no API shape change:

  * timeout  -> :class:`ThumbWorkerTimeout` (a :class:`TimeoutError`, so the
    existing ``_is_transient_thumb_error`` treats it as transient -> 503, or
    the caller folds it into the grey-fallback tail).
  * crash    -> :class:`ThumbWorkerCrash` (a plain :class:`RuntimeError`, NOT
    transient -> grey 200 fallback / prefix-no-frame 503 via the existing
    ``jpeg_bytes is None`` paths).
  * undecodable -> ``None`` (exactly like the in-process decoders).

The in-process decode routines are kept as the child-side implementation AND
as the fallback when the worker is disabled
(``TRANSFERA_THUMB_WORKER_DISABLE=1``) or the spawn itself fails, so behavior
is identical with or without isolation. LRU + L2 disk cache, the persistent
fail-cache and LQIP capture all stay in the parent (device_preview) untouched.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public error types + timeouts
# ---------------------------------------------------------------------------


class ThumbWorkerError(RuntimeError):
    """Base class for out-of-process thumbnail worker failures."""


class ThumbWorkerCrash(ThumbWorkerError):
    """The worker child died (segfault, non-zero exit, lost pipe) or errored.

    Deliberately NOT a transient error: callers fold it into the existing
    grey-fallback / prefix-no-frame paths (``jpeg_bytes is None``).
    """


class ThumbWorkerTimeout(ThumbWorkerError, TimeoutError):
    """The worker child exceeded its bounded wait (hung Pillow/ffmpeg).

    Subclasses :class:`TimeoutError` so ``_is_transient_thumb_error`` in
    device_preview maps it to 503 + Retry-After, matching the existing
    ffmpeg-timeout semantics.
    """


# Parent-side bounded waits. Each exceeds the longest child-internal ffmpeg
# timeout for that kind (frame: 15 s, video-path: 5 s + 5 s) with margin.
PHOTO_BYTES_TIMEOUT_S = 25.0
FRAME_BYTES_TIMEOUT_S = 25.0
PHOTO_PATH_TIMEOUT_S = 30.0
VIDEO_PATH_TIMEOUT_S = 20.0

_JOIN_GRACE_S = 5.0
# Sanity cap for bytes payloads piped to the child (video prefix is 4 MB,
# photo partial is 256 KB — anything far larger is a caller bug).
_MAX_BYTES_PAYLOAD = 32 * 1024 * 1024


def _worker_disabled() -> bool:
    return os.environ.get("TRANSFERA_THUMB_WORKER_DISABLE", "").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# In-process decode routines (child-side implementation + spawn fallback)
# ---------------------------------------------------------------------------
# These mirror backend/api/device_preview._generate_photo_thumbnail* /
# _extract_frame_from_bytes / _generate_video_thumbnail output contracts
# (valid JPEG bytes or None; TimeoutExpired only from ffmpeg) without
# importing device_preview (no FastAPI/router import in the child, no import
# cycle in the parent).


def _decode_photo_bytes(data: bytes, size: int) -> bytes | None:
    """Pillow decode of raw image bytes (embedded-EXIF attempt first)."""
    import io as _io

    if not data or len(data) <= 100:
        return None
    # Fast path: embedded EXIF preview via the persistent ExifTool session.
    # Runs in the child already, so a spawn-heavy fallback here is contained.
    try:
        from backend.engines.metadata_extractor import extract_embedded_thumbnail_bytes

        embedded: bytes | None = None
        if len(data) >= 4096:
            _tmp = ""
            try:
                with tempfile.NamedTemporaryFile(suffix=".heic", delete=False) as fh:
                    fh.write(data)
                    _tmp = fh.name
                embedded = extract_embedded_thumbnail_bytes(Path(_tmp))
            finally:
                if _tmp:
                    try:
                        os.unlink(_tmp)
                    except OSError:
                        pass
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
        pass  # fall through to full Pillow decode

    try:
        from PIL import Image as _Image
        from PIL import ImageOps as _ImageOps
        from PIL.Image import Resampling as _Resampling

        img = _Image.open(_io.BytesIO(data))
        try:
            img = _ImageOps.exif_transpose(img) or img
            img.thumbnail((size, size), _Resampling.BILINEAR)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
        finally:
            try:
                img.close()
            except Exception:
                pass
    except Exception:
        return None


def _decode_frame_bytes(data: bytes, size: int, suffix: str) -> bytes | None:
    """Single ffmpeg frame extraction from in-memory video bytes.

    Raises subprocess.TimeoutExpired on ffmpeg hang (caller maps to 503),
    returns None when the bytes hold no decodable frame.
    """
    if not data or len(data) <= 100:
        return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".mp4", delete=False) as tmp:
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
            return bytes(result.stdout)
    except subprocess.TimeoutExpired:
        raise
    except (OSError, Exception) as exc:
        logger.debug("thumb worker frame extraction failed: %s", exc)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return None


def _decode_photo_path(path: str, size: int) -> bytes | None:
    """Pillow decode of a file on disk (embedded-EXIF attempt first)."""
    import io as _io

    try:
        if not os.path.isfile(path):
            return None
    except Exception:
        return None
    ext = os.path.splitext(path)[1].lower()
    # Fast path: embedded thumbnail for formats that can carry one.
    if ext in (".jpg", ".jpeg", ".heic", ".heif", ".cr2", ".cr3", ".nef", ".arw", ".dng"):
        try:
            from backend.engines.metadata_extractor import extract_embedded_thumbnail_bytes

            embedded = extract_embedded_thumbnail_bytes(Path(path))
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
    try:
        from PIL import Image as _Image
        from PIL import ImageOps as _ImageOps
        from PIL.Image import Resampling as _Resampling

        img = _Image.open(path)
        try:
            if ext in (".jpg", ".jpeg"):
                try:
                    img.draft("RGB", (size * 2, size * 2))
                except Exception:
                    pass
            img = _ImageOps.exif_transpose(img) or img
            img.thumbnail((size, size), _Resampling.BILINEAR)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
        finally:
            try:
                img.close()
            except Exception:
                pass
    except Exception:
        return None


def _decode_video_path(path: str, size: int) -> bytes | None:
    """ffmpeg frame extraction from a video file (fast seek, then t=0 retry)."""
    for seek in ("00:00:01", "00:00:00"):
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-ss",
                    seek,
                    "-noaccurate_seek" if seek != "00:00:00" else "-accurate_seek",
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
            if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
                return bytes(result.stdout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("thumb worker video decode failed for %s: %s", path, exc)
        except Exception as exc:
            logger.debug("thumb worker video error for %s: %s", path, exc)
    return None


def _decode_in_process(kind: str, payload: bytes | str, size: int, suffix: str) -> bytes | None:
    """Run the decode for *kind* in this process (child body / fallback)."""
    if kind == "photo_bytes":
        assert isinstance(payload, (bytes, bytearray))
        return _decode_photo_bytes(bytes(payload), size)
    if kind == "frame_bytes":
        assert isinstance(payload, (bytes, bytearray))
        return _decode_frame_bytes(bytes(payload), size, suffix)
    if kind == "photo_path":
        return _decode_photo_path(str(payload), size)
    if kind == "video_path":
        return _decode_video_path(str(payload), size)
    raise ValueError(f"unknown thumb worker kind: {kind!r}")


# ---------------------------------------------------------------------------
# Child entry point (module-level: picklable for spawn)
# ---------------------------------------------------------------------------


def _child_main(conn, kind: str, payload: bytes | str, size: int, suffix: str) -> None:
    """Child body: decode, send ("ok", result) / ("timeout", msg) / ("error", msg)."""
    try:
        result = _decode_in_process(kind, payload, size, suffix)
        try:
            conn.send(("ok", result))
        except Exception:
            pass
    except subprocess.TimeoutExpired as exc:
        try:
            conn.send(("timeout", f"ffmpeg timed out: {exc}"))
        except Exception:
            pass
    except Exception as exc:
        try:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Parent-side spawn + bounded wait
# ---------------------------------------------------------------------------


def _run_in_worker(kind: str, payload: bytes | str, size: int, suffix: str, timeout: float) -> bytes | None:
    """Spawn one fresh child, bound its lifetime, return JPEG bytes or None.

    Raises ThumbWorkerTimeout on timeout, ThumbWorkerCrash when the child
    dies/errors without a result.
    """
    size = int(size)
    if size < 32:
        size = 32
    elif size > 512:
        size = 512
    if isinstance(payload, (bytes, bytearray)) and len(payload) > _MAX_BYTES_PAYLOAD:
        raise ThumbWorkerCrash(f"payload too large ({len(payload)} bytes)")

    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_main, args=(child_conn, kind, payload, size, suffix), daemon=True)
    proc.start()
    try:
        child_conn.close()
    except Exception:
        pass
    timed_out = False
    try:
        if not parent_conn.poll(timeout):
            timed_out = True
        else:
            try:
                status, value = parent_conn.recv()
            except EOFError as exc:
                raise ThumbWorkerCrash(f"worker pipe closed without result (exit={proc.exitcode})") from exc
            try:
                proc.join(_JOIN_GRACE_S)
            except Exception:
                pass
            if status == "ok":
                return value
            if status == "timeout":
                raise ThumbWorkerTimeout(f"thumbnail worker timed out after {timeout:.0f}s ({kind}): {value}")
            raise ThumbWorkerCrash(f"thumbnail worker failed ({kind}): {value}")
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if timed_out or proc.is_alive():
            for _attempt in range(2):
                try:
                    if not proc.is_alive():
                        break
                    proc.terminate()
                    proc.join(_JOIN_GRACE_S)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(_JOIN_GRACE_S)
                except Exception:
                    break
        try:
            proc.close()
        except Exception:
            pass
    if timed_out:
        raise ThumbWorkerTimeout(f"thumbnail worker timed out after {timeout:.0f}s ({kind})")
    raise ThumbWorkerCrash(f"thumbnail worker lost ({kind})")  # pragma: no cover - defensive


def _run_or_fallback(kind: str, payload: bytes | str, size: int, suffix: str, timeout: float) -> bytes | None:
    """Worker decode with in-process fallback (disabled env / spawn failure).

    Worker timeouts/crashes still RAISE (callers map them); only the inability
    to spawn at all falls back inline so thumbnails keep working.
    """
    if _worker_disabled():
        return _decode_in_process(kind, payload, size, suffix)
    try:
        return _run_in_worker(kind, payload, size, suffix, timeout)
    except (ThumbWorkerTimeout, ThumbWorkerCrash):
        raise
    except Exception as exc:
        logger.debug("thumb worker spawn failed (%s) — in-process fallback: %s", kind, exc)
        return _decode_in_process(kind, payload, size, suffix)


# ---------------------------------------------------------------------------
# Public sync API (call via asyncio.to_thread so the event loop never blocks)
# ---------------------------------------------------------------------------


def generate_photo_from_bytes(data: bytes, size: int, *, timeout: float = PHOTO_BYTES_TIMEOUT_S) -> bytes | None:
    """Decode image *data* to JPEG bytes out-of-process. None = undecodable."""
    return _run_or_fallback("photo_bytes", data, size, ".heic", timeout)


def extract_frame_from_bytes(
    data: bytes, size: int, suffix: str = ".mp4", *, timeout: float = FRAME_BYTES_TIMEOUT_S
) -> bytes | None:
    """Extract one ffmpeg frame from video *data* out-of-process.

    Raises ThumbWorkerTimeout (a TimeoutError) on ffmpeg hang so callers can
    preserve the existing 503 ffmpeg-timeout branch; None = no decodable
    frame (existing prefix-no-frame 503 path).
    """
    return _run_or_fallback("frame_bytes", data, size, suffix or ".mp4", timeout)


def generate_photo_from_path(path: str, size: int, *, timeout: float = PHOTO_PATH_TIMEOUT_S) -> bytes | None:
    """Decode the image file at *path* to JPEG bytes out-of-process."""
    started = time.monotonic()
    try:
        st_size = os.path.getsize(path)
    except OSError:
        return None
    if st_size > 200 * 1024 * 1024:
        return None
    # Shrink the parent-side wait by the stat cost (negligible, defensive).
    timeout = max(1.0, timeout - (time.monotonic() - started))
    return _run_or_fallback("photo_path", path, size, "", timeout)


def generate_video_from_path(path: str, size: int, *, timeout: float = VIDEO_PATH_TIMEOUT_S) -> bytes | None:
    """Extract one ffmpeg frame from the video file at *path* out-of-process."""
    try:
        if not os.path.isfile(path):
            return None
    except Exception:
        return None
    return _run_or_fallback("video_path", path, size, "", timeout)
