"""
Shared pytest fixtures for Transfera integration tests.
"""
from __future__ import annotations

import asyncio
import time
from threading import Thread
from unittest.mock import patch

import httpx
import pytest
import uvicorn

from backend.config import HOST, LOCAL_SECRET_TOKEN, PORT
from backend.main import create_app


def _run_server() -> None:
    """Run the FastAPI server in a background thread."""
    config = uvicorn.Config(
        create_app(),
        host=HOST,
        port=PORT,
        ws="wsproto",
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    """Start the backend server and return an HTTPX client with auth headers."""
    server_thread = Thread(target=_run_server, daemon=True)
    server_thread.start()

    for _ in range(30):
        try:
            r = httpx.get(f"http://{HOST}:{PORT}/api/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        raise RuntimeError("Server failed to start within 15 seconds")

    with httpx.Client(
        base_url=f"http://{HOST}:{PORT}",
        timeout=10.0,
        headers={"X-Local-Token": LOCAL_SECRET_TOKEN},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# In-memory database fixture
# ---------------------------------------------------------------------------
@pytest.fixture
async def db_session(tmp_path):
    """Create an in-memory SQLite database (via a temp file for multi-connection support) and yield an async session.

    Overrides ``DATABASE_URL`` so the engine points at a temporary database file instead
    of the on-disk database.  Tables are created before the test and the
    engine is disposed afterward.
    """
    db_file = tmp_path / "test_temp.db"
    mem_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    with (
        patch("backend.config.DATABASE_URL", mem_url),
        patch("backend.database.manager.DATABASE_URL", mem_url),
    ):
        from backend.database.manager import (
            create_all_tables,
            dispose_engine,
            get_engine,
            session_scope,
        )

        engine = await get_engine()
        await create_all_tables()

        async with session_scope() as session:
            yield session

        await dispose_engine()

        try:
            if db_file.exists():
                db_file.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# TestClient fixture (lightweight, no real server thread)
# ---------------------------------------------------------------------------
@pytest.fixture
def test_client():
    """Return a FastAPI ``TestClient`` backed by an in-memory database.

    The app's lifespan still runs (table creation, recovery, etc.) but the
    ``DATABASE_URL`` is patched to ``sqlite+aiosqlite://`` so nothing
    touches the real on-disk database.

    The ``require_local_token`` dependency is overridden to accept any
    request during tests — no ``X-Local-Token`` header needed.
    """
    with patch("backend.config.DATABASE_URL", "sqlite+aiosqlite://"):
        from starlette.testclient import TestClient

        from backend.api.auth import require_local_token

        app = create_app()

        async def _skip_auth() -> None:
            return None

        app.dependency_overrides[require_local_token] = _skip_auth

        with TestClient(app) as client:
            yield client
