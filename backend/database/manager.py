"""
MediaVault v2 — Database Manager
Async SQLAlchemy 2.0 engine for SQLite with WAL mode and FK enforcement.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.config import DATABASE_URL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine singleton
# ---------------------------------------------------------------------------
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_init_lock = asyncio.Lock()


def _build_sync_url(async_url: str) -> str:
    """Convert aiosqlite:// URL to plain sqlite:// for event listeners."""
    return async_url.replace("sqlite+aiosqlite://", "sqlite://")


async def _set_pragmas(connection) -> None:  # type: ignore[no-untyped-def]
    """Emit PRAGMA commands on every new raw connection."""
    await connection.execute(text("PRAGMA journal_mode=WAL"))
    await connection.execute(text("PRAGMA foreign_keys=ON"))
    await connection.execute(text("PRAGMA busy_timeout=5000"))
    await connection.execute(text("PRAGMA synchronous=NORMAL"))


async def get_engine() -> AsyncEngine:
    """Return (and lazily initialise) the global async engine."""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    async with _init_lock:
        if _engine is not None:
            return _engine

        _engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            future=True,
            connect_args={"timeout": 15},
        )

        # Register a sync-level event so every new connection gets pragmas.
        @event.listens_for(_engine.sync_engine, "connect")
        def _on_connect(dbapi_conn, connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        _session_factory = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        logger.info("Async engine created -> %s", DATABASE_URL)
        return _engine


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Explicit context-manager variant of get_session."""
    factory = _session_factory
    if factory is None:
        await get_engine()
        factory = _session_factory

    async with factory() as session:  # type: ignore[union-attr]
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Alias for cleaner API
get_session = session_scope


async def create_all_tables() -> None:
    """Create all tables defined in the models module."""
    from backend.database import models  # noqa: F811

    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    logger.info("All tables created.")


async def dispose_engine() -> None:
    """Gracefully shut down the engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Engine disposed.")
