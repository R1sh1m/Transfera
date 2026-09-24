"""
Transfera v2 — Offline Intelligence Engine.

Optional ONNX-backed faces (SCRFD + ArcFace) and semantic embeddings
(MobileCLIP/SigLIP), with graceful degradation when models are absent.

Design principles (from market research on immich / digiKam / smriti /
mim / Omoide / PrivateLens):
- Everything runs locally; nothing is ever uploaded.
- Heavy models are on-demand downloads into ``backend/data/models/``,
  never required for CI or first launch.
- All entry points return safe fallbacks (``[]`` / ``None`` /
  ``available=False``) when ``onnxruntime`` or model files are missing.
- Keyword fallback (filename + tags + camera + caption FTS) keeps
  semantic search useful with zero models installed.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

MODELS_SUBDIR = Path("models")
FACE_MODELS = ("scrfd_10g.onnx", "arcface_r50.onnx")
CLIP_MODELS = ("mobileclip_s0_image.onnx", "mobileclip_s0_text.onnx")


def models_dir() -> Path:
    try:
        from backend.config import DB_DIR

        d = Path(DB_DIR).parent / "models"
    except Exception:
        d = Path("backend/data/models")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _onnx_available() -> bool:
    try:
        import onnxruntime  # type: ignore  # noqa: F401

        return True
    except ImportError:
        return False


def get_capabilities() -> dict:
    """Report which intelligence features are usable on this machine."""
    md = models_dir()
    faces_ready = _onnx_available() and all((md / m).is_file() for m in FACE_MODELS)
    clip_ready = _onnx_available() and all((md / m).is_file() for m in CLIP_MODELS)
    try:
        from PIL import Image  # noqa: F401

        phash_ready = True
    except ImportError:
        phash_ready = False
    return {
        "phash_available": phash_ready,
        "faces_available": faces_ready,
        "clip_available": clip_ready,
        "onnx_available": _onnx_available(),
        "models_dir": str(md),
        "face_models": list(FACE_MODELS),
        "clip_models": list(CLIP_MODELS),
        "semantic_mode": "clip" if clip_ready else "keyword",
    }


def compute_blur_score(file_path: str | Path) -> float | None:
    """Sharpness proxy via Laplacian variance on a downscaled grayscale.

    Higher = sharper. Pure-Pillow (no OpenCV dep). Returns None when
    undecodable. Used by keeper scoring to prefer the sharpest burst frame.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(Path(file_path)) as img:
            img = ImageOps.exif_transpose(img) or img
            g = img.convert("L").resize((256, 256))
            px = list(g.getdata())  # type: ignore[arg-type]
            w, h = g.size
            # Laplacian kernel variance
            vals: list[float] = []
            for y in range(1, h - 1):
                for x in range(1, w - 1):
                    c = px[y * w + x]
                    lap = 4 * c - px[(y - 1) * w + x] - px[(y + 1) * w + x] - px[y * w + x - 1] - px[y * w + x + 1]
                    vals.append(float(lap))
            if not vals:
                return None
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            return round(var, 2)
    except Exception:
        return None


def extract_faces(file_path: str | Path) -> list[dict]:
    """Face detection stub: returns [] unless SCRFD ONNX models exist.

    Real inference (SCRFD-10G + ArcFace via onnxruntime) plugs in here;
    the schema (bbox, embedding, confidence) is already stable so the
    DB + API layers work today with zero models installed.
    """
    caps = get_capabilities()
    if not caps["faces_available"]:
        return []
    logger.debug("Face models present but inference not yet wired; returning []")
    return []


def keyword_tags(file_name: str, camera_model: str | None = None) -> list[str]:
    """Deterministic keyword tags from filename + camera (no ML needed)."""
    tags: list[str] = []
    name = (file_name or "").lower()
    for kw, tag in [
        ("screenshot", "screenshot"),
        ("screen shot", "screenshot"),
        ("whatsapp", "messaging"),
        ("snapchat", "messaging"),
        ("dsc_", "camera"),
        ("img_", "phone"),
        ("pano", "panorama"),
        ("burst", "burst"),
        ("live", "live-photo"),
    ]:
        if kw in name:
            tags.append(tag)
    if camera_model:
        tags.append(f"camera:{camera_model.strip().lower()[:32]}")
    ext = Path(file_name).suffix.lower() if file_name else ""
    if ext in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"):
        tags.append("video")
    elif ext:
        tags.append("photo")
    return sorted(set(tags))


def cosine_sim(a: list[float], b: list[float]) -> float:
    denom = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if not denom:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / denom


