"""
Transfera v2 — Perceptual Hashing (near-duplicate detection).

Pure-Pillow dHash (64-bit) + Hamming distance grouping. Zero new
dependencies — runs fully offline on CPU, suitable for Windows desktop.

Background
----------
Cryptographic hashes (BLAKE3) catch byte-identical duplicates only.
Real libraries accumulate *near*-duplicates: bursts, re-exports,
resized/compressed copies, format conversions (HEIC -> JPEG). Following
the PHASER evaluation framework (DFRWS 2024) and recent ViT-hash work
(De Geest et al. 2024/2025), a 64-bit DCT/difference hash with Hamming
threshold 6-10 gives the best robustness/cost trade-off for desktop use.

Design
------
- ``dhash64``: 9x8 grayscale difference hash -> 16 hex chars.
- ``hamming_distance``: popcount of XOR, 0 (identical) .. 64.
- ``group_near_duplicates``: prefix-blocked O(n*block) grouping.
- ``keeper_score``: deterministic best-shot heuristic (resolution,
  file size, EXIF date presence, favorite flag) for burst culling.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 8
BLOCK_BITS = 16  # high-order bits used as blocking key


def dhash64(file_path: str | Path) -> str | None:
    """Compute a 64-bit difference hash for an image file.

    Returns 16 lowercase hex chars, or None if undecodable (video,
    corrupt, unsupported). Never raises.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        p = Path(file_path)
        if not p.is_file():
            return None
        with Image.open(p) as img:
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("L").resize((9, 8))
            px = list(img.getdata())
            bits = 0
            for row in range(8):
                for col in range(8):
                    left = px[row * 9 + col]
                    right = px[row * 9 + col + 1]
                    bits = (bits << 1) | (1 if left > right else 0)
            return f"{bits:016x}"
    except Exception as exc:
        logger.debug("dhash failed for %s: %s", file_path, exc)
        return None


def dhash_bytes(image_bytes: bytes) -> str | None:
    """Hash in-memory image bytes (thumbnail pipeline reuse)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        import io as _io

        with Image.open(_io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("L").resize((9, 8))
            px = list(img.getdata())
            bits = 0
            for row in range(8):
                for col in range(8):
                    bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
            return f"{bits:016x}"
    except Exception:
        return None


def hamming_distance(h1: str | None, h2: str | None) -> int:
    """Hamming distance between two 16-hex-char hashes (0..64).

    Returns 64 (maximally different) for missing/malformed inputs instead
    of raising, so one corrupt row can never break duplicate grouping.
    """
    try:
        if not isinstance(h1, str) or not isinstance(h2, str):
            return 64
        return bin(int(h1, 16) ^ int(h2, 16)).count("1")
    except (ValueError, TypeError):
        return 64


def _block_key(h: str) -> str:
    """High-order prefix used for blocking to avoid O(n^2)."""
    return h[: BLOCK_BITS // 4]


def group_near_duplicates(
    items: list[dict],
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[list[dict]]:
    """Group items with ``phash`` keys by Hamming distance <= threshold.

    Each item: ``{"id": int, "phash": str|None, ...}``. Items without a
    hash are ignored. Returns groups of size >= 2, largest first.
    Greedy single-pass clustering with prefix blocking; deterministic.
    """
    hashed = [it for it in items if it.get("phash")]
    if len(hashed) < 2:
        return []
    # Blocking: only compare within same high-order prefix bucket plus
    # a global fallback bucket for small libraries (< 2000 items).
    buckets: dict[str, list[dict]] = {}
    for it in hashed:
        buckets.setdefault(_block_key(str(it["phash"])), []).append(it)
    use_global = len(hashed) < 2000
    groups: list[list[dict]] = []
    assigned: set[int] = set()
    pool = hashed if use_global else None
    for it in hashed:
        if it["id"] in assigned:
            continue
        candidates = pool if pool is not None else buckets[_block_key(str(it["phash"]))]
        cluster = [it]
        for other in candidates:
            if other["id"] in assigned or other["id"] == it["id"]:
                continue
            if other in cluster:
                continue
            if hamming_distance(str(it["phash"]), str(other["phash"])) <= threshold:
                cluster.append(other)
        if len(cluster) >= 2:
            for m in cluster:
                assigned.add(m["id"])
            groups.append(sorted(cluster, key=lambda x: x["id"]))
    groups.sort(key=len, reverse=True)
    return groups


def keeper_score(item: dict) -> tuple:
    """Deterministic keeper ranking for a duplicate group (higher wins).

    Priority: favorite > resolution (w*h) > file size > has EXIF date >
    lower id (stable tiebreak). Returns a comparable tuple.
    """
    fav = 1 if item.get("favorite") else 0
    w = item.get("width") or 0
    h = item.get("height") or 0
    res = (w * h) if (w and h) else 0
    size = item.get("file_size") or 0
    has_date = 1 if item.get("date_taken") else 0
    # Negative id so lower ids win ties when max() is used with key.
    return (fav, res, size, has_date, -int(item.get("id", 0)))


def suggest_keeper(group: list[dict]) -> dict | None:
    """Return the suggested keeper for a near-duplicate group."""
    if not group:
        return None
    return max(group, key=keeper_score)
