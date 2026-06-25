"""
Transfera v2 — Token-bucket rate-limiter tests.
"""

from __future__ import annotations

import time as _time

from backend.api.rate_limit import TokenBucket


def test_bucket_allows_burst() -> None:
    """Capacity tokens are available immediately on first call."""
    bucket = TokenBucket(rate=5.0, capacity=10.0)
    for _ in range(10):
        assert bucket.consume(), "Should allow all burst tokens"


def test_bucket_blocks_above_capacity() -> None:
    """Requests beyond capacity are denied."""
    bucket = TokenBucket(rate=5.0, capacity=5.0)
    for _ in range(5):
        assert bucket.consume()
    # 6th call should fail (no refill time has passed)
    assert not bucket.consume(), "Should block above capacity"


def test_bucket_refills_over_time() -> None:
    """After waiting, tokens are replenished."""
    bucket = TokenBucket(rate=10.0, capacity=10.0)
    # Drain
    for _ in range(10):
        bucket.consume()
    assert not bucket.consume()
    # Wait for 1 token (100ms at 10/s)
    _time.sleep(0.15)
    assert bucket.consume(), "Should allow after refill"


def test_bucket_caps_at_capacity() -> None:
    """Refilling doesn't exceed configured capacity."""
    bucket = TokenBucket(rate=100.0, capacity=5.0)
    _time.sleep(0.1)  # Would add 10 tokens, but cap is 5
    for _ in range(5):
        assert bucket.consume()
    assert not bucket.consume(), "Should cap at capacity"
