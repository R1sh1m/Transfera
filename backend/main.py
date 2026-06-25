"""
Transfera v2 — FastAPI Engine
Application entry point with lifespan management.
Serves compiled React frontend as SPA from frontend/dist/.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.device_preview import router as device_preview_router
from backend.api.routes import _run_transfer_background, router, ws_transfer
from backend.api.tier2_routes import router as tier2_router
from backend.config import CACHE_DIR, HOST, PORT
from backend.database.manager import create_all_tables, dispose_engine, session_scope
from backend.engines.recovery import recover_interrupted_batches

# ---------------------------------------------------------------------------
# Logging setup — ensure stdout uses UTF-8 so Unicode log characters
# (→, —, and any other standard Unicode) render correctly on Windows
# consoles that support it (Windows 10 1903+).  Purely cosmetic.
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("transfera")

# ---------------------------------------------------------------------------
# Resolve the compiled React asset directory (frontend/dist/)
# ---------------------------------------------------------------------------
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


# ---------------------------------------------------------------------------
# SPA StaticFiles — serves index.html for any path not matching a real asset
# ---------------------------------------------------------------------------
class SPAStaticFiles(StaticFiles):
    """StaticFiles subclass that falls back to index.html for client-side routes.
    Passes through non-HTTP scopes (WebSocket, lifespan) and API/WS paths
    so they reach explicitly registered endpoints instead of being swallowed."""

    async def __call__(self, scope, receive, send):
        # Non-HTTP scopes (WebSocket handshake, lifespan) must pass through
        # to the handlers registered BEFORE this mount — just return.
        if scope["type"] != "http":
            return

        path = scope.get("path", "")

        # API and WS paths should have been matched by registered routes above
        # this mount. If they reach here, no route matched — send a 404 so the
        # connection is properly closed rather than left hanging.
        if path.startswith("/api/") or path.startswith("/ws/"):
            from starlette.responses import JSONResponse
            response = JSONResponse(
                {"detail": f"Not found: {path}"},
                status_code=404,
            )
            await response(scope, receive, send)
            return

        # For all other paths, try to serve a static file.
        # Falls back to index.html via get_response override below.
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except Exception:
            return FileResponse(str(FRONTEND_DIST / "index.html"))


# ---------------------------------------------------------------------------
# Background initializer for device manager — must not block server startup
# ---------------------------------------------------------------------------
async def _init_device_manager_background(manager) -> None:
    """Runs after the app is already serving HTTP traffic. Probes
    device tiers and may auto-start the WSL bridge, which can take
    up to ~60s on a cold start — this MUST NOT block Uvicorn's
    startup, so it is scheduled as a background task instead of
    being awaited inside lifespan()."""
    try:
        await manager.initialize()
        active_tier = await manager.get_active_tier()
        logger.info("Device manager initialized — active tier: %s", active_tier.value)
    except Exception as exc:
        logger.warning("Device manager init failed (Tier 2 unavailable): %s", exc)

    try:
        orchestrator = manager.get_orchestrator()
        if orchestrator is not None:
            await orchestrator.cleanup_orphaned_bridge()
    except Exception as exc:
        logger.debug("Bridge orphan cleanup skipped: %s", exc)


# ---------------------------------------------------------------------------
# Lifespan (startup/shutdown)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB + recovery. Shutdown: dispose engine."""
    logger.info("Transfera v2 starting on %s:%d", HOST, PORT)

    # Expand the default asyncio executor thread pool so CPU-bound thumbnail
    # generation and AFC I/O don't queue behind each other.
    # Default is min(32, cpu_count + 4) — we double that, capped at 32.
    import concurrent.futures
    _cpu = os.cpu_count() or 4
    _pool_size = min(32, max(16, _cpu * 2))
    loop = asyncio.get_event_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=_pool_size,
            thread_name_prefix="uvicorn-exec",
        )
    )
    logger.info("Asyncio executor expanded to %d threads (%d CPUs)", _pool_size, _cpu)

    if not FRONTEND_DIST.is_dir():
        logger.warning(
            "Frontend dist not found at %s — API-only mode", FRONTEND_DIST
        )
    else:
        logger.info("Serving frontend from %s", FRONTEND_DIST)

    # Ensure tables exist
    await create_all_tables()
    logger.info("Database tables ready")

    # Reset stale in-memory thumbnail sentinels — after a restart the LRU
    # cache is empty, so "memory" entries from a prior process no longer
    # have actual bytes available.  Setting them to NULL means the frontend
    # will get a clean 404 instead of endlessly retrying.
    from typing import cast

    from sqlalchemy import CursorResult, text
    async with session_scope() as session:
        result = await session.execute(
            text("UPDATE media_items SET thumbnail_path = NULL WHERE thumbnail_path = 'memory'")
        )
        cursor_result = cast(CursorResult, result)
        if cursor_result.rowcount > 0:
            logger.info("Reset %d stale 'memory' thumbnail entries after restart", cursor_result.rowcount)
    logger.info("Stale thumbnail sentinels cleared")

    # Run crash recovery
    stats = await recover_interrupted_batches(cache_dir=CACHE_DIR)
    logger.info(
        "Recovery complete: %d LOADING, %d ARCHIVED batches handled "
        "(%d orphaned partials removed)",
        stats.get("loading_recovered", 0),
        stats.get("archived_recovered", 0),
        stats.get("orphaned_partials_removed", 0),
    )

    # Auto-resume sessions that had interrupted batches
    resumable = stats.get("resumable_session_ids", [])
    if resumable:
        logger.info("Auto-resuming %d interrupted session(s): %s", len(resumable), resumable)
        for sid in resumable:
            asyncio.create_task(_run_transfer_background(sid))

    # Check if Tier 2 setup needs to resume after restart
    try:
        from backend.wsl_orchestrator import Tier2PersistedState
        tier2_state = Tier2PersistedState.load()
        if tier2_state and tier2_state.pending_step:
            logger.info(
                "Tier 2 setup was interrupted (step: %s) — will resume automatically",
                tier2_state.pending_step,
            )
    except Exception:
        pass

    # Pre-warm ExifTool so it's ready for the first transfer
    try:
        from backend.engines.metadata_extractor import _bootstrap_exiftool, _exiftool_session
        exiftool_path = _bootstrap_exiftool()
        if exiftool_path:
            _exiftool_session._ensure_running()
            logger.info("ExifTool stay_open session pre-warmed at startup")
        else:
            logger.warning("ExifTool not available — metadata extraction limited to filesystem timestamps")
    except Exception as exc:
        logger.warning("Failed to pre-warm ExifTool: %s", exc)

    # Fire up device manager in background — don't block server startup
    from backend.tier2_manager import get_device_manager
    manager = get_device_manager()
    app.state.device_manager_init_task = asyncio.create_task(
        _init_device_manager_background(manager)
    )
    yield

    # Shutdown
    task = getattr(app.state, "device_manager_init_task", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Close the persistent ExifTool session (if it was ever started)
    try:
        from backend.engines.metadata_extractor import _exiftool_session
        _exiftool_session.close()
    except Exception:
        pass

    await dispose_engine()
    logger.info("Transfera v2 shutdown complete")



# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title="Transfera v2",
        description="Local media backup engine",
        version="2.4.0",
        lifespan=lifespan,
    )

    # CORS for Electron renderer and same-origin dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",   # Vite dev server
            "http://localhost:5173",
            f"http://127.0.0.1:{PORT}",
            f"http://localhost:{PORT}",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API routes FIRST -- these take priority over the SPA catch-all
    app.include_router(router)
    app.include_router(tier2_router)
    app.include_router(device_preview_router)

    # WebSocket endpoint
    @app.websocket("/ws/transfer/{session_id}")
    async def websocket_endpoint(websocket: WebSocket, session_id: int):
        await ws_transfer(websocket, session_id)

    # Mount compiled React frontend (SPA catch-all)
    if FRONTEND_DIST.is_dir():
        app.mount(
            "/",
            SPAStaticFiles(directory=str(FRONTEND_DIST), html=True),
            name="spa",
        )
    else:
        # Fallback: return JSON status when frontend is not built
        @app.get("/")
        async def root():
            return {
                "name": "Transfera Backend API",
                "status": "active",
                "version": "2.4.0",
                "note": "Frontend not built — run 'npm run build' in frontend/",
            }

    return app


app = create_app()


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=HOST,
        port=PORT,
        ws="wsproto",
        reload=False,
        log_level="info",
    )