def semantic_search_keyword(
    query: str,
    items: list[dict],
    *,
    limit: int = 50,
) -> list[dict]:
    """Zero-model semantic-ish search over filename/tags/caption/camera.

    Scores: filename hit (3) + tag hit (2 each) + caption hit (2) +
    camera hit (1). Deterministic, offline, testable. CLIP cosine path
    replaces this transparently when models are installed.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    terms = [t for t in q.replace(",", " ").split() if t]
    scored: list[tuple[float, dict]] = []
    for it in items:
        score = 0.0
        name = str(it.get("file_name") or "").lower()
        tags = (
            json.loads(it.get("tags_json") or "[]")
            if isinstance(it.get("tags_json"), str)
            else (it.get("tags_json") or [])
        )
        caption = str(it.get("caption") or "").lower()
        camera = f"{it.get('camera_make') or ''} {it.get('camera_model') or ''}".lower()
        for t in terms:
            if t in name:
                score += 3.0
            if any(t in str(tag).lower() for tag in tags):
                score += 2.0
            if t in caption:
                score += 2.0
            if t in camera:
                score += 1.0
        if score > 0:
            scored.append((score, it))
    scored.sort(key=lambda s: (-s[0], s[1].get("id", 0)))
    return [it for _, it in scored[:limit]]


def cluster_moments(
    items: list[dict],
    *,
    gap_hours: float = 12.0,
) -> list[dict]:
    """Group sorted items into moments/events by capture-time gaps.

    Spatio-temporal lite (time only; geo upgrade path reserved):
    a new moment starts when the gap to the previous item exceeds
    ``gap_hours``. Mirrors Popsa/recasa event detection in simplified,
    deterministic form suitable for unit tests.
    """
    from datetime import datetime

    def _dt(it: dict) -> datetime | None:
        v = it.get("date_taken") or it.get("created_at")
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                return None
        return None

    ordered = sorted([it for it in items if _dt(it) is not None], key=lambda it: _dt(it))  # type: ignore[arg-type]
    moments: list[dict] = []
    current: list[dict] = []
    prev: datetime | None = None
    for it in ordered:
        cur = _dt(it)
        assert cur is not None
        if prev is not None and (cur - prev).total_seconds() > gap_hours * 3600:
            if current:
                moments.append(_summarize_moment(current))
            current = []
        current.append(it)
        prev = cur
    if current:
        moments.append(_summarize_moment(current))
    return moments


def structured_from_tags(tags: dict) -> dict:
    """Parse ExifTool tag dict into structured intelligence fields.

    Handles GPS (deg+ref and decimal), dimensions, camera, duration.
    Never raises; missing keys -> None.
    """

    def _f(v: object) -> float | None:
        try:
            return float(str(v).strip())
        except (ValueError, TypeError, AttributeError):
            return None

    def _i(v: object) -> int | None:
        try:
            return int(float(str(v).strip()))
        except (ValueError, TypeError, AttributeError):
            return None

    out: dict = {
        "width": _i(tags.get("ImageWidth") or tags.get("ExifImageWidth")),
        "height": _i(tags.get("ImageHeight") or tags.get("ExifImageHeight")),
        "camera_make": (str(tags.get("Make") or "").strip() or None),
        "camera_model": (str(tags.get("Model") or "").strip() or None),
        "gps_lat": None,
        "gps_lon": None,
        "duration_s": None,
    }
    lat = _f(tags.get("GPSLatitude"))
    lat_ref = str(tags.get("GPSLatitudeRef") or "").upper()
    lon = _f(tags.get("GPSLongitude"))
    lon_ref = str(tags.get("GPSLongitudeRef") or "").upper()
    if lat is not None:
        out["gps_lat"] = -abs(lat) if lat_ref == "S" else abs(lat)
    if lon is not None:
        out["gps_lon"] = -abs(lon) if lon_ref == "W" else abs(lon)
    dur = tags.get("Duration") or tags.get("MediaDuration")
    if dur is not None:
        d = _f(dur)
        if d is None:
            # "00:01:23.45" form
            try:
                parts = str(dur).strip().split(":")
                d = (
                    float(parts[-1])
                    + (float(parts[-2]) * 60 if len(parts) >= 2 else 0)
                    + (float(parts[-3]) * 3600 if len(parts) >= 3 else 0)
                )
            except (ValueError, IndexError):
                d = None
        out["duration_s"] = d
    return out


def _summarize_moment(group: list[dict]) -> dict:
    from datetime import datetime

    def _dt(it: dict) -> datetime:
        v = it.get("date_taken") or it.get("created_at")
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v))

    start = min(_dt(it) for it in group)
    end = max(_dt(it) for it in group)
    cover = max(group, key=lambda it: it.get("file_size") or 0)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(group),
        "cover_id": cover.get("id"),
        "item_ids": [it.get("id") for it in group],
    }
