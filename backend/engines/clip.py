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
from pathlib import Path
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

_READ_TIMEOUT = 600
_DOWNLOAD_CHUNK = 1024 * 256

_sessions_lock = threading.Lock()
_image_session = None  # onnxruntime InferenceSession | None
_text_session = None  # onnxruntime InferenceSession | None
_tokenizer = None  # tokenizers.Tokenizer | None

# Background model-download state (read by the status endpoint).
# RLock (not Lock): start_background_download() calls download_status()
# while already holding it — a plain Lock would deadlock same-thread.
_download_state: dict = {"status": "idle", "downloaded_bytes": 0, "error": None}
_download_lock = threading.RLock()


def download_status() -> dict:
    """Snapshot of model presence + any in-progress background download."""
    with _download_lock:
        state = dict(_download_state)
    missing = missing_models()
    state["ready"] = not missing
    state["missing"] = missing
    if state["status"] == "downloading" and state["ready"]:
        with _download_lock:
            _download_state["status"] = "ready"
        state["status"] = "ready"
    return state


def start_background_download() -> dict:
    """Kick off model download in a daemon thread (idempotent)."""
    import threading as _threading

    with _download_lock:
        if _download_state["status"] == "downloading":
            return download_status()
        if not missing_models():
            _download_state["status"] = "ready"
            return download_status()
        _download_state["status"] = "downloading"
        _download_state["downloaded_bytes"] = 0
        _download_state["error"] = None

    def _run() -> None:
        try:
            ok = ensure_models()
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


def ensure_models() -> bool:
    """Download any missing model files. Returns True when all present."""
    missing = missing_models()
    if not missing:
        return True
    root = _models_root()
    spec_by_name = {name: (path, size) for name, path, size in MODEL_SPECS}
    for name in missing:
        rel, _expected = spec_by_name[name]
        url = f"{HF_BASE}/{rel}"
        dest = root / name
        tmp = dest.with_suffix(dest.suffix + ".part")
        logger.info("Downloading CLIP model %s (%s)", name, url)
        try:
            req = Request(url, headers={"User-Agent": "Transfera/2.0"})
            total = 0
            with urlopen(req, timeout=_READ_TIMEOUT) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
                    with _download_lock:
                        _download_state["downloaded_bytes"] += len(chunk)
            if total < 1_000_000:
                raise OSError(f"suspiciously small download ({total} bytes)")
            tmp.replace(dest)
            logger.info("CLIP model ready: %s (%.1f MB)", name, total / 1e6)
        except Exception as exc:
            logger.warning("CLIP model download failed for %s: %s", name, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
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
        out = sess.run(None, {sess.get_inputs()[0].name: _preprocess_image(image)})[0]
        row = out[0] if isinstance(out[0], list) else list(out[0])
        return _normalize([float(x) for x in row])
    except Exception as exc:
        logger.debug("CLIP image encode failed: %s", exc)
        return None


def encode_text(text: str) -> list[float] | None:
    """Embed a query string -> 512-d normalized vector. None on failure."""
    try:
        _, sess = _get_sessions()
        out = sess.run(None, {sess.get_inputs()[0].name: _tokenize(text)})[0]
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
