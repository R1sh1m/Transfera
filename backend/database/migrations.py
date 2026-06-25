"""
Transfera v2 — Numbered schema migrations for SQLite.

Each migration is a ``(id, sql)`` tuple with a monotonically increasing integer
ID that is permanent and never re-ordered.  On startup ``run_pending_migrations``
applies any unapplied migrations in order using the ``schema_migrations`` ledger
table for idempotency.

For fresh installs the ORM's ``create_all`` already creates all columns, so the
ALTER TABLE statements will fail with ``duplicate column`` — that's expected and
caught.  The migration is still recorded as applied so subsequent startup is a
no-op.

For existing installs from before this system existed, columns that are missing
will be added, and already-present columns will be skipped via the same error
handling.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Migration ledger
# ---------------------------------------------------------------------------
# Each entry: (id, sql_statement)
# IDs are permanent — never reorder, never delete, never reuse.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, "ALTER TABLE transfer_sessions ADD COLUMN total_files INTEGER NOT NULL DEFAULT 0"),
    (2, "ALTER TABLE transfer_sessions ADD COLUMN cached_files INTEGER NOT NULL DEFAULT 0"),
    (3, "ALTER TABLE transfer_sessions ADD COLUMN imported_files INTEGER NOT NULL DEFAULT 0"),
    (4, "ALTER TABLE transfer_sessions ADD COLUMN failed_files INTEGER NOT NULL DEFAULT 0"),
    (5, "ALTER TABLE transfer_sessions ADD COLUMN current_batch INTEGER NOT NULL DEFAULT 0"),
    (6, "ALTER TABLE transfer_sessions ADD COLUMN total_batches INTEGER NOT NULL DEFAULT 0"),
    (7, "ALTER TABLE media_items ADD COLUMN original_capture_time DATETIME DEFAULT NULL"),
    (8, "ALTER TABLE media_items ADD COLUMN thumbnail_status VARCHAR(16) NOT NULL DEFAULT 'pending'"),
    (9, "ALTER TABLE media_items ADD COLUMN thumbnail_path VARCHAR(4096) DEFAULT NULL"),
    (10, "ALTER TABLE media_items ADD COLUMN date_taken DATETIME DEFAULT NULL"),
    (11, "ALTER TABLE media_items ADD COLUMN date_source VARCHAR(32) DEFAULT NULL"),
    (12, "ALTER TABLE transfer_sessions ADD COLUMN only_new_mode BOOLEAN NOT NULL DEFAULT 0"),
    (13, "ALTER TABLE transfer_sessions ADD COLUMN resolved_batch_id INTEGER DEFAULT NULL"),
    (14, "ALTER TABLE transfer_sessions ADD COLUMN duplicate_resolutions_json TEXT DEFAULT NULL"),
    (15, "ALTER TABLE transfer_sessions ADD COLUMN paused_at DATETIME DEFAULT NULL"),
    (16, "ALTER TABLE transfer_sessions ADD COLUMN total_paused_ms INTEGER NOT NULL DEFAULT 0"),
    (17, "ALTER TABLE transfer_sessions ADD COLUMN speed_samples TEXT DEFAULT NULL"),
    (18, "ALTER TABLE transfer_sessions ADD COLUMN folder_layout VARCHAR(32) NOT NULL DEFAULT 'year/month'"),
    (19, "CREATE INDEX IF NOT EXISTS ix_media_items_filename_size ON media_items (file_name, file_size)"),
    (20, "CREATE INDEX IF NOT EXISTS ix_media_items_source_path_session ON media_items (source_path, session_id)"),
    (21, "ALTER TABLE transfer_sessions ADD COLUMN selected_files_json TEXT DEFAULT NULL"),
]


async def _ensure_migrations_table(conn: AsyncConnection) -> None:
    """Create the schema_migrations ledger table if it does not exist."""
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  id INTEGER PRIMARY KEY,"
            "  applied_at DATETIME NOT NULL"
            ")"
        )
    )


async def run_pending_migrations(conn: AsyncConnection) -> None:
    """Apply any unapplied migrations in order.

    Must be called inside an ``engine.begin()`` transaction so each migration
    is committed atomically with its ledger entry.
    """
    await _ensure_migrations_table(conn)

    result = await conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM schema_migrations"))
    max_applied = result.scalar() or 0

    for mid, sql in _MIGRATIONS:
        if mid <= max_applied:
            continue
        try:
            await conn.execute(text(sql))
            logger.info("Migration %d applied: %s", mid, sql[:80])
        except Exception as exc:
            err_str = str(exc).lower()
            if "duplicate column" in err_str or "already exists" in err_str:
                logger.debug("Migration %d skipped (column/index already exists): %s", mid, exc)
            else:
                logger.warning("Migration %d failed with unexpected error: %s", mid, exc)
                raise
        now = datetime.now(UTC).isoformat()
        await conn.execute(
            text("INSERT INTO schema_migrations (id, applied_at) VALUES (:id, :applied_at)"),
            {"id": mid, "applied_at": now},
        )

    logger.info("Schema migrations up to date (latest_id=%d)", max(max_applied, len(_MIGRATIONS)))
