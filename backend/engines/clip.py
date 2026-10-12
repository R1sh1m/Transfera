"""
Transfera v2 — On-board CLIP semantic engine (MobileCLIP-S0, ONNX, CPU).

Fully local image<->text embeddings for real semantic search
("sunset" finds sunsets, not just filenames containing "sunset").
Models download on demand from Hugging Face into ``backend/data/models/``
and are cached there; nothing is ever uploaded.

Files (fp32, CPU-friendly sizes):
  mobileclip_s0_image.onnx  (~46 MB, vision encoder, 256x256, dim 512)
  mobileclip_s0_text.onnx   (~170 MB, text encoder, 77 BPE tokens)
  mobileclip_s0_tokenizer.json (~2 MB, CLIP BPE vocab)

Preprocessing follows the upstream preprocessor config exactly:
resize shortest edge to 256, center-crop 256, rescale to [0,1], NO mean/std
normalisation. Text is lowercased CLIP-tokenized with SOT/EOT wrapping and
77-token truncation/padding.

Vector-DB style retrieval lives here too: embeddings are plain L2-normalized
float lists persisted in ``media_embeddings`` (model="mobileclip-s0"); search
is brute-force cosine top-k, which is instant at desktop-library scale and
needs no server process.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

HF_BASE = "https://huggingface.co/Xenova/mobileclip_s0/resolve/main"
MODEL_NAME = "mobileclip-s0"
EMBED_DIM = 512
IMAGE_SIZE = 256
TEXT_CONTEXT = 77

# Canonical on-disk names (match intelligence.CLIP_MODELS for capabilities).
IMAGE_MODEL_FILE = "mobileclip_s0_image.onnx"
TEXT_MODEL_FILE = "mobileclip_s0_text.onnx"
TOKENIZER_FILE = "mobileclip_s0_tokenizer.json"

MODEL_SPECS: tuple[tuple[str, str, int], ...] = (
    # (canonical name, HF path, expected bytes, informational)
    (IMAGE_MODEL_FILE, "onnx/vision_model.onnx", 45_500_000),
    (TEXT_MODEL_FILE, "onnx/text_model.onnx", 170_000_000),
    (TOKENIZER_FILE, "tokenizer.json", 2_200_000),
)

# Per-file network policy: urllib exposes a single timeout covering both
# connect and read, so both budgets are ~30s via the same knob.
_CONNECT_TIMEOUT = 30
_READ_TIMEOUT = 30
_DOWNLOAD_CHUNK = 1024 * 256
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # sleep = base ** attempt (1s, 2s, 4s ...)
_STALLED_AFTER = 120  # seconds without progress before status reports stalled

_sessions_lock = threading.Lock()
_image_session = None  # onnxruntime InferenceSession | None
_text_session = None  # onnxruntime InferenceSession | None
_tokenizer = None  # tokenizers.Tokenizer | None

# Background model-download state (read by the status endpoint).
# RLock (not Lock): start_background_download() calls download_status()
# while already holding it — a plain Lock would deadlock same-thread.
# started_at/last_activity are time.time() floats (None when never started);
# last_activity is touched on every received chunk so a stuck thread is
# detectable via download_status()'s stalled check.
_download_state: dict = {
    "status": "idle",
    "downloaded_bytes": 0,
    "error": None,
    "started_at": None,
    "last_activity": None,
}
_download_lock = threading.RLock()


def _now() -> float:
    return time.time()


def _touch() -> None:
    with _download_lock:
        _download_state["last_activity"] = _now()


def _resume_baseline_bytes() -> int:
    """Sum of existing .part bytes for still-missing models (resume credit)."""
    try:
        root = _models_root()
    except Exception:
        return 0
    total = 0
    for name, _, _ in MODEL_SPECS:
        part = root / name
        part = part.with_suffix(part.suffix + ".part")
        try:
            if part.is_file():
                total += part.stat().st_size
        except OSError:
            pass
    return total


def download_status() -> dict:
    """Snapshot of model presence + any in-progress background download."""
    with _download_lock:
        state = dict(_download_state)
    missing = missing_models()
    state["ready"] = not missing
    state["missing"] = missing
    # Total expected bytes across all specs — drives the frontend's
    # fill-inside-the-pill progress (downloaded_bytes / total_bytes).
    state["total_bytes"] = sum(size for _, _, size in MODEL_SPECS)
    if state["status"] == "downloading" and state["ready"]:
        with _download_lock:
            _download_state["status"] = "ready"
        state["status"] = "ready"
        return state
    # Heartbeat: a download thread that made no progress for _STALLED_AFTER
    # while still claiming "downloading" is stuck (e.g. wedged socket that
    # outlived per-read timeouts). Surface it so the frontend can offer retry.
    if state.get("status") == "downloading":
        last = state.get("last_activity")
        if isinstance(last, (int, float)) and (_now() - last) > _STALLED_AFTER:
            with _download_lock:
                if _download_state.get("status") == "downloading":
                    _download_state["status"] = "error"
                    _download_state["error"] = "stalled — retry"
            state["status"] = "error"
            state["error"] = "stalled — retry"
    return state


def start_background_download() -> dict:
    """Kick off model download in a daemon thread (idempotent)."""
    import threading as _threading

    with _download_lock:
        if _download_state["status"] in ("downloading", "installing-packages"):
            return download_status()
        if not missing_models():
            _download_state["status"] = "ready"
            return download_status()
        _download_state["status"] = "downloading"
        # Progress truth: only reset the counter when starting fresh. When
        # .part files exist we resume, so credit bytes already on disk.
        resume_bytes = _resume_baseline_bytes()
        _download_state["downloaded_bytes"] = resume_bytes
        _download_state["error"] = None
        now = _now()
        _download_state["started_at"] = now
        _download_state["last_activity"] = now

    def _run() -> None:
        try:
            # Runtime first (~120 MB pip install), weights second (~207 MB).
            # Either may already be present; both steps are idempotent.
            if not ensure_ai_packages():
                with _download_lock:
                    _download_state["status"] = "error"
                    _download_state["error"] = "AI library install failed"
                return
            with _download_lock:
                # ensure_ai_packages() flips status to installing-packages;
                # flip back so heartbeat/stalled logic sees the weights phase.
                if _download_state.get("status") == "installing-packages":
                    _download_state["status"] = "downloading"
                _download_state["last_activity"] = _now()
            ok = ensure_models()
            if ok:
                try:
                    from backend.engines.intelligence import ensure_face_models

                    ensure_face_models()
                except Exception as exc:
                    logger.debug("Follow-up face model download skipped: %s", exc)
            with _download_lock:
                _download_state["status"] = "ready" if ok else "error"
                if not ok and not _download_state["error"]:
                    _download_state["error"] = "download failed, see logs"
        except Exception as exc:
            with _download_lock:
                _download_state["status"] = "error"
                _download_state["error"] = str(exc)[:200]

    _threading.Thread(target=_run, daemon=True, name="clip-download").start()
    return download_status()


def _models_root() -> Path:
    from backend.engines.intelligence import models_dir

    return models_dir()


def missing_models() -> list[str]:
    """Canonical model filenames not yet present on disk."""
    root = _models_root()
    return [name for name, _, _ in MODEL_SPECS if not (root / name).is_file()]


def ai_packages_missing() -> bool:
    """True when the optional AI runtime (onnxruntime/tokenizers/numpy)
    is not importable. Import-light: no installs, no downloads."""
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return True
    return False


def _ai_requirements_file() -> Path:
    from backend.config import BACKEND_ROOT

    return Path(BACKEND_ROOT) / "requirements-ai.txt"


def ensure_ai_packages(timeout: int = 600) -> bool:
    """pip-install the optional AI stack (onnxruntime/tokenizers/numpy).

    Runs synchronously in the caller (the download endpoint invokes it from
    its background thread). Returns True when all three import afterwards.
    Best-effort: failure just leaves AI search unavailable with keyword
    fallback intact.
    """
    if not ai_packages_missing():
        return True
    req = _ai_requirements_file()
    if not req.is_file():
        logger.warning("AI requirements file missing: %s", req)
        return False
    import subprocess
    import sys

    if getattr(sys, "frozen", False):
        # The frozen bundle is immutable: sys.executable is the engine exe,
        # not a Python interpreter, so `exe -m pip install` would reboot a
        # duplicate engine instead of installing anything. The AI runtime
        # ships inside the bundle; if the import still fails, AI search
        # stays unavailable with keyword fallback intact.
        logger.warning("AI packages missing in frozen build -- skipping pip install (bundle is immutable)")
        return False
    logger.info("Installing on-board AI packages from %s", req)
    try:
        with _download_lock:
            _download_state["status"] = "installing-packages"
            _download_state["last_activity"] = _now()
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning(
                "AI package install failed (rc=%d): %s",
                proc.returncode,
                (proc.stderr or "")[-500:],
            )
            return False
        ok = not ai_packages_missing()
        logger.info("AI package install %s", "succeeded" if ok else "incomplete")
        return ok
    except Exception as exc:
        logger.warning("AI package install failed: %s", exc)
        return False


def _download_one_file(name: str, rel: str, root: Path) -> bool:
    """Download a single model file with resume + retries.

    Keeps the ``.part`` temp file across attempts/restarts and resumes via
    ``Range`` when the server answers 206; falls back to a full rewrite when
    resume is unsupported. Returns True on success. On exhausted retries,
    records ``"<name>: <HTTP/OS error>"`` (200 chars) in _download_state.
    """
    url = f"{HF_BASE}/{rel}"
    dest = root / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        resume_from = tmp.stat().st_size if tmp.is_file() else 0
    except OSError:
        resume_from = 0

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        # Re-stat: a previous attempt in this loop may have grown .part.
        try:
            resume_from = tmp.stat().st_size if tmp.is_file() else 0
        except OSError:
            resume_from = 0
        headers = {"User-Agent": "Transfera/2.0"}
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
        logger.info(
            "Downloading CLIP model %s (%s)%s attempt %d/%d",
            name,
            url,
            f" resuming at {resume_from} bytes" if resume_from else "",
            attempt + 1,
            _MAX_RETRIES,
        )
        try:
            req = Request(url, headers=headers)
            # Single urllib timeout covers connect + read (~30s each budget).
            with urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
                code = getattr(resp, "status", None)
                try:
                    if code is None and hasattr(resp, "getcode"):
                        code = resp.getcode()
                except Exception:
                    code = None
                resumed = resume_from > 0 and code == 206
                if resume_from > 0 and not resumed:
                    # Server ignored Range: full rewrite, so un-count the
                    # previously credited partial bytes to keep progress true.
                    with _download_lock:
                        _download_state["downloaded_bytes"] = max(0, _download_state["downloaded_bytes"] - resume_from)
                    resume_from = 0
                mode = "ab" if resumed else "wb"
                new_bytes = 0
                with open(tmp, mode) as fh:
                    while True:
                        chunk = resp.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        new_bytes += len(chunk)
                        with _download_lock:
                            # Counts every spec incl. tokenizer (no per-type skip).
                            _download_state["downloaded_bytes"] += len(chunk)
                            _download_state["last_activity"] = _now()
            full_size = resume_from + new_bytes
            if full_size < 1_000_000:
                raise OSError(f"suspiciously small download ({full_size} bytes)")
            tmp.replace(dest)
            logger.info("CLIP model ready: %s (%.1f MB)", name, full_size / 1e6)
            return True
        except Exception as exc:  # noqa: BLE001 — per-file retry + error text
            last_err = exc
            logger.warning(
                "CLIP model download failed for %s (attempt %d/%d): %s",
                name,
                attempt + 1,
                _MAX_RETRIES,
                exc,
            )
            with _download_lock:
                _download_state["last_activity"] = _now()
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_BASE**attempt)
    # Retries exhausted: keep .part on disk for a future resume, and record
    # which file + why so the status endpoint can show it.
    with _download_lock:
        _download_state["error"] = f"{name}: {last_err}"[:200] if last_err else f"{name}: download failed"[:200]
    return False


def _seed_from_bundle(root: Path, missing: list[str]) -> None:
    """Copy missing model files from the packaged bundle into *root*.

    Local duplicate of the seed helper in engines/intelligence.py (kept
    duplicate so intelligence.py never imports clip.py). Same guards:
    skip 0-byte placeholders, size sanity check >1MB, best-effort with
    debug log. Silent when the bundle ships no models.
    """
    try:
        import shutil

        from backend.config import PACKAGED_MODELS_DIR
    except Exception:
        return
    for name in list(missing):
        try:
            dest = root / name
            if dest.is_file():
                continue
            src = PACKAGED_MODELS_DIR / name
            try:
                if not src.is_file() or src.stat().st_size < 1_000_000:
                    continue
            except OSError:
                continue
            try:
                shutil.copy2(src, dest)
            except OSError as exc:
                logger.debug("Bundle model seed failed for %s: %s", name, exc)
                continue
            try:
                if dest.stat().st_size < 1_000_000:
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    continue
            except OSError:
                pass
            logger.debug("Seeded CLIP model from bundle: %s", name)
        except Exception:
            logger.debug("Bundle model seed skipped for %s", name, exc_info=True)


def ensure_models() -> bool:
    """Download any missing model files. Returns True when all present."""
    missing = missing_models()
    if not missing:
        return True
    root = _models_root()
    # Tier 1: first-run seed from the packaged bundle (resources/models/).
    # Only what is still missing afterwards falls through to download.
    _seed_from_bundle(root, missing)
    missing = missing_models()
    if not missing:
        return True
    # Progress truth: if .part bytes exist on disk but the in-memory counter
    # was lost (backend restart), credit them once via max() — never double
    # count bytes already credited by start_background_download().
    baseline = _resume_baseline_bytes()
    if baseline > 0:
        with _download_lock:
            if _download_state["downloaded_bytes"] < baseline:
                _download_state["downloaded_bytes"] = baseline
            _download_state["last_activity"] = _now()
    spec_by_name = {name: (path, size) for name, path, size in MODEL_SPECS}
    for name in missing:
        rel, _expected = spec_by_name[name]
        if not _download_one_file(name, rel, root):
            return False
    return not missing_models()


def _load_tokenizer():
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(_models_root() / TOKENIZER_FILE))
    _tokenizer = tok
    return tok


def _get_sessions():
    """Lazily create (and cache) the two ONNX sessions. Thread-safe."""
    global _image_session, _text_session
    if _image_session is not None and _text_session is not None:
        return _image_session, _text_session
    import onnxruntime as ort

    root = _models_root()
    opts = ort.SessionOptions()
    opts.log_severity_level = 3  # errors only
    opts.intra_op_num_threads = max(1, min(4, (__import__("os").cpu_count() or 2)))
    with _sessions_lock:
        if _image_session is None:
            _image_session = ort.InferenceSession(
                str(root / IMAGE_MODEL_FILE), sess_options=opts, providers=["CPUExecutionProvider"]
            )
        if _text_session is None:
            _text_session = ort.InferenceSession(
                str(root / TEXT_MODEL_FILE), sess_options=opts, providers=["CPUExecutionProvider"]
            )
    return _image_session, _text_session


def clip_available() -> bool:
    """True when runtime + both encoders are usable (downloads NOT triggered)."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return not missing_models()


