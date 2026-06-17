"""
MediaVault v2 — End-to-End Smoke Test
Validates the full integrated pipeline: scan -> batch -> Hop1 -> Hop2 -> recovery.
Run: python -m backend.test_smoke
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import select  # noqa: E402
from backend.database.manager import (  # noqa: E402
    create_all_tables,
    dispose_engine,
    session_scope,
)
from backend.database.models import (  # noqa: E402
    HopStatus,
    MediaItem,
    TransferSession,
)
from backend.engines.batch_manager import create_batches  # noqa: E402
from backend.engines.cache_manager import cache_batch  # noqa: E402
from backend.engines.importer import import_batch  # noqa: E402
from backend.engines.recovery import recover_interrupted_batches  # noqa: E402
from backend.engines.scanner import scan  # noqa: E402

_PASS = 0
_FAIL = 0


def _check(name: str, condition: bool) -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}")


async def smoke_test() -> None:
    print("=" * 60)
    print("  MediaVault v2 -- End-to-End Smoke Test")
    print("=" * 60)

    await dispose_engine()
    await create_all_tables()

    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "source"
        dest_dir = Path(tmp) / "dest"
        cache_dir = Path(tmp) / "cache"
        src_dir.mkdir()

        # Create 25 test files
        for i in range(25):
            (src_dir / f"photo_{i:03d}.jpg").write_bytes(f"content-{i}".encode())

        # 1. Create session
        async with session_scope() as session:
            ts = TransferSession(
                session_name="smoke-test",
                source_root=str(src_dir),
                dest_root=str(dest_dir),
            )
            session.add(ts)
            await session.flush()
            session_id = ts.id
        _check("Session created", session_id is not None)

        # 2. Scan source
        item_ids = await scan(src_dir, session_id=session_id)
        _check(f"Scanned {len(item_ids)} items", len(item_ids) == 25)

        # 3. Create batches
        batch_ids = await create_batches(session_id, item_ids)
        _check(f"Created {len(batch_ids)} batch", len(batch_ids) == 1)

        # 4. Hop 1: Cache
        cached = await cache_batch(batch_ids[0], cache_dir=cache_dir)
        _check(f"Hop 1 cached {cached}/25", cached == 25)

        # 5. Hop 2: Import
        imported = await import_batch(batch_ids[0], dest_root=dest_dir, cache_dir=cache_dir)
        _check(f"Hop 2 imported {imported}/25", imported == 25)

        # 6. Verify destination files
        dest_files = list(dest_dir.rglob("*.jpg"))
        _check(f"Destination has 25 files", len(dest_files) == 25)

        # 7. Verify all items completed
        async with session_scope() as session:
            result = await session.execute(
                select(MediaItem.final_status).where(MediaItem.session_id == session_id)
            )
            statuses = [r[0] for r in result.all()]
            all_done = all(s == HopStatus.COMPLETED.value for s in statuses)
        _check("All items COMPLETED", all_done)

        # 8. Recovery (clean state)
        stats = await recover_interrupted_batches(cache_dir=cache_dir)
        _check(
            "Recovery clean",
            stats["loading_recovered"] == 0 and stats["archived_recovered"] == 0,
        )

        # 9. No .partial files
        partials = list(cache_dir.rglob("*.partial")) + list(dest_dir.rglob("*.partial"))
        _check("No .partial files", len(partials) == 0)

    await dispose_engine()

    print()
    print("=" * 60)
    total = _PASS + _FAIL
    print(f"  Results: {_PASS}/{total} passed, {_FAIL} failed")
    print("=" * 60)

    if _FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(smoke_test())
