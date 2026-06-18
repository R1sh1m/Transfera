"""
Transfera v2 — API Routes
All HTTP endpoints for the Transfera backend.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select

from backend.api.schemas import (
    BatchInfo,
    BatchList,
    ConfigResponse,
    DirSizeRequest,
    DirSizeResponse,
    DuplicateCheckRequest,
    DuplicateReportResponse,
    DuplicateEntrySchema,
    ErrorResponse,
    FolderMetadataRequest,
    FolderMetadataResponse,
    HealthResponse,
    MediaItemInfo,
    MediaList,
    PreflightValidateRequest,
    PreflightValidateResponse,
    ScanRequest,
    ScanResponse,
    SessionActionResponse,
    SessionCreate,
    SessionInfo,
    SessionList,
)
from backend.api.websocket import manager as ws_manager
from backend.api import websocket as ws_events
from backend.config import (
    AUDIO_EXTENSIONS,
    BATCH_SIZE,
    CACHE_DIR,
    DB_DIR,
    DOCUMENT_EXTENSIONS,
    HOST,
    IMAGE_EXTENSIONS,
    MAX_RETRY,
    PORT,
    VIDEO_EXTENSIONS,
)
from backend.database.manager import create_all_tables, get_engine, get_session, session_scope
from backend.database.models import (
    BatchStatus,
    HopStatus,
    MediaItem,
    SessionStatus,
    TransferBatch,
    TransferSession,
)
from backend.engines.batch_manager import create_batches, get_batch_items, mark_batch_status
from backend.engines.cache_manager import cache_batch
from backend.engines.duplicate_detector import check_batch
from backend.engines.importer import import_batch
from backend.engines.recovery import recover_interrupted_batches
from backend.engines.reporter import generate_session_report
from backend.engines.scanner import scan as run_scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "version": "2.0"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@router.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    return ConfigResponse(
        port=PORT,
        host=HOST,
        batch_size=BATCH_SIZE,
        max_retry=MAX_RETRY,
        cache_dir=str(CACHE_DIR),
        db_dir=str(DB_DIR),
        image_extensions=sorted(IMAGE_EXTENSIONS),
        video_extensions=sorted(VIDEO_EXTENSIONS),
        audio_extensions=sorted(AUDIO_EXTENSIONS),
        document_extensions=sorted(DOCUMENT_EXTENSIONS),
    )


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
@router.post("/scan", response_model=ScanResponse)
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks) -> ScanResponse:
    source = Path(req.source_path).resolve()
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Source not found: {source}")

    dest = Path(req.dest_path).resolve() if req.dest_path else CACHE_DIR
    session_name = req.session_name or f"scan-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    # Create session
    async with session_scope() as session:
        ts = TransferSession(
            session_name=session_name,
            source_root=str(source),
            dest_root=str(dest),
        )
        session.add(ts)
        await session.flush()
        session_id = ts.id

    # Run scan in background thread
    background_tasks.add_task(_run_scan_background, session_id, str(source))

    return ScanResponse(
        session_id=session_id,
        status="scanning",
        message=f"Scan started for {source}",
    )


async def _run_scan_background(session_id: int, source_path: str) -> None:
    """Background task: scan source and emit progress events."""
    try:
        def _scan_sync() -> list[int]:
            import asyncio as _aio
            loop = _aio.new_event_loop()
            try:
                return loop.run_until_complete(
                    run_scan(source_path, session_id=session_id)
                )
            finally:
                loop.close()

        item_ids = await asyncio.to_thread(_scan_sync)

        # Create batches
        batch_ids = await create_batches(session_id, item_ids)

        # Emit events
        await ws_events.emit_scan_complete(session_id, len(item_ids))
        for bid in batch_ids:
            async with session_scope() as session:
                batch = await session.get(TransferBatch, bid)
                await ws_events.emit_batch_created(
                    session_id, bid, batch.batch_number, batch.total_items
                )

        # Mark session as ready
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.total_items = len(item_ids)
                ts.touch()

    except Exception as exc:
        logger.error("Scan background failed for session %d: %s", session_id, exc)
        await ws_events.emit_error(session_id, str(exc))


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------
@router.post("/sessions", response_model=SessionInfo)
async def create_session(req: SessionCreate) -> SessionInfo:
    async with session_scope() as session:
        ts = TransferSession(
            session_name=req.session_name,
            source_root=req.source_root,
            dest_root=req.dest_root,
            transfer_mode=req.transfer_mode,
        )
        session.add(ts)
        await session.flush()
        return _session_to_info(ts)


@router.get("/sessions", response_model=SessionList)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> SessionList:
    async with session_scope() as session:
        # Count
        count_q = select(func.count(TransferSession.id))
        total = (await session.execute(count_q)).scalar() or 0

        # Fetch page
        offset = (page - 1) * page_size
        result = await session.execute(
            select(TransferSession)
            .order_by(TransferSession.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = [_session_to_info(ts) for ts in result.scalars().all()]

    return SessionList(
        sessions=items,
        total=total,
    )


@router.get("/sessions/{session_id}", response_model=SessionInfo)
async def get_session_detail(session_id: int) -> SessionInfo:
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_to_info(ts)


@router.post("/sessions/{session_id}/start", response_model=SessionActionResponse)
async def start_session(session_id: int, background_tasks: BackgroundTasks) -> SessionActionResponse:
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if ts.status not in (SessionStatus.CREATED.value, SessionStatus.PAUSED.value):
            raise HTTPException(status_code=400, detail=f"Cannot start session in status: {ts.status}")
        ts.status = SessionStatus.RUNNING.value
        ts.started_at = datetime.now(timezone.utc)
        ts.touch()

    background_tasks.add_task(_run_transfer_background, session_id)

    await ws_events.emit_session_started(session_id)
    return SessionActionResponse(session_id=session_id, status="running", message="Session started")


@router.post("/sessions/{session_id}/pause", response_model=SessionActionResponse)
async def pause_session(session_id: int) -> SessionActionResponse:
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if ts.status != SessionStatus.RUNNING.value:
            raise HTTPException(status_code=400, detail=f"Cannot pause session in status: {ts.status}")
        ts.status = SessionStatus.PAUSED.value
        ts.touch()

    await ws_events.emit_session_paused(session_id)
    return SessionActionResponse(session_id=session_id, status="paused", message="Session paused")


@router.post("/sessions/{session_id}/cancel", response_model=SessionActionResponse)
async def cancel_session(session_id: int) -> SessionActionResponse:
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")
        ts.status = SessionStatus.CANCELLED.value
        ts.completed_at = datetime.now(timezone.utc)
        ts.touch()

    return SessionActionResponse(session_id=session_id, status="cancelled", message="Session cancelled")


async def _run_transfer_background(session_id: int) -> None:
    """Background task: process all batches through Hop 1 then Hop 2."""
    try:
        async with session_scope() as session:
            result = await session.execute(
                select(TransferBatch)
                .where(TransferBatch.session_id == session_id)
                .order_by(TransferBatch.batch_number)
            )
            batches = list(result.scalars().all())

        for batch in batches:
            # Check if session was paused/cancelled
            async with session_scope() as session:
                ts = await session.get(TransferSession, session_id)
                if ts and ts.status != SessionStatus.RUNNING.value:
                    return

            # --- Pre-flight duplicate check ---
            report = await check_batch(batch.id)
            if report.has_duplicates:
                await ws_events.emit_duplicates_detected(session_id, {
                    "batch_id": batch.id,
                    "exact_count": len(report.exact_duplicates),
                    "potential_count": len(report.potential_duplicates),
                    "summary": report.summary,
                })
                # Pause and wait for user resolution
                async with session_scope() as session:
                    ts = await session.get(TransferSession, session_id)
                    if ts:
                        ts.status = SessionStatus.PAUSED.value
                        ts.touch()
                await ws_events.emit_session_paused(session_id)
                return

            # --- Hop 1: Source -> Cache ---
            await ws_events.emit_batch_processing(session_id, batch.id, batch.batch_number)

            def _hop1_sync() -> int:
                import asyncio as _aio
                loop = _aio.new_event_loop()
                try:
                    return loop.run_until_complete(cache_batch(batch.id, cache_dir=CACHE_DIR))
                finally:
                    loop.close()

            cached = await asyncio.to_thread(_hop1_sync)
            await ws_events.emit_hop1_complete(session_id, batch.id, cached)

            # --- Hop 2: Cache -> Destination ---
            async with session_scope() as session:
                ts = await session.get(TransferSession, session_id)
                dest_root = Path(ts.dest_root) if ts else CACHE_DIR

            def _hop2_sync() -> int:
                import asyncio as _aio
                loop = _aio.new_event_loop()
                try:
                    return loop.run_until_complete(
                        import_batch(batch.id, dest_root=dest_root, cache_dir=CACHE_DIR)
                    )
                finally:
                    loop.close()

            imported = await asyncio.to_thread(_hop2_sync)
            await ws_events.emit_hop2_complete(session_id, batch.id, imported)
            await ws_events.emit_batch_complete(session_id, batch.id, batch.batch_number, "completed")

        # All batches done
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.status = SessionStatus.COMPLETED.value
                ts.completed_at = datetime.now(timezone.utc)
                ts.completed_items = ts.total_items
                ts.touch()

        await ws_events.emit_session_complete(session_id, {
            "total_items": ts.total_items if ts else 0,
            "completed_items": ts.completed_items if ts else 0,
            "failed_items": ts.failed_items if ts else 0,
        })

        # --- Post-session report generation ---
        try:
            report_path = await generate_session_report(session_id)
            async with session_scope() as session:
                ts = await session.get(TransferSession, session_id)
                if ts:
                    ts.session_report_path = str(report_path)
                    ts.touch()
            logger.info("Session %d report saved at %s", session_id, report_path)
        except Exception as report_exc:
            logger.error("Failed to generate report for session %d: %s", session_id, report_exc)

    except Exception as exc:
        logger.error("Transfer background failed for session %d: %s", session_id, exc)
        await ws_events.emit_error(session_id, str(exc))
        async with session_scope() as session:
            ts = await session.get(TransferSession, session_id)
            if ts:
                ts.status = SessionStatus.FAILED.value
                ts.error_message = str(exc)
                ts.touch()

        # --- Post-session report generation (failure case) ---
        try:
            report_path = await generate_session_report(session_id)
            async with session_scope() as session:
                ts = await session.get(TransferSession, session_id)
                if ts:
                    ts.session_report_path = str(report_path)
                    ts.touch()
            logger.info("Session %d failure report saved at %s", session_id, report_path)
        except Exception as report_exc:
            logger.error("Failed to generate failure report for session %d: %s", session_id, report_exc)


# ---------------------------------------------------------------------------
# Duplicate Handling
# ---------------------------------------------------------------------------
@router.post("/duplicates/check", response_model=DuplicateReportResponse)
async def check_duplicates(req: DuplicateCheckRequest) -> DuplicateReportResponse:
    report = await check_batch(req.batch_id)
    return DuplicateReportResponse(
        batch_id=report.batch_id,
        session_id=report.session_id,
        checked_at=report.checked_at,
        exact_duplicates=[
            DuplicateEntrySchema(
                item_id=e.item_id,
                file_name=e.file_name,
                source_path=e.source_path,
                source_hash=e.source_hash,
                file_size=e.file_size,
                match_type=e.match_type,
                matched_path=e.matched_path,
            )
            for e in report.exact_duplicates
        ],
        potential_duplicates=[
            DuplicateEntrySchema(
                item_id=e.item_id,
                file_name=e.file_name,
                source_path=e.source_path,
                source_hash=e.source_hash,
                file_size=e.file_size,
                match_type=e.match_type,
                matched_path=e.matched_path,
            )
            for e in report.potential_duplicates
        ],
        total_items_checked=report.total_items_checked,
        processing_paused=report.processing_paused,
        summary=report.summary,
    )


# ---------------------------------------------------------------------------
# Media Library
# ---------------------------------------------------------------------------
@router.get("/media", response_model=MediaList)
async def list_media(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    session_id: Optional[int] = Query(None),
    hop1_status: Optional[str] = Query(None),
    hop2_status: Optional[str] = Query(None),
    final_status: Optional[str] = Query(None),
    extension: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
) -> MediaList:
    async with session_scope() as session:
        # Build base query
        q = select(MediaItem)
        count_q = select(func.count(MediaItem.id))

        # Apply filters
        filters = []
        if session_id is not None:
            filters.append(MediaItem.session_id == session_id)
        if hop1_status is not None:
            filters.append(MediaItem.hop1_status == hop1_status)
        if hop2_status is not None:
            filters.append(MediaItem.hop2_status == hop2_status)
        if final_status is not None:
            filters.append(MediaItem.final_status == final_status)
        if extension is not None:
            filters.append(MediaItem.extension == extension.lower())
        if search:
            filters.append(MediaItem.file_name.ilike(f"%{search}%"))

        for f in filters:
            q = q.where(f)
            count_q = count_q.where(f)

        total = (await session.execute(count_q)).scalar() or 0
        pages = math.ceil(total / page_size) if total > 0 else 1

        offset = (page - 1) * page_size
        result = await session.execute(
            q.order_by(MediaItem.id).offset(offset).limit(page_size)
        )
        items = [_media_to_info(mi) for mi in result.scalars().all()]

    return MediaList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


# ---------------------------------------------------------------------------
# Batch queries
# ---------------------------------------------------------------------------
@router.get("/sessions/{session_id}/batches", response_model=BatchList)
async def list_batches(session_id: int) -> BatchList:
    async with session_scope() as session:
        ts = await session.get(TransferSession, session_id)
        if ts is None:
            raise HTTPException(status_code=404, detail="Session not found")

        result = await session.execute(
            select(TransferBatch)
            .where(TransferBatch.session_id == session_id)
            .order_by(TransferBatch.batch_number)
        )
        batches = [_batch_to_info(b) for b in result.scalars().all()]

    return BatchList(batches=batches, total=len(batches))


# ---------------------------------------------------------------------------
# Crash Recovery
# ---------------------------------------------------------------------------
@router.post("/recovery", response_model=SessionActionResponse)
async def trigger_recovery() -> SessionActionResponse:
    stats = await recover_interrupted_batches(cache_dir=CACHE_DIR)
    return SessionActionResponse(
        session_id=0,
        status="recovered",
        message=f"Recovered {stats['loading_recovered']} LOADING, "
                f"{stats['archived_recovered']} ARCHIVED batches",
    )


# ---------------------------------------------------------------------------
# Directory Size Metrics
# ---------------------------------------------------------------------------
def _measure_directory(dir_path: str) -> dict:
    """Synchronous directory traversal — total size, file count, folder count."""
    total_bytes = 0
    file_count = 0
    folder_count = 0
    p = Path(dir_path)
    if not p.exists():
        return {"total_bytes": 0, "file_count": 0, "folder_count": 0, "exists": False}
    for entry in p.rglob("*"):
        if entry.is_file():
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                pass  # skip inaccessible files
            file_count += 1
        elif entry.is_dir():
            folder_count += 1
    return {"total_bytes": total_bytes, "file_count": file_count, "folder_count": folder_count, "exists": True}


def _format_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    return f"{size_bytes / (1024 ** 3):.2f} GB"


@router.post("/utils/dir-size", response_model=DirSizeResponse)
async def get_dir_size(req: DirSizeRequest) -> DirSizeResponse:
    try:
        result = await asyncio.to_thread(_measure_directory, req.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot measure directory: {exc}")

    if not result["exists"]:
        raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")

    return DirSizeResponse(
        path=req.path,
        total_bytes=result["total_bytes"],
        file_count=result["file_count"],
        folder_count=result["folder_count"],
        readable=_format_size(result["total_bytes"]),
    )


# ---------------------------------------------------------------------------
# Folder Metadata (lightweight size + count for dashboard cards)
# ---------------------------------------------------------------------------
def _measure_folder_metadata(dir_path: str) -> dict:
    """Synchronous directory traversal returning size in GB and file count."""
    total_bytes = 0
    file_count = 0
    p = Path(dir_path)
    if not p.exists():
        return {"size_gb": 0.0, "file_count": 0}
    for entry in p.rglob("*"):
        if entry.is_file():
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                pass
            file_count += 1
    return {"size_gb": round(total_bytes / (1024 ** 3), 2), "file_count": file_count}


@router.post("/utils/folder-metadata", response_model=FolderMetadataResponse)
async def get_folder_metadata(req: FolderMetadataRequest) -> FolderMetadataResponse:
    """Return aggregate file size (GB) and file count for a directory."""
    try:
        result = await asyncio.to_thread(_measure_folder_metadata, req.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot measure folder: {exc}")
    return FolderMetadataResponse(
        path=req.path,
        size_gb=result["size_gb"],
        file_count=result["file_count"],
    )


# ---------------------------------------------------------------------------
# Preflight Disk Capacity Validation
# ---------------------------------------------------------------------------
def _scan_source_volume(source_path: str) -> dict:
    """Walk source directory with os.scandir and return aggregate size + file count."""
    total_bytes = 0
    file_count = 0
    try:
        with os.scandir(source_path) as scanner:
            for entry in scanner:
                if entry.is_file(follow_symlinks=False):
                    try:
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                    file_count += 1
                elif entry.is_dir(follow_symlinks=False):
                    for sub_file in _scan_dir_tree(entry.path):
                        total_bytes += sub_file[0]
                        file_count += sub_file[1]
    except FileNotFoundError:
        return {"source_size_bytes": 0, "file_count": 0, "exists": False}
    except PermissionError:
        return {"source_size_bytes": 0, "file_count": 0, "exists": False}
    return {"source_size_bytes": total_bytes, "file_count": file_count, "exists": True}


def _scan_dir_tree(dir_path: str) -> list[tuple[int, int]]:
    """Recursive scandir walker yielding (byte_size, file_count) per subdirectory."""
    results: list[tuple[int, int]] = []
    try:
        with os.scandir(dir_path) as scanner:
            for entry in scanner:
                if entry.is_file(follow_symlinks=False):
                    try:
                        sz = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        sz = 0
                    results.append((sz, 1))
                elif entry.is_dir(follow_symlinks=False):
                    results.extend(_scan_dir_tree(entry.path))
    except (FileNotFoundError, PermissionError):
        pass
    return results


def _get_dest_free_space(dest_path: str) -> int:
    """Return free bytes on the drive hosting dest_path."""
    usage = shutil.disk_usage(dest_path)
    return usage.free


def _preflight_validate_sync(source_path: str, dest_path: str) -> dict:
    """Synchronous pre-flight: source volume scan + destination free space check."""
    source = _scan_source_volume(source_path)
    dest_free = _get_dest_free_space(dest_path)

    source_size = source["source_size_bytes"]
    is_sufficient = dest_free >= source_size

    return {
        "source_size_bytes": source_size,
        "dest_free_bytes": dest_free,
        "is_sufficient": is_sufficient,
        "file_count": source["file_count"],
    }


@router.post("/utils/preflight-validate", response_model=PreflightValidateResponse)
async def preflight_validate(req: PreflightValidateRequest) -> PreflightValidateResponse:
    """Pre-flight disk capacity check: compare source volume against destination free space."""
    # Validate paths exist
    if not Path(req.source_path).exists():
        raise HTTPException(status_code=404, detail=f"Source path not found: {req.source_path}")
    if not Path(req.dest_path).exists():
        raise HTTPException(status_code=404, detail=f"Destination path not found: {req.dest_path}")

    try:
        result = await asyncio.to_thread(_preflight_validate_sync, req.source_path, req.dest_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Preflight validation failed: {exc}")

    # --- Logging warnings ---
    src_human = _format_size(result["source_size_bytes"])
    free_human = _format_size(result["dest_free_bytes"])

    if not result["is_sufficient"]:
        logger.warning(
            "PREFLIGHT BLOCKED: destination free space (%s) is LESS than source volume (%s, %d files) "
            "at dest=%s",
            free_human, src_human, result["file_count"], req.dest_path,
        )
    else:
        margin = result["dest_free_bytes"] - result["source_size_bytes"]
        if margin < result["source_size_bytes"] * 0.1:
            logger.warning(
                "PREFLIGHT WARNING: destination free space (%s) is dangerously close to source volume "
                "(%s, %d files) — only %s headroom at dest=%s",
                free_human, src_human, result["file_count"], _format_size(margin), req.dest_path,
            )
        else:
            logger.info(
                "PREFLIGHT OK: source=%s (%d files), dest_free=%s at dest=%s",
                src_human, result["file_count"], free_human, req.dest_path,
            )

    return PreflightValidateResponse(
        source_size_bytes=result["source_size_bytes"],
        dest_free_bytes=result["dest_free_bytes"],
        is_sufficient=result["is_sufficient"],
        file_count=result["file_count"],
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
async def ws_transfer(websocket: WebSocket, session_id: int) -> None:
    await ws_manager.connect(websocket, session_id)
    try:
        while True:
            data = await websocket.receive_json()
            # Handle pong responses
            if data.get("event") == "pong":
                continue
            # Handle client-initiated events if needed
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, session_id)
    except Exception:
        ws_manager.disconnect(websocket, session_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _session_to_info(ts: TransferSession) -> SessionInfo:
    return SessionInfo(
        id=ts.id,
        session_name=ts.session_name,
        source_root=ts.source_root,
        dest_root=ts.dest_root,
        transfer_mode=ts.transfer_mode,
        status=ts.status,
        total_items=ts.total_items,
        completed_items=ts.completed_items,
        failed_items=ts.failed_items,
        total_bytes_volume=ts.total_bytes_volume,
        session_report_path=ts.session_report_path,
        created_at=ts.created_at,
        updated_at=ts.updated_at,
        started_at=ts.started_at,
        completed_at=ts.completed_at,
    )


def _media_to_info(mi: MediaItem) -> MediaItemInfo:
    return MediaItemInfo(
        id=mi.id,
        source_path=mi.source_path,
        file_name=mi.file_name,
        file_size=mi.file_size,
        extension=mi.extension,
        mime_type=mi.mime_type,
        hop1_status=mi.hop1_status,
        hop2_status=mi.hop2_status,
        final_status=mi.final_status,
        live_photo_group=mi.live_photo_group,
        created_at=mi.created_at,
        updated_at=mi.updated_at,
    )


def _batch_to_info(b: TransferBatch) -> BatchInfo:
    return BatchInfo(
        id=b.id,
        session_id=b.session_id,
        batch_number=b.batch_number,
        status=b.status,
        total_items=b.total_items,
        completed_items=b.completed_items,
        failed_items=b.failed_items,
        created_at=b.created_at,
        updated_at=b.updated_at,
    )
