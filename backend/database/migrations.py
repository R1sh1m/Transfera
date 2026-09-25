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
    (22, "ALTER TABLE media_items ADD COLUMN phash VARCHAR(16) DEFAULT NULL"),
    (23, "ALTER TABLE media_items ADD COLUMN width INTEGER DEFAULT NULL"),
    (24, "ALTER TABLE media_items ADD COLUMN height INTEGER DEFAULT NULL"),
    (25, "ALTER TABLE media_items ADD COLUMN duration_s FLOAT DEFAULT NULL"),
    (26, "ALTER TABLE media_items ADD COLUMN camera_make VARCHAR(128) DEFAULT NULL"),
    (27, "ALTER TABLE media_items ADD COLUMN camera_model VARCHAR(128) DEFAULT NULL"),
    (28, "ALTER TABLE media_items ADD COLUMN gps_lat FLOAT DEFAULT NULL"),
    (29, "ALTER TABLE media_items ADD COLUMN gps_lon FLOAT DEFAULT NULL"),
    (30, "ALTER TABLE media_items ADD COLUMN favorite BOOLEAN NOT NULL DEFAULT 0"),
    (31, "ALTER TABLE media_items ADD COLUMN trashed BOOLEAN NOT NULL DEFAULT 0"),
    (32, "ALTER TABLE media_items ADD COLUMN trashed_at DATETIME DEFAULT NULL"),
    (33, "ALTER TABLE media_items ADD COLUMN blur_score FLOAT DEFAULT NULL"),
    (34, "ALTER TABLE media_items ADD COLUMN tags_json TEXT DEFAULT NULL"),
    (35, "ALTER TABLE media_items ADD COLUMN caption TEXT DEFAULT NULL"),
    (36, "CREATE INDEX IF NOT EXISTS ix_media_items_phash ON media_items (phash)"),
    (37, "CREATE INDEX IF NOT EXISTS ix_media_items_favorite ON media_items (favorite)"),
    (38, "CREATE INDEX IF NOT EXISTS ix_media_items_trashed ON media_items (trashed)"),
    (39, "CREATE INDEX IF NOT EXISTS ix_media_items_date_taken ON media_items (date_taken)"),
    (
        40,
        "CREATE TABLE IF NOT EXISTS persons ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name VARCHAR(255) DEFAULT NULL, "
        "face_count INTEGER NOT NULL DEFAULT 0, "
        "cover_face_id INTEGER DEFAULT NULL, "
        "hidden BOOLEAN NOT NULL DEFAULT 0, "
        "created_at DATETIME NOT NULL, "
        "updated_at DATETIME NOT NULL)",
    ),
    (
        41,
        "CREATE TABLE IF NOT EXISTS faces ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "person_id INTEGER DEFAULT NULL REFERENCES persons(id) ON DELETE SET NULL, "
        "bbox_json TEXT DEFAULT NULL, "
        "embedding_json TEXT DEFAULT NULL, "
        "confidence FLOAT DEFAULT NULL, "
        "created_at DATETIME NOT NULL)",
    ),
    (
        42,
        "CREATE TABLE IF NOT EXISTS media_embeddings ("
        "media_id INTEGER PRIMARY KEY REFERENCES media_items(id) ON DELETE CASCADE, "
        "model VARCHAR(64) NOT NULL DEFAULT 'keyword-v1', "
        "dim INTEGER NOT NULL DEFAULT 0, "
        "vector_json TEXT DEFAULT NULL, "
        "updated_at DATETIME NOT NULL)",
    ),
    (43, "CREATE INDEX IF NOT EXISTS ix_faces_media_id ON faces (media_id)"),
    (44, "CREATE INDEX IF NOT EXISTS ix_faces_person_id ON faces (person_id)"),
]


async def _ensure_migrations_table(conn: AsyncConnection) -> None:
    """Create the schema_migrations ledger table if it does not exist."""
    await conn.execute(
        text("CREATE TABLE IF NOT EXISTS schema_migrations (  id INTEGER PRIMARY KEY,  applied_at DATETIME NOT NULL)")
    )


def _prune_old_backups(db_dir, keep: int = 5) -> None:
    """Delete all but the newest ``keep`` pre-migration backups."""
    try:
        backups = sorted(
            db_dir.glob("transfera.pre-migrate-*.bak"),
            key=lambda p: p.stat().st_mtime,
        )
        for stale in backups[:-keep] if len(backups) > keep else []:
            try:
                stale.unlink()
            except OSError as exc:
                logger.debug("Backup prune failed for %s: %s", stale.name, exc)
        if len(backups) > keep:
            logger.info(
                "Pruned %d old pre-migration backup(s), kept %d",
                len(backups) - keep,
                keep,
            )
    except OSError as exc:
        logger.debug("Backup prune skipped: %s", exc)


async def run_pending_migrations(conn: AsyncConnection) -> None:
    """Apply any unapplied migrations in order.

    Uses the applied-ID SET (not MAX) so a missed middle migration backfills.
    Runs ``PRAGMA integrity_check`` first and backs up the DB file before DDL.
    Must be called inside an ``engine.begin()`` transaction.
    """
    await _ensure_migrations_table(conn)

    try:
        chk = await conn.execute(text("PRAGMA integrity_check"))
        row = chk.scalar() or chk.fetchone()
        status = str(row[0] if isinstance(row, (list, tuple)) else row or "").lower()
        if status and status != "ok":
            logger.error("DB integrity_check failed before migrations: %s", row)
            raise RuntimeError(f"Database integrity check failed: {row}")
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("integrity_check skipped: %s", exc)

    result = await conn.execute(text("SELECT id FROM schema_migrations"))
    applied: set[int] = {row[0] for row in result.all()}
    pending = [mid for mid, _sql in _MIGRATIONS if mid not in applied]

    # Best-effort pre-migrate backup (same dir, timestamped) — only when
    # there is actual DDL to run, otherwise every boot would pile up a
    # ~300 KB duplicate. Retain the newest few, prune the rest.
    if pending:
        try:
            from backend.config import DB_DIR as _db_dir

            db_file = _db_dir / "transfera.db"
            if db_file.is_file():
                ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                backup = _db_dir / f"transfera.pre-migrate-{ts}.bak"
                try:
                    import shutil as _shutil

                    _shutil.copy2(str(db_file), str(backup))
                    logger.info("Pre-migration backup written: %s", backup.name)
                except OSError as exc:
                    logger.warning("Pre-migration backup failed: %s", exc)
                _prune_old_backups(_db_dir, keep=5)
        except Exception as exc:
            logger.debug("Backup probe skipped: %s", exc)
    else:
        # No DDL pending: still prune backups left by older versions.
        try:
            from backend.config import DB_DIR as _db_dir

            _prune_old_backups(_db_dir, keep=5)
        except Exception as exc:
            logger.debug("Backup prune skipped: %s", exc)

    for mid, sql in _MIGRATIONS:
        if mid in applied:
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
            text("INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (:id, :applied_at)"),
            {"id": mid, "applied_at": now},
        )

    _latest_migration_id = _MIGRATIONS[-1][0] if _MIGRATIONS else 0
    logger.info(
        "Schema migrations up to date (latest_id=%d, applied=%d)",
        _latest_migration_id,
        len(applied),
    )