def _preprocess_image(image) -> list:
    """PIL image -> [1,3,256,256] float32 in [0,1] (upstream spec)."""
    import numpy as _np
    from PIL import ImageOps

    img = ImageOps.exif_transpose(image) or image
    img = img.convert("RGB")
    w, h = img.size
    scale = IMAGE_SIZE / min(w, h)
    img = img.resize((round(w * scale), round(h * scale)), 2)  # BILINEAR
    left = (img.width - IMAGE_SIZE) // 2
    top = (img.height - IMAGE_SIZE) // 2
    img = img.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))
    arr = _np.asarray(img, dtype=_np.float32) / 255.0
    return arr.transpose(2, 0, 1)[None, ...].tolist()


def _tokenize(text: str) -> list[list[int]]:
    """CLIP tokenize: lowercase, BPE, SOT/EOT wrap, pad/truncate to 77."""
    tok = _load_tokenizer()
    enc = tok.encode((text or "").lower())
    sot = tok.token_to_id("<|startoftext|>") or 49406
    eot = tok.token_to_id("<|endoftext|>") or 49407
    ids = [sot, *enc.ids[: TEXT_CONTEXT - 2], eot][:TEXT_CONTEXT]
    pad_id = tok.token_to_id("!") or 0
    ids += [pad_id] * (TEXT_CONTEXT - len(ids))
    return [ids]


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def encode_image(image) -> list[float] | None:
    """Embed a PIL image -> 512-d normalized vector. None on failure."""
    try:
        sess, _ = _get_sessions()
        out: Any = sess.run(None, {sess.get_inputs()[0].name: _preprocess_image(image)})[0]
        row = out[0] if isinstance(out[0], list) else list(out[0])
        return _normalize([float(x) for x in row])
    except Exception as exc:
        logger.debug("CLIP image encode failed: %s", exc)
        return None


def encode_text(text: str) -> list[float] | None:
    """Embed a query string -> 512-d normalized vector. None on failure."""
    try:
        _, sess = _get_sessions()
        out: Any = sess.run(None, {sess.get_inputs()[0].name: _tokenize(text)})[0]
        row = out[0] if isinstance(out[0], list) else list(out[0])
        return _normalize([float(x) for x in row])
    except Exception as exc:
        logger.debug("CLIP text encode failed: %s", exc)
        return None


def cosine_top_k(
    query: list[float],
    candidates: list[tuple[int, list[float]]],
    *,
    limit: int = 50,
    min_score: float = 0.15,
) -> list[tuple[int, float]]:
    """Brute-force cosine top-k over (media_id, vector) pairs.

    Vectors are expected L2-normalized, so cosine == dot product.
    ``min_score`` filters noise: random pairs sit near ~0.1 for CLIP.
    """
    scored: list[tuple[int, float]] = []
    for mid, vec in candidates:
        if len(vec) != len(query):
            continue
        s = sum(x * y for x, y in zip(query, vec))
        if s >= min_score:
            scored.append((mid, s))
    scored.sort(key=lambda t: -t[1])
    return scored[:limit]
