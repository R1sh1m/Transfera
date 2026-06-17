"""
MediaVault v2 — FastAPI Engine
Application entry point with lifespan management.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import BATCH_SIZE, CACHE_DIR, HOST, PORT
from backend.database.manager import create_all_tables, dispose_engine
from backend.engines.recovery import recover_interrupted_batches
from backend.api.routes import router, ws_transfer

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("mediavault")


# ---------------------------------------------------------------------------
# Lifespan (startup/shutdown)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB + recovery. Shutdown: dispose engine."""
    logger.info("MediaVault v2 starting on %s:%d", HOST, PORT)

    # Ensure tables exist
    await create_all_tables()
    logger.info("Database tables ready")

    # Run crash recovery
    stats = await recover_interrupted_batches(cache_dir=CACHE_DIR)
    logger.info(
        "Recovery complete: %d LOADING, %d ARCHIVED batches handled",
        stats.get("loading_recovered", 0),
        stats.get("archived_recovered", 0),
    )

    yield

    # Shutdown
    await dispose_engine()
    logger.info("MediaVault v2 shutdown complete")


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title="MediaVault v2",
        description="Local media backup engine",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS for Electron renderer
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Electron uses file:// or custom protocol
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    app.include_router(router)

    # WebSocket endpoint
    @app.websocket("/ws/transfer/{session_id}")
    async def websocket_endpoint(websocket, session_id: int):
        await ws_transfer(websocket, session_id)

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
        reload=False,
        log_level="info",
    )
