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
    """Create all tables defined in the models module, then apply migrations."""
    from backend.database import models  # noqa: F811

    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)

        # SQLite migrations: add columns if they don't exist yet.
        migrations = [
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='total_files'",
                "ALTER TABLE transfer_sessions ADD COLUMN total_files INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='cached_files'",
                "ALTER TABLE transfer_sessions ADD COLUMN cached_files INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='imported_files'",
                "ALTER TABLE transfer_sessions ADD COLUMN imported_files INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='failed_files'",
                "ALTER TABLE transfer_sessions ADD COLUMN failed_files INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='current_batch'",
                "ALTER TABLE transfer_sessions ADD COLUMN current_batch INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='total_batches'",
                "ALTER TABLE transfer_sessions ADD COLUMN total_batches INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('media_items') WHERE name='original_capture_time'",
                "ALTER TABLE media_items ADD COLUMN original_capture_time DATETIME DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('media_items') WHERE name='thumbnail_status'",
                "ALTER TABLE media_items ADD COLUMN thumbnail_status VARCHAR(16) NOT NULL DEFAULT 'pending'",
            ),
            (
                "SELECT name FROM pragma_table_info('media_items') WHERE name='thumbnail_path'",
                "ALTER TABLE media_items ADD COLUMN thumbnail_path VARCHAR(4096) DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('media_items') WHERE name='date_taken'",
                "ALTER TABLE media_items ADD COLUMN date_taken DATETIME DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('media_items') WHERE name='date_source'",
                "ALTER TABLE media_items ADD COLUMN date_source VARCHAR(32) DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='only_new_mode'",
                "ALTER TABLE transfer_sessions ADD COLUMN only_new_mode BOOLEAN NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='resolved_batch_id'",
                "ALTER TABLE transfer_sessions ADD COLUMN resolved_batch_id INTEGER DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='duplicate_resolutions_json'",
                "ALTER TABLE transfer_sessions ADD COLUMN duplicate_resolutions_json TEXT DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='paused_at'",
                "ALTER TABLE transfer_sessions ADD COLUMN paused_at DATETIME DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='total_paused_ms'",
                "ALTER TABLE transfer_sessions ADD COLUMN total_paused_ms INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='speed_samples'",
                "ALTER TABLE transfer_sessions ADD COLUMN speed_samples TEXT DEFAULT NULL",
            ),
            (
                "SELECT name FROM pragma_table_info('transfer_sessions') WHERE name='folder_layout'",
                "ALTER TABLE transfer_sessions ADD COLUMN folder_layout VARCHAR(32) NOT NULL DEFAULT 'year/month'",
            ),
            (
                "SELECT name FROM pragma_index_list('media_items') WHERE name='ix_media_items_filename_size'",
                "CREATE INDEX IF NOT EXISTS ix_media_items_filename_size ON media_items (file_name, file_size)",
            ),
        ]
        for check_sql, alter_sql in migrations:
            result = await conn.execute(text(check_sql))
            if result.fetchone() is None:
                await conn.execute(text(alter_sql))
                logger.info("Migration applied: %s", alter_sql[:60])

    logger.info("All tables created.")


# ---------------------------------------------------------------------------
# Atomic session counter increments
# ---------------------------------------------------------------------------

async def increment_session_counter(
    session_id: int,
    column: str,
    amount: int = 1,
) -> None:
    """Atomically increment a TransferSession counter column."""
    from backend.database.models import TransferSession
    async with session_scope() as db_session:
        ts = await db_session.get(TransferSession, session_id)
        if ts is not None:
            current = getattr(ts, column, 0)
            setattr(ts, column, current + amount)
            ts.touch()


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
