"""
Transfera v2 — Database Manager
Async SQLAlchemy 2.0 engine for SQLite with WAL mode and FK enforcement.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

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
    """Emit PRAGMA commands on every new raw connection.

    synchronous=FULL: WAL frames survive OS/power crash. Costs some write
    throughput — acceptable for a vaulting engine where durability is law.
    """
    await connection.execute(text("PRAGMA journal_mode=WAL"))
    await connection.execute(text("PRAGMA foreign_keys=ON"))
    await connection.execute(text("PRAGMA busy_timeout=10000"))
    await connection.execute(text("PRAGMA synchronous=FULL"))


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
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA synchronous=FULL")
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
    """Explicit context-manager variant of get_session.

    Retries ``database is locked`` (SQLITE_BUSY) with exponential backoff —
    parallel Hop-2 workers + thumbnail thread otherwise flake under WAL.
    """
    import asyncio as _asyncio

    factory = _session_factory
    if factory is None:
        await get_engine()
        factory = _session_factory

    # Guard: raise a clear error instead of a cryptic AttributeError if the
    # engine failed to initialise (e.g. DB file locked at startup).
    if factory is None:
        raise RuntimeError(
            "Database engine is unavailable — cannot open a session. "
            "Check that the DB file is accessible and not locked by another process."
        )

    async with factory() as session:
        try:
            yield session
            # Retry commit if SQLite was temporarily locked during WAL commit
            for attempt in range(4):
                try:
                    await session.commit()
                    break
                except Exception as exc:
                    msg = str(exc).lower()
                    if attempt < 3 and ("database is locked" in msg or "database table is locked" in msg):
                        await _asyncio.sleep(0.05 * (2**attempt))
                        continue
                    raise
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise


# Alias for cleaner API
get_session = session_scope


async def create_all_tables() -> None:
    """Create all tables defined in the models module, then apply migrations."""
    from backend.database import models  # noqa: F811

    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)

        # Run numbered migrations (if any) via the separate migrations module.
        from backend.database.migrations import run_pending_migrations

        await run_pending_migrations(conn)

    logger.info("All tables created.")


# ---------------------------------------------------------------------------
# Atomic session counter increments
# ---------------------------------------------------------------------------


async def increment_session_counter(
    session_id: int,
    column: str,
    amount: int = 1,
) -> None:
    """Atomically increment a TransferSession counter column.

    Uses a SQL-level ``UPDATE col = col + :amount`` to avoid the
    read-modify-write race condition that would occur when multiple
    concurrent coroutines (e.g. parallel Hop 2 import workers) increment
    the same column simultaneously.
    """
    from backend.database.models import _utcnow

    async with session_scope() as db_session:
        await db_session.execute(
            text(
                f"UPDATE transfer_sessions "
                f"SET {column} = COALESCE({column}, 0) + :amount, "
                f"    updated_at = :now "
                f"WHERE id = :sid"
            ),
            {"amount": amount, "now": _utcnow().isoformat(), "sid": session_id},
        )


async def set_session_field(
    session_id: int,
    column: str,
    value: object,
) -> None:
    """Set a TransferSession field to a specific value."""
    from backend.database.models import TransferSession

    async with session_scope() as db_session:
        ts = await db_session.get(TransferSession, session_id)
        if ts is not None:
            setattr(ts, column, value)
            ts.touch()


async def dispose_engine() -> None:
    """Gracefully shut down the engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Engine disposed.")
