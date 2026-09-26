"""
Regression test suite for data safety and architectural fixes from the deep audit.

Covers:
1. session_scope retry commit on database locked without generator didn't stop crash.
2. claim_archive_path mutual exclusion & race condition closing.
3. duplicate_detector only matching COMPLETED vaulted files.
4. check_free_space and disk full transient exception rejection.
5. Windows reserved filename and character sanitization.
"""

from __future__ import annotations

import errno
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from backend.database.manager import session_scope
from backend.database.models import HopStatus, MediaItem, TransferBatch, TransferSession
from backend.engines.cache_manager import _is_transient_exc
from backend.engines.duplicate_detector import scan_batch_duplicates
from backend.engines.organizer import claim_archive_path
from backend.utils.durability import check_free_space, sanitize_filename


class TestSessionScopeCommitRetry:
    """Validate that commit retry on database lock does NOT crash the generator."""

    @pytest.mark.asyncio
    async def test_commit_retry_succeeds_without_generator_stop_error(self):
        """When session.commit() raises database is locked once then succeeds,
        session_scope completes cleanly without RuntimeError('generator didn't stop')."""
        mock_session = AsyncMock()
        attempts = 0

        async def fake_commit():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("database is locked", {}, Exception("locked"))
            return None

        mock_session.commit = fake_commit
        mock_factory = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        mock_factory.return_value = mock_ctx

        with patch("backend.database.manager._session_factory", mock_factory):
            executed = False
            async with session_scope() as session:
                assert session is mock_session
                executed = True

            assert executed
            assert attempts == 2


class TestClaimArchivePathMutualExclusion:
    """Validate that claim_archive_path holds reservation to prevent collision."""

    def test_claim_archive_path_allocates_consecutive_slots(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest_root = Path(tmp_dir)
            item1 = MediaItem(source_path="foo.jpg", file_name="photo.jpg", file_size=100)
            item2 = MediaItem(source_path="bar.jpg", file_name="photo.jpg", file_size=100)

            path1 = claim_archive_path(dest_root, item1, layout="flat")
            path2 = claim_archive_path(dest_root, item2, layout="flat")

            assert path1.name == "photo.jpg"
            assert path2.name == "photo_001.jpg"
            # Both reservation files exist on disk
            assert path1.with_suffix(path1.suffix + ".partial").exists()
            assert path2.with_suffix(path2.suffix + ".partial").exists()


class TestDuplicateDetectionOnlyCompleted:
    """Validate that duplicate detection ignores incomplete/failed transfer items."""

    @pytest.mark.asyncio
    async def test_only_completed_items_flagged_as_duplicates(self, db_session):
        # 1. Create a session with a completed item and a failed item (different sessions)
        old_session = TransferSession(
            session_name="old",
            source_root="C:/old",
            dest_root="C:/dest",
            total_items=2,
        )
        db_session.add(old_session)
        await db_session.flush()

        # Item that successfully completed in old_session
        vaulted_item = MediaItem(
            session_id=old_session.id,
            source_path="C:/old/completed.jpg",
            file_name="completed.jpg",
            file_size=5000,
            source_hash="abcd1234abcd1234",
            final_status=HopStatus.COMPLETED.value,
        )
        # Item that FAILED in old_session (never made it to destination)
        failed_item = MediaItem(
            session_id=old_session.id,
            source_path="C:/old/failed.jpg",
            file_name="failed.jpg",
            file_size=6000,
            source_hash="deadbeefdeadbeef",
            final_status=HopStatus.FAILED.value,
        )
        db_session.add_all([vaulted_item, failed_item])
        await db_session.flush()

        # 2. Create new session and batch with items sharing the same hashes
        new_session = TransferSession(
            session_name="new",
            source_root="C:/new",
            dest_root="C:/dest",
            total_items=2,
        )
        db_session.add(new_session)
        await db_session.flush()

        batch = TransferBatch(session_id=new_session.id, batch_number=1, total_items=2)
        db_session.add(batch)
        await db_session.flush()

        new_item1 = MediaItem(
            session_id=new_session.id,
            batch_id=batch.id,
            source_path="C:/new/completed.jpg",
            file_name="completed.jpg",
            file_size=5000,
            source_hash="abcd1234abcd1234",
        )
        new_item2 = MediaItem(
            session_id=new_session.id,
            batch_id=batch.id,
            source_path="C:/new/failed.jpg",
            file_name="failed.jpg",
            file_size=6000,
            source_hash="deadbeefdeadbeef",
        )
        db_session.add_all([new_item1, new_item2])
        await db_session.commit()

        # 3. Run duplicate detection on the new batch
        report = await scan_batch_duplicates(batch.id)

        # Only the vaulted completed item should be detected as exact duplicate
        assert len(report.exact_duplicates) == 1
        assert report.exact_duplicates[0].file_name == "completed.jpg"
        assert report.exact_duplicates[0].source_hash == "abcd1234abcd1234"

        # The failed item MUST NOT be flagged as duplicate
        duplicate_names = [e.file_name for e in report.exact_duplicates + report.potential_duplicates]
        assert "failed.jpg" not in duplicate_names


class TestDurabilityHelpers:
    """Validate sanitization, disk space check, and exception discrimination."""

    def test_sanitize_filename_windows_reserved(self):
        assert sanitize_filename("CON.jpg") == "_CON.jpg"
        assert sanitize_filename("aux.png") == "_aux.png"
        assert sanitize_filename("com1.mp4") == "_com1.mp4"
        assert sanitize_filename("nul.txt") == "_nul.txt"

    def test_sanitize_filename_illegal_chars(self):
        assert sanitize_filename('bad:file*name?"<>.jpg') == "bad_file_name____.jpg"
        assert sanitize_filename("trailing.dot. ") == "trailing.dot"

    def test_disk_full_never_treated_as_transient(self):
        enospc_exc = OSError(errno.ENOSPC, "No space left on device")
        assert _is_transient_exc(enospc_exc) is False

        win_disk_full = OSError()
        win_disk_full.winerror = 112
        assert _is_transient_exc(win_disk_full) is False

    def test_check_free_space(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            is_ok, free_b, req_b = check_free_space(Path(tmp_dir), 1024)
            assert is_ok is True
            assert free_b > 0
            assert req_b > 1024
