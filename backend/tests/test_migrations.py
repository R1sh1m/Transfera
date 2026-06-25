"""
Transfera v2 — Schema migration tests.

Verifies the numbered migration system is idempotent and produces the
expected schema on a fresh database.
"""

from __future__ import annotations

import pytest

from sqlalchemy import text

from backend.database.manager import create_all_tables, dispose_engine, get_engine


@pytest.mark.asyncio
async def test_migrations_fresh_db() -> None:
    """Migrations from scratch should create all columns."""
    engine = await get_engine()
    async with engine.begin() as conn:
        # Drop the migration ledger so we test from a clean state
        await conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))

    await create_all_tables()

    async with engine.begin() as conn:
        # Verify ledger exists
        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"))
        assert result.fetchone() is not None, "schema_migrations table must exist"

        # Verify all migrations were recorded
        result = await conn.execute(text("SELECT COUNT(*) FROM schema_migrations"))
        count = result.scalar()
        assert count == 21, f"Expected 21 migrations, got {count}"

        # Verify a sample of the columns actually exist
        for table, col in [
            ("transfer_sessions", "total_files"),
            ("transfer_sessions", "folder_layout"),
            ("transfer_sessions", "selected_files_json"),
            ("media_items", "thumbnail_status"),
            ("media_items", "original_capture_time"),
        ]:
            result = await conn.execute(
                text("SELECT name FROM pragma_table_info(:table) WHERE name=:col"),
                {"table": table, "col": col},
            )
            assert result.fetchone() is not None, f"Column {table}.{col} should exist"


@pytest.mark.asyncio
async def test_migrations_idempotent() -> None:
    """Re-running migrations should be a no-op (no errors, ledger unchanged)."""
    engine = await get_engine()
    async with engine.begin() as conn:
        before = (await conn.execute(text("SELECT COUNT(*) FROM schema_migrations"))).scalar()

    await create_all_tables()

    async with engine.begin() as conn:
        after = (await conn.execute(text("SELECT COUNT(*) FROM schema_migrations"))).scalar()
        assert after == before, "Migration ledger count should not change on re-run"
