"""
Transfera v2 — WebSocket Event Pipeline
Manages per-session WS connections with 30s keepalive ping/pong.
Broadcasts all 15 system-wide events to connected clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KEEPALIVE_INTERVAL = 30  # seconds
KEEPALIVE_TIMEOUT = 10   # seconds to wait for pong before disconnect


# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------
class ConnectionManager:
    """
    Manages WebSocket connections keyed by session_id.
    Supports multiple simultaneous listeners per session.
    """

    def __init__(self) -> None:
        self._connections: dict[int, list[WebSocket]] = {}
        self._keepalive_tasks: dict[int, asyncio.Task] = {}
        self._pong_events: dict[int, dict[WebSocket, asyncio.Event]] = {}

    async def connect(self, websocket: WebSocket, session_id: int) -> None:
        await websocket.accept()
        self._connections.setdefault(session_id, []).append(websocket)
        self._pong_events.setdefault(session_id, {})[websocket] = asyncio.Event()
        logger.info("WS connected: session=%d (total=%d)", session_id, len(self._connections[session_id]))

        # Start keepalive for the first connection on this session
        if len(self._connections[session_id]) == 1:
            self._keepalive_tasks[session_id] = asyncio.create_task(
                self._keepalive_loop(session_id)
            )

    def disconnect(self, websocket: WebSocket, session_id: int) -> None:
        conns = self._connections.get(session_id, [])
        if websocket in conns:
            conns.remove(websocket)
        logger.info("WS disconnected: session=%d (remaining=%d)", session_id, len(conns))

        # Clean up pong event
        pong_map = self._pong_events.get(session_id, {})
        pong_map.pop(websocket, None)
        if not pong_map:
            self._pong_events.pop(session_id, None)

        # Clean up empty sessions
        if not conns:
            self._connections.pop(session_id, None)
            task = self._keepalive_tasks.pop(session_id, None)
            if task is not None:
                task.cancel()

    def signal_pong(self, websocket: WebSocket, session_id: int) -> None:
        event = self._pong_events.get(session_id, {}).get(websocket)
        if event is not None:
            event.set()

    async def broadcast(self, session_id: int, event: str, data: dict[str, Any]) -> None:
        """Send a typed event to all listeners of a session."""
        payload = json.dumps({
            "event": event,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        dead: list[WebSocket] = []
        for ws in self._connections.get(session_id, []):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        # Prune dead connections
        for ws in dead:
            self.disconnect(ws, session_id)

    async def broadcast_all(self, event: str, data: dict[str, Any]) -> None:
        """Send to every connected session."""
        for sid in list(self._connections.keys()):
            await self.broadcast(sid, event, data)

    # ------------------------------------------------------------------
    # Keepalive
    # ------------------------------------------------------------------
    async def _keepalive_loop(self, session_id: int) -> None:
        """Periodically ping all connections for a session."""
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                conns = list(self._connections.get(session_id, []))
                if not conns:
                    break

                dead: list[WebSocket] = []
                for ws in conns:
                    # Reset the pong event before sending ping
                    event = self._pong_events.get(session_id, {}).get(ws)
                    if event:
                        event.clear()
                    try:
                        await ws.send_json({
                            "event": "ping",
                            "data": {},
                            "timestamp": datetime.now(UTC).isoformat(),
                        })
                    except Exception:
                        dead.append(ws)
                        continue

                    # Wait for pong response within timeout
                    if event:
                        try:
                            await asyncio.wait_for(event.wait(), timeout=KEEPALIVE_TIMEOUT)
                        except TimeoutError:
                            logger.warning(
                                "WS pong timeout: session=%d, disconnecting stale connection",
                                session_id,
                            )
                            dead.append(ws)

                for ws in dead:
                    self.disconnect(ws, session_id)
        except asyncio.CancelledError:
            pass

    @property
    def active_sessions(self) -> list[int]:
        return list(self._connections.keys())

    def connection_count(self, session_id: int) -> int:
        return len(self._connections.get(session_id, []))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Event emitters (convenience wrappers)
# ---------------------------------------------------------------------------
async def emit_scan_progress(session_id: int, processed: int, total: int, current_file: str) -> None:
    await manager.broadcast(session_id, "scan_progress", {
        "processed": processed, "total": total, "current_file": current_file,
    })

async def emit_scan_complete(session_id: int, item_count: int) -> None:
    await manager.broadcast(session_id, "scan_complete", {"item_count": item_count})

async def emit_batch_created(session_id: int, batch_id: int, batch_number: int, item_count: int) -> None:
    await manager.broadcast(session_id, "batch_created", {
        "batch_id": batch_id, "batch_number": batch_number, "item_count": item_count,
    })

async def emit_batch_processing(session_id: int, batch_id: int, batch_number: int, item_count: int = 0) -> None:
    await manager.broadcast(session_id, "batch_processing", {
        "batch_id": batch_id, "batch_number": batch_number, "item_count": item_count,
    })

async def emit_batch_complete(session_id: int, batch_id: int, batch_number: int, status: str) -> None:
    await manager.broadcast(session_id, "batch_complete", {
        "batch_id": batch_id, "batch_number": batch_number, "status": status,
    })

async def emit_hop1_progress(session_id: int, batch_id: int, processed: int, total: int, file_name: str, item_id: int | None = None) -> None:
    await manager.broadcast(session_id, "hop1_progress", {
        "batch_id": batch_id, "processed": processed, "total": total, "file_name": file_name,
        "item_id": item_id,
    })

async def emit_hop1_complete(session_id: int, batch_id: int, cached_count: int) -> None:
    await manager.broadcast(session_id, "hop1_complete", {
        "batch_id": batch_id, "cached_count": cached_count,
    })

async def emit_hop2_progress(session_id: int, batch_id: int, processed: int, total: int, file_name: str, item_id: int | None = None) -> None:
    await manager.broadcast(session_id, "hop2_progress", {
        "batch_id": batch_id, "processed": processed, "total": total, "file_name": file_name,
        "item_id": item_id,
    })

async def emit_hop2_complete(session_id: int, batch_id: int, imported_count: int) -> None:
    await manager.broadcast(session_id, "hop2_complete", {
        "batch_id": batch_id, "imported_count": imported_count,
    })

async def emit_duplicates_detected(session_id: int, report: dict[str, Any]) -> None:
    await manager.broadcast(session_id, "duplicates_detected", report)

async def emit_duplicates_resolved(session_id: int, batch_id: int) -> None:
    await manager.broadcast(session_id, "duplicates_resolved", {"batch_id": batch_id})

async def emit_session_started(session_id: int) -> None:
    await manager.broadcast(session_id, "session_started", {"session_id": session_id})

async def emit_session_paused(session_id: int) -> None:
    await manager.broadcast(session_id, "session_paused", {"session_id": session_id})

async def emit_session_complete(session_id: int, stats: dict[str, Any]) -> None:
    await manager.broadcast(session_id, "session_complete", {"session_id": session_id, **stats})

async def emit_error(session_id: int, message: str, code: str = "error") -> None:
    await manager.broadcast(session_id, "error", {"message": message, "code": code})
