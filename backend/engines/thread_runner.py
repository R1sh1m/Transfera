"""
Transfera v2 — Shared singleton event-loop thread for sync→async DB bridge calls.

Provides ``submit`` (fire-and-forget) and ``submit_and_wait`` (blocking call)
so that code running on a non-async thread (e.g. a ``threading.Thread``) can
schedule coroutines on a single persistent event loop without creating a new
loop per call.

This module replaces the three ad-hoc patterns:
1. ``cache_manager.py``'s per-module ``_thumb_worker_loop`` singleton.
2. ``routes.py``'s ``asyncio.new_event_loop()`` in ``_generate_all`` (thumb regen).
3. ``routes.py``'s ``asyncio.new_event_loop()`` in ``_backfill_sync``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import TypeVar

_T = TypeVar("_T")

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start the singleton event-loop thread on first use."""
    global _loop, _loop_thread
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_loop.run_forever,
                daemon=True,
                name="thread-runner",
            )
            _loop_thread.start()
        return _loop


def submit(coro: Coroutine[object, object, _T]) -> None:
    """Schedule *coro* on the singleton loop (fire-and-forget)."""
    loop = _ensure_loop()
    asyncio.run_coroutine_threadsafe(coro, loop)


def submit_and_wait(
    coro: Coroutine[object, object, _T],
    timeout: float = 10.0,
) -> _T:
    """Schedule *coro* on the singleton loop and block the calling thread
    until it completes (or *timeout* expires).

    Raises
    ------
    TimeoutError
        If the coroutine does not finish within *timeout* seconds.
    """
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def shutdown(timeout: float = 2.0) -> None:
    """Best-effort drain of the singleton loop thread (audit item 5).

    The loop thread is a daemon (never blocks interpreter exit); this
    helper merely gives in-flight DB bridge calls a bounded *timeout* to
    land during teardown instead of being cut mid-write. Never raises.
    """
    global _loop, _loop_thread
    with _lock:
        loop, thread = _loop, _loop_thread
    if loop is None or thread is None:
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(_drain_noop(), loop)
        fut.result(timeout=timeout)
    except Exception:
        pass


async def _drain_noop() -> None:
    return None
