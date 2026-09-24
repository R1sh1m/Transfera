"""
Transfera v2 — Intelligence API (offline-first).

Library-wide near-duplicate center (dHash), timeline/moments, people,
semantic keyword search (CLIP-ready), trash/favorites, backfill.

All endpoints require ``X-Local-Token`` like the rest of the API.
Heavy ML (SCRFD/ArcFace/CLIP) is optional: capabilities report what is
usable; every endpoint works with zero models installed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from backend.api.auth import require_local_token
from backend.api.schemas import (
    BackfillResponse,
    CapabilitiesResponse,
    DuplicateGroupResolveRequest,
    ManifestItemSchema,
    MediaItemInfo,
    MediaList,
    MediaPatchRequest,
    MomentSchema,
    MomentsResponse,
    NearDuplicateGroupSchema,
    NearDuplicateGroupsResponse,
    NearDuplicateItemSchema,
    PeopleListResponse,
    PeopleMergeRequest,
    PersonRenameRequest,
    PersonSchema,
    ReviewQueueResponse,
    SemanticSearchRequest,
    SemanticSearchResponse,
    SessionManifestResponse,
    SessionVerifyResponse,
    TimelineBucketSchema,
    TimelineResponse,
    VaultStatsResponse,
)
from backend.database.manager import session_scope
from backend.database.models import Face, MediaItem, Person, TransferSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


def _thumb_url(mi: MediaItem) -> str:
    try:
        return f"/api/media/{mi.id}/thumbnail?t={int(mi.updated_at.timestamp())}"
    except Exception:
        return f"/api/media/{mi.id}/thumbnail"


def _tags_of(mi: MediaItem) -> list[str]:
    try:
        return json.loads(mi.tags_json or "[]") if mi.tags_json else []
    except (json.JSONDecodeError, TypeError):
        return []


def _to_near_item(mi: MediaItem) -> NearDuplicateItemSchema:
    return NearDuplicateItemSchema(
        id=mi.id,
        file_name=mi.file_name,
        file_size=mi.file_size,
        phash=mi.phash,
        width=mi.width,
        height=mi.height,
        date_taken=mi.date_taken,
        favorite=bool(mi.favorite),
        thumbnail_url=_thumb_url(mi),
    )


def _to_media_info(mi: MediaItem) -> MediaItemInfo:
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
        thumbnail_url=_thumb_url(mi),
        thumbnail_status=mi.thumbnail_status,
        date_taken=mi.date_taken,
        date_source=mi.date_source,
        error_message=mi.error_message,
        created_at=mi.created_at,
        updated_at=mi.updated_at,
        phash=mi.phash,
        width=mi.width,
        height=mi.height,
        duration_s=mi.duration_s,
        camera_make=mi.camera_make,
        camera_model=mi.camera_model,
        gps_lat=mi.gps_lat,
        gps_lon=mi.gps_lon,
        favorite=bool(mi.favorite),
        trashed=bool(mi.trashed),
        blur_score=mi.blur_score,
        tags=_tags_of(mi),
        caption=mi.caption,
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities(_: None = Depends(require_local_token)) -> CapabilitiesResponse:
    from backend.engines.intelligence import get_capabilities

    caps = get_capabilities()
    return CapabilitiesResponse(**caps)


@router.get("/duplicates/groups", response_model=NearDuplicateGroupsResponse)
async def duplicate_groups(
    threshold: int = Query(8, ge=0, le=64),
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(require_local_token),
) -> NearDuplicateGroupsResponse:
    from backend.engines.perceptual_hash import group_near_duplicates, suggest_keeper

    async with session_scope() as session:
        result = await session.execute(
            select(MediaItem).where(
                MediaItem.final_status == "completed",
                MediaItem.trashed == False,  # noqa: E712
                MediaItem.phash.is_not(None),
            )
        )
        items = list(result.scalars().all())

    dicts = [
        {
            "id": mi.id,
            "phash": mi.phash,
            "file_name": mi.file_name,
            "file_size": mi.file_size,
            "width": mi.width,
            "height": mi.height,
            "date_taken": mi.date_taken,
            "favorite": bool(mi.favorite),
        }
        for mi in items
    ]
    by_id = {mi.id: mi for mi in items}
    raw_groups = group_near_duplicates(dicts, threshold=threshold)[:limit]
    groups: list[NearDuplicateGroupSchema] = []
    for g in raw_groups:
        keeper = suggest_keeper(g)
        groups.append(
            NearDuplicateGroupSchema(
                members=[_to_near_item(by_id[m["id"]]) for m in g],
                suggested_keeper_id=(keeper or {}).get("id"),
                threshold=threshold,
            )
        )
    return NearDuplicateGroupsResponse(groups=groups, total_groups=len(groups), threshold=threshold)


@router.post("/duplicates/groups/resolve")
async def resolve_duplicate_group(
    req: DuplicateGroupResolveRequest,
    _: None = Depends(require_local_token),
) -> dict:
    if req.keep_id in req.trash_ids:
        raise HTTPException(status_code=422, detail="keep_id must not be in trash_ids")
    async with session_scope() as session:
        keep = await session.get(MediaItem, req.keep_id)
        if keep is None:
            raise HTTPException(status_code=404, detail="Keeper not found")
        trashed = 0
        for tid in req.trash_ids:
            mi = await session.get(MediaItem, tid)
            if mi is None:
                continue
            mi.trashed = True
            mi.trashed_at = datetime.now(UTC)
            mi.touch()
            trashed += 1
    return {"keep_id": req.keep_id, "trashed": trashed}


@router.get("/timeline", response_model=TimelineResponse)
async def timeline(
    granularity: str = Query("month", pattern="^(month|day)$"),
    _: None = Depends(require_local_token),
) -> TimelineResponse:
    async with session_scope() as session:
        result = await session.execute(
            select(MediaItem).where(
                MediaItem.final_status == "completed",
                MediaItem.trashed == False,  # noqa: E712
            )
        )
        items = list(result.scalars().all())
    buckets: dict[str, list[MediaItem]] = {}
    for mi in items:
        dt = mi.date_taken or mi.original_capture_time or mi.created_at
        if dt is None:
            continue
        key = (
            f"{dt.year:04d}-{dt.month:02d}" if granularity == "month" else f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        )
        buckets.setdefault(key, []).append(mi)
    out: list[TimelineBucketSchema] = []
    for key in sorted(buckets):
        members = buckets[key]
        cover = max(members, key=lambda m: m.file_size or 0)
        dts = [m.date_taken or m.original_capture_time or m.created_at for m in members]
        dts = [d for d in dts if d is not None]
        out.append(
            TimelineBucketSchema(
                key=key,
                count=len(members),
                cover_id=cover.id,
                start=min(dts).isoformat() if dts else key,
                end=max(dts).isoformat() if dts else key,
            )
        )
    return TimelineResponse(granularity=granularity, buckets=out, total=len(items))


@router.get("/moments", response_model=MomentsResponse)
async def moments(
    gap_hours: float = Query(12.0, ge=1.0, le=168.0),
    _: None = Depends(require_local_token),
) -> MomentsResponse:
    from backend.engines.intelligence import cluster_moments

    async with session_scope() as session:
        result = await session.execute(
            select(MediaItem).where(
                MediaItem.final_status == "completed",
                MediaItem.trashed == False,  # noqa: E712
            )
        )
        items = list(result.scalars().all())
    dicts = [
        {"id": mi.id, "date_taken": mi.date_taken, "created_at": mi.created_at, "file_size": mi.file_size}
        for mi in items
    ]
    clustered = cluster_moments(dicts, gap_hours=gap_hours)
    return MomentsResponse(
        moments=[MomentSchema(**m) for m in clustered],
        total=len(clustered),
    )


@router.get("/people", response_model=PeopleListResponse)
async def list_people(_: None = Depends(require_local_token)) -> PeopleListResponse:
    async with session_scope() as session:
        result = await session.execute(select(Person).order_by(Person.face_count.desc()))
        people = list(result.scalars().all())
    return PeopleListResponse(
        people=[
            PersonSchema(
                id=p.id, name=p.name, face_count=p.face_count, cover_face_id=p.cover_face_id, hidden=bool(p.hidden)
            )
            for p in people
        ],
        total=len(people),
    )


@router.patch("/people/{person_id}", response_model=PersonSchema)
async def rename_person(
    person_id: int,
    req: PersonRenameRequest,
    _: None = Depends(require_local_token),
) -> PersonSchema:
    async with session_scope() as session:
        p = await session.get(Person, person_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Person not found")
        p.name = (req.name or "").strip() or None
        p.touch = getattr(p, "touch", None)
        # Person has no touch(); bump manually
        p.updated_at = datetime.now(UTC)
    return PersonSchema(
        id=p.id, name=p.name, face_count=p.face_count, cover_face_id=p.cover_face_id, hidden=bool(p.hidden)
    )


@router.post("/people/merge")
async def merge_people(
    req: PeopleMergeRequest,
    _: None = Depends(require_local_token),
) -> dict:
    async with session_scope() as session:
        target = await session.get(Person, req.target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Target person not found")
        moved = 0
        for sid in req.source_ids:
            if sid == req.target_id:
                continue
            src = await session.get(Person, sid)
            if src is None:
                continue
            fres = await session.execute(select(Face).where(Face.person_id == sid))
            for f in fres.scalars().all():
                f.person_id = req.target_id
                moved += 1
            await session.delete(src)
        # Refresh count
        cnt = await session.execute(select(func.count(Face.id)).where(Face.person_id == req.target_id))
        target.face_count = cnt.scalar() or 0
        target.updated_at = datetime.now(UTC)
    return {"target_id": req.target_id, "faces_moved": moved}


@router.post("/search/semantic", response_model=SemanticSearchResponse)
async def semantic_search(
    req: SemanticSearchRequest,
    _: None = Depends(require_local_token),
) -> SemanticSearchResponse:
    from backend.engines.intelligence import get_capabilities, semantic_search_keyword

    caps = get_capabilities()
    async with session_scope() as session:
        result = await session.execute(
            select(MediaItem)
            .where(
                MediaItem.final_status == "completed",
                MediaItem.trashed == False,  # noqa: E712
            )
            .limit(5000)
        )
        items = list(result.scalars().all())
    dicts = [
        {
            "id": mi.id,
            "file_name": mi.file_name,
            "tags_json": mi.tags_json,
            "caption": mi.caption,
            "camera_make": mi.camera_make,
            "camera_model": mi.camera_model,
        }
        for mi in items
    ]
    by_id = {mi.id: mi for mi in items}
    hits = semantic_search_keyword(req.query, dicts, limit=req.limit)
    mode = "clip" if caps.get("clip_available") else "keyword"
    return SemanticSearchResponse(
        query=req.query,
        mode=mode,
        results=[_to_media_info(by_id[h["id"]]) for h in hits],
        total=len(hits),
    )


@router.post("/backfill", response_model=BackfillResponse)
async def backfill_intelligence(
    limit: int = Query(2000, ge=1, le=10000),
    _: None = Depends(require_local_token),
) -> BackfillResponse:
    """Fill missing phash / dimensions / tags for completed items.

    Bounded (``limit``) so it is safe to call repeatedly; idempotent —
    only touches rows with NULL fields. Pure-Pillow, fully offline.
    """
    import io

    from backend.engines.intelligence import compute_blur_score, keyword_tags
    from backend.engines.perceptual_hash import dhash_bytes
    from backend.engines.thumbnailer import generate_thumbnail_bytes

    scanned = phash_filled = dims_filled = tags_filled = 0
    async with session_scope() as session:
        result = await session.execute(select(MediaItem).where(MediaItem.final_status == "completed").limit(limit))
        items = list(result.scalars().all())
        for mi in items:
            scanned += 1
            touched = False
            # Tags (cheap, always)
            if not mi.tags_json:
                try:
                    tags = keyword_tags(mi.file_name or "", mi.camera_model)
                    mi.tags_json = json.dumps(tags)
                    tags_filled += 1
                    touched = True
                except Exception:
                    pass
            # Dimensions via Pillow on the archived file (source_path may be stale;
            # thumbnail bytes are the reliable fallback for hashing)
            needs_visual = mi.phash is None or mi.width is None or mi.blur_score is None
            if needs_visual:
                try:
                    from pathlib import Path

                    from PIL import Image

                    p = Path(mi.source_path) if mi.source_path else None
                    thumb = generate_thumbnail_bytes(p) if p and p.is_file() else None
                    if thumb:
                        if mi.phash is None:
                            h = dhash_bytes(thumb)
                            if h:
                                mi.phash = h
                                phash_filled += 1
                                touched = True
                        if mi.width is None:
                            try:
                                with Image.open(io.BytesIO(thumb)) as im:
                                    mi.width, mi.height = im.size[0], im.size[1]
                                    dims_filled += 1
                                    touched = True
                            except Exception:
                                pass
                    if mi.blur_score is None and p and p.is_file():
                        s = compute_blur_score(p)
                        if s is not None:
                            mi.blur_score = s
                            touched = True
                except Exception as exc:
                    logger.debug("backfill skipped %s: %s", mi.id, exc)
            if touched:
                mi.touch()
    msg = f"backfilled {phash_filled} hashes, {dims_filled} dims, {tags_filled} tags across {scanned} items"
    logger.info(msg)
    return BackfillResponse(
        scanned=scanned, phash_filled=phash_filled, dims_filled=dims_filled, tags_filled=tags_filled, message=msg
    )


def _manifest_chain(items: list[ManifestItemSchema], session_id: int) -> str:
    """Tamper-evident chain hash over ordered per-item identities.

    sha256 of ``session_id`` + one line per item
    (``blake3:size:phash:file_name``), items ordered by id.
    Deterministic: same vault rows always yield the same chain.
    """
    import hashlib

    lines = [str(session_id)]
    for it in sorted(items, key=lambda i: i.id):
        lines.append(f"{it.blake3 or ''}:{it.file_size}:{it.phash or ''}:{it.file_name}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


async def _session_manifest_items(session_id: int) -> tuple[TransferSession, list[ManifestItemSchema]]:
    async with session_scope() as session:
        sess = await session.get(TransferSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        result = await session.execute(
            select(MediaItem)
            .where(
                MediaItem.session_id == session_id,
                MediaItem.final_status == "completed",
            )
            .order_by(MediaItem.id)
        )
        items = [
            ManifestItemSchema(
                id=mi.id,
                file_name=mi.file_name,
                file_size=mi.file_size,
                blake3=mi.source_hash,
                phash=mi.phash,
                date_taken=mi.date_taken,
            )
            for mi in result.scalars().all()
        ]
        name = sess.session_name
    return sess, items


@router.get("/sessions/{session_id}/manifest", response_model=SessionManifestResponse)
async def session_manifest(
    session_id: int,
    _: None = Depends(require_local_token),
) -> SessionManifestResponse:
    """Chain-of-custody manifest (MHL-lite) for a transfer session.

    Lists every verified item with its BLAKE3 + perceptual hash and a
    session chain hash. Hand the JSON to anyone to prove *what* was
    vaulted *when* — the OffShoot MHL workflow, democratized.
    Read-only: never touches the pipeline.
    """
    _, items = await _session_manifest_items(session_id)
    async with session_scope() as session:
        sess = await session.get(TransferSession, session_id)
        name = sess.session_name if sess else ""
    return SessionManifestResponse(
        session_id=session_id,
        session_name=name,
        item_count=len(items),
        total_bytes=sum(i.file_size for i in items),
        chain_hash=_manifest_chain(items, session_id),
        generated_at=datetime.now(UTC),
        items=items,
    )


@router.post("/sessions/{session_id}/verify", response_model=SessionVerifyResponse)
async def session_verify(
    session_id: int,
    _: None = Depends(require_local_token),
) -> SessionVerifyResponse:
    """Re-verify a session manifest: recompute the chain from live DB rows.

    Detects database tampering or record loss since the transfer ran.
    Read-only.
    """
    _, items = await _session_manifest_items(session_id)
    missing = sum(1 for i in items if not i.blake3)
    chain = _manifest_chain(items, session_id)
    ok = missing == 0
    return SessionVerifyResponse(
        session_id=session_id,
        item_count=len(items),
        chain_hash=chain,
        chain_valid=ok,
        missing_hashes=missing,
        message=(
            f"verified {len(items)} items, chain {chain[:12]}…"
            if ok
            else f"{missing} items lack cryptographic hashes — chain incomplete"
        ),
    )


@router.get("/stats", response_model=VaultStatsResponse)
async def vault_stats(_: None = Depends(require_local_token)) -> VaultStatsResponse:
    """One-glance vault economics: size, trash, and recoverable duplicates."""
    from backend.engines.perceptual_hash import group_near_duplicates, suggest_keeper

    async with session_scope() as session:
        total = (
            await session.execute(
                select(func.count(MediaItem.id), func.coalesce(func.sum(MediaItem.file_size), 0)).where(
                    MediaItem.final_status == "completed",
                    MediaItem.trashed == False,  # noqa: E712
                )
            )
        ).one()
        fav = (
            await session.execute(
                select(func.count(MediaItem.id)).where(
                    MediaItem.final_status == "completed",
                    MediaItem.trashed == False,  # noqa: E712
                    MediaItem.favorite == True,  # noqa: E712
                )
            )
        ).scalar() or 0
        trash = (
            await session.execute(
                select(func.count(MediaItem.id), func.coalesce(func.sum(MediaItem.file_size), 0)).where(
                    MediaItem.trashed == True  # noqa: E712
                )
            )
        ).one()
        shots = (
            await session.execute(
                select(func.count(MediaItem.id)).where(
                    MediaItem.final_status == "completed",
                    MediaItem.trashed == False,  # noqa: E712
                    MediaItem.tags_json.ilike("%screenshot%"),
                )
            )
        ).scalar() or 0
        phash_rows = (
            await session.execute(
                select(MediaItem.id, MediaItem.phash, MediaItem.file_size).where(
                    MediaItem.final_status == "completed",
                    MediaItem.trashed == False,  # noqa: E712
                    MediaItem.phash.is_not(None),
                )
            )
        ).all()
    dicts = [{"id": r[0], "phash": r[1], "file_size": r[2]} for r in phash_rows]
    groups = group_near_duplicates(dicts)
    recoverable = 0
    for g in groups:
        keeper = suggest_keeper(g)
        kid = (keeper or {}).get("id")
        recoverable += sum(m.get("file_size", 0) for m in g if m.get("id") != kid)
    return VaultStatsResponse(
        items=total[0] or 0,
        bytes=total[1] or 0,
        favorites=fav,
        trashed_items=trash[0] or 0,
        trashed_bytes=trash[1] or 0,
        near_duplicate_groups=len(groups),
        recoverable_bytes=recoverable,
        screenshots=shots,
    )


@router.get("/review-queue", response_model=ReviewQueueResponse)
async def review_queue(
    limit: int = Query(50, ge=1, le=500),
    _: None = Depends(require_local_token),
) -> ReviewQueueResponse:
    """Effortless culling queue (Photo Mechanic 'reject tag', democratized).

    Surfaces the blurriest shots, screenshots, and untagged stragglers for
    one-pass review. Read-only; culling happens via Trash resolve actions.
    """
    async with session_scope() as session:
        blurry = (
            (
                await session.execute(
                    select(MediaItem.id)
                    .where(
                        MediaItem.final_status == "completed",
                        MediaItem.trashed == False,  # noqa: E712
                        MediaItem.blur_score.is_not(None),
                    )
                    .order_by(MediaItem.blur_score.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        shots = (
            (
                await session.execute(
                    select(MediaItem.id)
                    .where(
                        MediaItem.final_status == "completed",
                        MediaItem.trashed == False,  # noqa: E712
                        MediaItem.tags_json.ilike("%screenshot%"),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        untagged = (
            await session.execute(
                select(func.count(MediaItem.id)).where(
                    MediaItem.final_status == "completed",
                    MediaItem.trashed == False,  # noqa: E712
                    (MediaItem.tags_json.is_(None)) | (MediaItem.tags_json == "[]"),
                )
            )
        ).scalar() or 0
    return ReviewQueueResponse(blurry_ids=list(blurry), screenshot_ids=list(shots), untagged_count=untagged)


# --- Media curation (favorites / trash) mounted here to avoid routes.py churn ---
curation_router = APIRouter(prefix="/api/media", tags=["media-curation"])


@curation_router.patch("/{item_id}", response_model=MediaItemInfo)
async def patch_media(
    item_id: int,
    req: MediaPatchRequest,
    _: None = Depends(require_local_token),
) -> MediaItemInfo:
    async with session_scope() as session:
        mi = await session.get(MediaItem, item_id)
        if mi is None:
            raise HTTPException(status_code=404, detail="Media item not found")
        if req.favorite is not None:
            mi.favorite = bool(req.favorite)
        if req.trashed is not None:
            mi.trashed = bool(req.trashed)
            mi.trashed_at = datetime.now(UTC) if req.trashed else None
        mi.touch()
        # Re-read for response while session is open
        info = _to_media_info(mi)
    return info


@curation_router.post("/{item_id}/restore", response_model=MediaItemInfo)
async def restore_media(item_id: int, _: None = Depends(require_local_token)) -> MediaItemInfo:
    async with session_scope() as session:
        mi = await session.get(MediaItem, item_id)
        if mi is None:
            raise HTTPException(status_code=404, detail="Media item not found")
        mi.trashed = False
        mi.trashed_at = None
        mi.touch()
        info = _to_media_info(mi)
    return info


trash_router = APIRouter(prefix="/api/trash", tags=["trash"])


@trash_router.get("", response_model=MediaList)
async def list_trash(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    _: None = Depends(require_local_token),
) -> MediaList:
    import math

    async with session_scope() as session:
        base = select(MediaItem).where(MediaItem.trashed == True)  # noqa: E712
        total = (await session.execute(select(func.count(MediaItem.id)).where(MediaItem.trashed == True))).scalar() or 0  # noqa: E712
        pages = math.ceil(total / page_size) if total else 1
        result = await session.execute(
            base.order_by(MediaItem.trashed_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
        items = [_to_media_info(mi) for mi in result.scalars().all()]
    return MediaList(items=items, total=total, page=page, page_size=page_size, pages=pages)


@trash_router.post("/empty")
async def empty_trash(_: None = Depends(require_local_token)) -> dict:
    async with session_scope() as session:
        result = await session.execute(select(MediaItem).where(MediaItem.trashed == True))  # noqa: E712
        items = list(result.scalars().all())
        n = 0
        for mi in items:
            await session.delete(mi)
            n += 1
    return {"emptied": n}
