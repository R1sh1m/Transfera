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

from backend.config import HOST, LOCAL_SECRET_TOKEN
from backend.main import create_app


def _free_port() -> int:
    """Pick an unused localhost port so tests never clash with a dev server."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session", autouse=True)
def _guard_real_database(tmp_path_factory):
    """Process-wide guard: no test may EVER touch the developer's real DB.

    Several legacy helpers (e.g. ``_reset_db`` in test_crash_recovery.py)
    call ``drop_all``/``create_all_tables`` on the global engine without
    patching ``DATABASE_URL`` — against the real
    ``backend/data/db/transfera.db`` that wiped real user data mid-suite.
    This autouse session fixture redirects both ``DATABASE_URL`` bindings
    (``backend.config`` and ``backend.database.manager``, which imports it
    by value) at a throwaway file for the whole pytest process. Narrower
    per-test fixtures (``db_session``, ``client``, ``test_client``)
    re-patch on top without conflict.
    """
    import backend.database.manager as _manager

    guard_file = tmp_path_factory.mktemp("guard_db") / "guard.db"
    guard_url = f"sqlite+aiosqlite:///{guard_file.as_posix()}"
    try:
        asyncio.run(_manager.dispose_engine())
    except Exception:
        pass
    with (
        patch("backend.config.DATABASE_URL", guard_url),
        patch("backend.database.manager.DATABASE_URL", guard_url),
    ):
        yield
    try:
        asyncio.run(_manager.dispose_engine())
    except Exception:
        pass


def _run_server(port: int) -> None:
    """Run the FastAPI server in a background thread."""
    config = uvicorn.Config(
        create_app(),
        host=HOST,
        port=port,
        ws="wsproto",
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> httpx.Client:
    """Start the backend server and return an HTTPX client with auth headers.

    Isolation (hard-won lesson: this fixture used to boot the REAL app on
    the REAL port with the REAL database, so destructive tests wiped the
    developer's dev data and clashed with a running dev server):
      * free localhost port instead of the production PORT;
      * temp-file DATABASE_URL so the server thread builds its engine
        against a throwaway database. Any pre-existing global engine is
        disposed first and the test engine is disposed at teardown.
    """
    import backend.database.manager as _manager

    db_file = tmp_path_factory.mktemp("client_db") / "client_test.db"
    test_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    port = _free_port()

    # Drop any engine bound to the real (or a previous test) database so
    # the server thread lazily creates a fresh one under the patched URL.
    try:
        asyncio.run(_manager.dispose_engine())
    except Exception:
        pass

    with (
        patch("backend.config.DATABASE_URL", test_url),
        patch("backend.database.manager.DATABASE_URL", test_url),
    ):
        server_thread = Thread(target=_run_server, args=(port,), daemon=True)
        server_thread.start()

        for _ in range(60):
            try:
                r = httpx.get(f"http://{HOST}:{port}/api/health", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Server failed to start within 30 seconds")

        with httpx.Client(
            base_url=f"http://{HOST}:{port}",
            timeout=10.0,
            headers={"X-Local-Token": LOCAL_SECRET_TOKEN},
        ) as c:
            yield c

    try:
        asyncio.run(_manager.dispose_engine())
    except Exception:
        pass


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
def test_client(tmp_path):
    """Return a FastAPI ``TestClient`` backed by a throwaway database.

    The app's lifespan still runs (table creation, recovery, etc.) but both
    ``DATABASE_URL`` bindings (``backend.config`` AND
    ``backend.database.manager``, which imports it by value) point at a
    temp file — patching only ``backend.config`` is a no-op for the engine
    and previously let destructive tests (media/clear, trash/empty, which
    now also delete vault files) run against the developer's REAL database.
    Any pre-existing global engine is disposed first so the lifespan builds
    a fresh one under the patched URL.

    The ``require_local_token`` dependency is overridden to accept any
    request during tests — no ``X-Local-Token`` header needed.
    """
    import backend.database.manager as _manager

    db_file = tmp_path / "testclient.db"
    test_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    try:
        asyncio.run(_manager.dispose_engine())
    except Exception:
        pass

    with (
        patch("backend.config.DATABASE_URL", test_url),
        patch("backend.database.manager.DATABASE_URL", test_url),
    ):
        from starlette.testclient import TestClient

        from backend.api.auth import require_local_token

        app = create_app()

        async def _skip_auth() -> None:
            return None

        app.dependency_overrides[require_local_token] = _skip_auth

        with TestClient(app) as client:
            yield client

    try:
        asyncio.run(_manager.dispose_engine())
    except Exception:
        pass
