"""
Transfera v2 — In-memory thumbnail cache.
Bounded LRU, no disk writes. Thumbnails exist only for the lifetime
of the backend process. Cleared per-session on completion.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

_MAX_ENTRIES = 500          # hard cap on total thumbnails in memory
_MAX_BYTES   = 50 * 1024 * 1024  # 50 MB total ceiling

class _ThumbnailCache:
    """
    Thread-safe LRU cache mapping item_id -> JPEG bytes.
    Evicts oldest entries when either limit is exceeded.
    """
    def __init__(self, max_entries: int = _MAX_ENTRIES, max_bytes: int = _MAX_BYTES):
        self._lock = threading.Lock()
        self._store: OrderedDict[int, bytes] = OrderedDict()
        self._sizes: dict[int, int] = {}
        self._total_bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes

    def put(self, item_id: int, data: bytes) -> None:
        with self._lock:
            if item_id in self._store:
                self._total_bytes -= self._sizes[item_id]
                del self._store[item_id]
            self._store[item_id] = data
            self._sizes[item_id] = len(data)
            self._total_bytes += len(data)
            self._evict()

    def get(self, item_id: int) -> bytes | None:
        with self._lock:
            if item_id not in self._store:
                return None
            self._store.move_to_end(item_id)
            return self._store[item_id]

    def has(self, item_id: int) -> bool:
        with self._lock:
            return item_id in self._store

    def evict_items(self, item_ids: list[int]) -> None:
        with self._lock:
            for iid in item_ids:
                if iid in self._store:
                    self._total_bytes -= self._sizes.pop(iid)
                    del self._store[iid]

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._sizes.clear()
            self._total_bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._store),
                "total_bytes": self._total_bytes,
                "max_entries": self._max_entries,
                "max_bytes": self._max_bytes,
            }

    def _evict(self) -> None:
        while (len(self._store) > self._max_entries
               or self._total_bytes > self._max_bytes):
            oldest_id, _ = self._store.popitem(last=False)
            self._total_bytes -= self._sizes.pop(oldest_id)


# Module-level singleton
thumbnail_cache = _ThumbnailCache()
