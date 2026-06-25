"""
Transfera v2 — In-memory token-bucket rate limiter for polling endpoints.

Each bucket is keyed by a string (typically ``"session:{session_id}"``) and
tokens refill at *rate* per second with a *capacity* burst ceiling.
Buckets are stored in a plain dict that resets on restart — fine for a
desktop app with no Redis dependency.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable

from fastapi import HTTPException, Request


class TokenBucket:
    """In-memory token bucket with burst support."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = _time.time()

    def consume(self) -> bool:
        """Try to consume one token. Returns True if allowed."""
        now = _time.time()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


_buckets: dict[str, TokenBucket] = {}


def per_session_rate_limit(
    rate: float = 5.0,
    capacity: float = 10.0,
) -> Callable[[int, Request], None]:
    """FastAPI dependency factory. Keyed by ``session_id`` path parameter.

    Usage as a route decorator::

        @router.get("/sessions/{session_id}/progress")
        async def get_progress(
            session_id: int,
            _: None = Depends(per_session_rate_limit()),
        ):
            ...

    Parameters
    ----------
    rate
        Tokens added per second (refill rate).
    capacity
        Maximum burst size (token bucket capacity).
    """
    def _dependency(session_id: int, request: Request) -> None:
        key = f"session:{session_id}"
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _buckets[key] = TokenBucket(rate, capacity)
        if not bucket.consume():
            raise HTTPException(status_code=429, detail="Too many requests")
    return _dependency
