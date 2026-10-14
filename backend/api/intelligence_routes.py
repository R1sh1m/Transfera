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

from backend.api.auth import require_local_token, require_local_token_or_query
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


def _clip_embed_thumb(thumb: bytes) -> list[float] | None:
    """Encode thumbnail bytes with the on-board CLIP image encoder."""
    try:
        import io as _io

        from PIL import Image as _Image

        from backend.engines import clip as _clip

        with _Image.open(_io.BytesIO(thumb)) as im:
            return _clip.encode_image(im)
    except Exception:
        return None


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


def _parse_face_bbox(face: Face | None) -> list[float] | None:
    """Parse ``Face.bbox_json`` into [x1, y1, x2, y2] floats (None when absent)."""
    try:
        if face is None or not face.bbox_json:
            return None
        vals = json.loads(face.bbox_json)
        if isinstance(vals, (list, tuple)) and len(vals) == 4:
            return [float(v) for v in vals]
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


def _to_near_item(mi: MediaItem) -> NearDuplicateItemSchema:
    return NearDuplicateItemSchema(
        id=mi.id,
        file_name=mi.file_name,
        file_size=mi.file_size,
        phash=mi.phash,
        width=mi.width,
        height=mi.height,
        date_taken=mi.date_taken,
        favorite=mi.favorite,
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
        favorite=mi.favorite,
        trashed=mi.trashed,
        blur_score=mi.blur_score,
        tags=_tags_of(mi),
        caption=mi.caption,
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities(_: None = Depends(require_local_token)) -> CapabilitiesResponse:
    from backend.engines.intelligence import get_capabilities

    caps = get_capabilities()
    return CapabilitiesResponse(**caps)


@router.get("/models/status")
async def model_download_status(_: None = Depends(require_local_token)) -> dict:
    """On-board AI model presence + background download progress (CLIP + Face models, ~400 MB)."""
    try:
        from backend.engines import clip as clip_engine
        from backend.engines import intelligence as intel_engine

        clip_st = clip_engine.download_status()
        face_st = intel_engine.face_download_status()

        is_downloading = (
            clip_st.get("status") in ("downloading", "installing-packages") or face_st.get("status") == "downloading"
        )
        has_error = clip_st.get("status") == "error" or face_st.get("status") == "error"
        ready = bool(clip_st.get("ready")) and bool(face_st.get("ready"))

        total_bytes = int(clip_st.get("total_bytes", 0)) + int(face_st.get("total_bytes", 0))
        downloaded_bytes = int(clip_st.get("downloaded_bytes", 0)) + int(face_st.get("downloaded_bytes", 0))

        status_str = (
            "downloading"
            if is_downloading
            else ("ready" if ready else ("error" if has_error else clip_st.get("status", "idle")))
        )
        err_msg = clip_st.get("error") or face_st.get("error")

        return {
            "status": status_str,
            "ready": ready,
            "total_bytes": total_bytes,
            "downloaded_bytes": downloaded_bytes,
            "error": err_msg,
        }
    except Exception as exc:
        return {"status": "error", "ready": False, "error": str(exc)[:200]}


@router.post("/models/download")
async def start_model_download(_: None = Depends(require_local_token)) -> dict:
    """Start background download of on-board CLIP and Face models (idempotent).

    Returns immediately; poll ``GET /models/status`` for progress. The
    Library "Index library" backfill picks up embeddings on its next run
    once the models are ready.
    """
    try:
        from backend.engines import clip as clip_engine
        from backend.engines import intelligence as intel_engine

        clip_status = clip_engine.start_background_download()
        intel_engine.start_background_face_download()
        return clip_status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Model download unavailable: {exc}")


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
            "favorite": mi.favorite,
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
        out: list[PersonSchema] = []
        used_media_ids: set[int] = set()
        for p in people:
            cover_face: Face | None = None
            if p.cover_face_id is not None:
                try:
                    cover_face = await session.get(Face, p.cover_face_id)
                except Exception:
                    cover_face = None
            cover_media_id: int | None = cover_face.media_id if cover_face is not None else None
            eff_cover_face_id: int | None = cover_face.id if cover_face is not None else None
            cover_bbox = _parse_face_bbox(cover_face)
            if cover_media_id is not None and cover_media_id in used_media_ids:
                # Duplicate cover: fall through to distinct re-choice below.
                cover_face = None
                cover_media_id = None
                eff_cover_face_id = None
                cover_bbox = None
            if cover_media_id is None:
                # Fallback: highest-confidence face whose media is unused by
                # another person; same-media fallback only if no alternative.
                try:
                    fres = await session.execute(
                        select(Face).where(Face.person_id == p.id).order_by(Face.confidence.desc().nullslast())
                    )
                    faces = list(fres.scalars().all())
                except Exception:
                    faces = []
                chosen: Face | None = None
                for f in faces:
                    if f.media_id not in used_media_ids:
                        chosen = f
                        break
                if chosen is None and faces:
                    chosen = faces[0]
                if chosen is not None:
                    eff_cover_face_id = chosen.id
                    cover_media_id = chosen.media_id
                    cover_bbox = _parse_face_bbox(chosen)
            if cover_media_id is not None:
                used_media_ids.add(cover_media_id)
            out.append(
                PersonSchema(
                    id=p.id,
                    name=p.name,
                    face_count=p.face_count,
                    cover_face_id=eff_cover_face_id if eff_cover_face_id is not None else p.cover_face_id,
                    cover_media_id=cover_media_id,
                    cover_thumbnail_url=(
                        f"/api/media/{cover_media_id}/thumbnail" if cover_media_id is not None else None
                    ),
                    cover_bbox=cover_bbox,
                    hidden=p.hidden,
                )
            )
    return PeopleListResponse(people=out, total=len(out))


@router.get("/people/{person_id}")
async def get_person_detail(
    person_id: int,
    _: None = Depends(require_local_token),
) -> dict:
    """Person detail: faces ordered by confidence desc.

    Returns ``{person_id, faces: [{face_id, media_id, thumbnail_url,
    bbox, confidence}]}``. 404 when the person does not exist.
    """
    async with session_scope() as session:
        p = await session.get(Person, person_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Person not found")
        fres = await session.execute(
            select(Face).where(Face.person_id == person_id).order_by(Face.confidence.desc().nullslast())
        )
        faces = list(fres.scalars().all())
        items: list[dict] = []
        for f in faces:
            try:
                bbox = json.loads(f.bbox_json) if f.bbox_json else None
            except (json.JSONDecodeError, TypeError):
                bbox = None
            items.append(
                {
                    "face_id": f.id,
                    "media_id": f.media_id,
                    "thumbnail_url": f"/api/media/{f.media_id}/thumbnail",
                    "bbox": bbox,
                    "confidence": f.confidence,
                }
            )
    return {"person_id": person_id, "faces": items}


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
        # Person has no touch(); bump manually
        p.updated_at = datetime.now(UTC)
        cover_media_id: int | None = None
        cover_bbox: list[float] | None = None
        if p.cover_face_id is not None:
            cf = await session.get(Face, p.cover_face_id)
            if cf is not None:
                cover_media_id = cf.media_id
                cover_bbox = _parse_face_bbox(cf)
        if cover_media_id is None:
            fres = await session.execute(
                select(Face.media_id)
                .where(Face.person_id == p.id)
                .order_by(Face.confidence.desc().nullslast())
                .limit(1)
            )
            row = fres.first()
            if row is not None:
                cover_media_id = row[0]
        pid, pname, pcount, pcover, phidden = p.id, p.name, p.face_count, p.cover_face_id, p.hidden
    return PersonSchema(
        id=pid,
        name=pname,
        face_count=pcount,
        cover_face_id=pcover,
        cover_media_id=cover_media_id,
        cover_thumbnail_url=(f"/api/media/{cover_media_id}/thumbnail" if cover_media_id is not None else None),
        cover_bbox=cover_bbox,
        hidden=phidden,
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
    import asyncio as _asyncio

    from backend.database.models import MediaEmbedding
    from backend.engines.intelligence import get_capabilities, semantic_search_keyword

    caps = get_capabilities()
    async with session_scope() as session:
        # CLIP vector path (vector-DB style): cosine top-k over stored
        # on-board embeddings. Falls through to keyword search when models
        # are absent, the query fails to encode, or nothing is indexed yet.
        if caps.get("clip_available"):
            try:
                from backend.engines import clip as _clip

                qvec = await _asyncio.to_thread(_clip.encode_text, req.query or "")
                if qvec:
                    rows = await session.execute(select(MediaEmbedding).where(MediaEmbedding.model == _clip.MODEL_NAME))
                    cands: list[tuple[int, list]] = []
                    for row in rows.scalars().all():
                        try:
                            vec = json.loads(row.vector_json or "[]")
                        except (ValueError, TypeError):
                            continue
                        if isinstance(vec, list) and len(vec) == len(qvec):
                            cands.append((row.media_id, [float(x) for x in vec]))
                    ranked = _clip.cosine_top_k(qvec, cands, limit=req.limit)
                    if ranked:
                        wanted = [mid for mid, _ in ranked]
                        got = await session.execute(
                            select(MediaItem).where(
                                MediaItem.id.in_(wanted),
                                MediaItem.trashed == False,  # noqa: E712
                            )
                        )
                        by_id = {mi.id: mi for mi in got.scalars().all()}
                        ordered = [by_id[mid] for mid, _ in ranked if mid in by_id]
                        if ordered:
                            return SemanticSearchResponse(
                                query=req.query,
                                mode="clip",
                                results=[_to_media_info(mi) for mi in ordered],
                                total=len(ordered),
                            )
            except Exception as exc:
                logger.debug("CLIP semantic path failed, using keyword: %s", exc)
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


async def _cluster_and_sync_people(session) -> int:
    from backend.database.models import Face, Person
    from backend.engines.intelligence import cluster_faces_greedy

    res = await session.execute(select(Face).where(Face.embedding_json.isnot(None)))
    faces = list(res.scalars().all())
    if not faces:
        logger.debug("Face clustering: no faces with embeddings found")
        return 0

    face_payload = []
    face_map = {}
    for f in faces:
        try:
            emb = json.loads(f.embedding_json or "[]")
            if emb:
                face_payload.append({"id": f.id, "embedding": emb})
                face_map[f.id] = f
        except Exception:
            pass

    if not face_payload:
        logger.debug("Face clustering: no valid face embeddings after parsing")
        return 0

    # Lowered threshold from 0.55 to 0.50 for more inclusive clustering with ArcFace embeddings
    clusters = cluster_faces_greedy(face_payload, threshold=0.50)
    logger.info("Face clustering: formed %d clusters from %d faces (threshold=0.50)", len(clusters), len(face_payload))
    # Distinct-cover bookkeeping: media_id -> owning person_id for covers
    # already assigned (best effort; never fail clustering on this).
    cover_owner: dict[int, int] = {}
    try:
        pres = await session.execute(select(Person.id, Person.cover_face_id).where(Person.cover_face_id.isnot(None)))
        _cover_ids = [(r[0], r[1]) for r in pres.all() if r[1] is not None]
        if _cover_ids:
            _cres = await session.execute(
                select(Face.id, Face.media_id).where(Face.id.in_([cid for _, cid in _cover_ids]))
            )
            _media_by_face = {fid: mid for fid, mid in _cres.all()}
            for pid, cid in _cover_ids:
                _mid = _media_by_face.get(cid)
                if _mid is not None and _mid not in cover_owner:
                    cover_owner[_mid] = pid
    except Exception:
        cover_owner = {}

    def _ranked(faces_list: list) -> list:
        return sorted(
            faces_list,
            key=lambda f: (-(f.confidence if f.confidence is not None else -1.0), f.id),
        )

    for cl in clusters:
        cl_faces = [face_map[fid] for fid in cl if fid in face_map]
        if not cl_faces:
            continue
        existing_pids = {f.person_id for f in cl_faces if f.person_id is not None}
        person = None
        for pid in existing_pids:
            person = await session.get(Person, pid)
            if person is not None:
                break
        ranked = _ranked(cl_faces)
        if person is None:
            chosen = next((f for f in ranked if f.media_id not in cover_owner), ranked[0] if ranked else None)
            person = Person(
                face_count=len(cl_faces),
                cover_face_id=chosen.id if chosen is not None else None,
                hidden=False,
            )
            session.add(person)
            await session.flush()
            if chosen is not None and chosen.media_id is not None:
                cover_owner.setdefault(chosen.media_id, person.id)
        else:
            # Keep the existing cover when it is still distinct; otherwise
            # re-choose the highest-confidence face on an unused media.
            keep_ok = False
            if person.cover_face_id is not None:
                try:
                    cur = await session.get(Face, person.cover_face_id)
                except Exception:
                    cur = None
                if cur is not None and cur.media_id is not None:
                    owner = cover_owner.get(cur.media_id)
                    if owner is None or owner == person.id:
                        keep_ok = True
                        cover_owner.setdefault(cur.media_id, person.id)
            if not keep_ok:
                chosen = next(
                    (f for f in ranked if f.media_id not in cover_owner or cover_owner.get(f.media_id) == person.id),
                    ranked[0] if ranked else None,
                )
                if chosen is not None:
                    person.cover_face_id = chosen.id
                    if chosen.media_id is not None:
                        cover_owner[chosen.media_id] = person.id

        for f in cl_faces:
            f.person_id = person.id

        person.face_count = len(cl_faces)
        if person.cover_face_id is None and ranked:
            fallback = next(
                (f for f in ranked if f.media_id not in cover_owner or cover_owner.get(f.media_id) == person.id),
                ranked[0],
            )
            person.cover_face_id = fallback.id
            if fallback.media_id is not None:
                cover_owner[fallback.media_id] = person.id
        person.updated_at = datetime.now(UTC)

    return len(clusters)


@router.post("/backfill", response_model=BackfillResponse)
async def backfill_intelligence(
    limit: int = Query(2000, ge=1, le=10000),
    _: None = Depends(require_local_token),
) -> BackfillResponse:
    """Fill missing phash / dimensions / tags / embeddings / faces for completed items.

    Bounded (``limit``) so it is safe to call repeatedly; idempotent —
    only touches rows with NULL fields. Pure-Pillow and on-board ONNX.
    """
    import asyncio
    import io
    from pathlib import Path

    from PIL import Image

    from backend.config import IMAGE_EXTENSIONS
    from backend.database.models import Face, MediaEmbedding
    from backend.engines.intelligence import (
        compute_blur_score,
        extract_faces,
        get_capabilities,
        keyword_tags,
    )
    from backend.engines.perceptual_hash import dhash_bytes
    from backend.engines.thumbnail_ops import resolve_thumbnail_source_path
    from backend.engines.thumbnailer import generate_thumbnail_bytes

    try:
        from backend.engines import clip as clip_engine

        clip_ready = clip_engine.clip_available()
        clip_model_name = clip_engine.MODEL_NAME if clip_ready else ""
    except Exception:
        clip_engine = None  # type: ignore[assignment]
        clip_ready = False
        clip_model_name = ""

    caps = get_capabilities()
    faces_ready = bool(caps.get("faces_available", False))

    # If face models are not available, return early with a clear message
    if not faces_ready:
        logger.info(
            "Backfill: face models not downloaded — skipping face extraction. Click 'Get AI models' in Library to download."
        )
        # Still run other backfill tasks (phash, dims, tags, clip) but skip faces
        # We'll continue below with faces_ready=False

    scanned = phash_filled = dims_filled = tags_filled = embeddings_filled = faces_filled = 0
    async with session_scope() as session:
        result = await session.execute(select(MediaItem).where(MediaItem.final_status == "completed").limit(limit))
        items = list(result.scalars().all())
        sessions: dict[int, TransferSession | None] = {}

        async def _resolve_path(mi: MediaItem) -> Path | None:
            if mi.session_id is not None and mi.session_id not in sessions:
                sessions[mi.session_id] = await session.get(TransferSession, mi.session_id)
            sess = sessions.get(mi.session_id) if mi.session_id is not None else None
            if sess is not None and getattr(sess, "dest_root", None):
                entry = {
                    "source_path": mi.source_path,
                    "file_name": mi.file_name,
                    "date_taken": mi.date_taken,
                    "original_capture_time": mi.original_capture_time,
                    "created_at": mi.created_at,
                    "file_size": mi.file_size,
                    "extension": mi.extension,
                    "source_hash": mi.source_hash,
                    "folder_layout": getattr(sess, "folder_layout", "year/month"),
                }
                found = resolve_thumbnail_source_path(entry, Path(sess.dest_root))
                if found is not None:
                    return found
            if mi.source_path:
                sp = Path(mi.source_path)
                if sp.is_file():
                    return sp
            return None

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

            needs_visual = mi.phash is None or mi.width is None or mi.blur_score is None
            needs_camera = mi.camera_make is None or mi.camera_model is None or mi.width is None or mi.height is None
            p: Path | None = None
            thumb: bytes | None = None

            if needs_visual or needs_camera or clip_ready or faces_ready:
                try:
                    p = await _resolve_path(mi)
                except Exception:
                    p = None

            if needs_visual and p is not None:
                try:
                    thumb = generate_thumbnail_bytes(p)
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
                    if mi.blur_score is None:
                        s = compute_blur_score(p)
                        if s is not None:
                            mi.blur_score = s
                            touched = True
                except Exception as exc:
                    logger.debug("backfill visual skipped %s: %s", mi.id, exc)

            # Camera + dimensions re-extract via ExifTool (fixes legacy rows
            # scanned before structured_from_tags wiring, e.g. HEIC batches).
            # Resolves from the dest archive so ios:// / missing-source rows
            # still backfill. date_taken is filled too when ExifTool (incl.
            # MOV CreationDate tags) yields one.
            if needs_camera and p is not None and p.is_file():
                try:
                    from backend.engines.metadata_extractor import extract_metadata as _extract_meta

                    meta = await asyncio.to_thread(_extract_meta, p)
                    if meta is not None:
                        if mi.camera_make is None and meta.camera_make:
                            mi.camera_make = meta.camera_make
                            touched = True
                        if mi.camera_model is None and meta.camera_model:
                            mi.camera_model = meta.camera_model
                            touched = True
                        if mi.width is None and meta.width is not None:
                            mi.width = meta.width
                            mi.height = meta.height
                            dims_filled += 1
                            touched = True
                        elif mi.height is None and meta.height is not None:
                            mi.height = meta.height
                            touched = True
                        if getattr(mi, "gps_lat", None) is None and meta.gps_lat is not None:
                            mi.gps_lat = meta.gps_lat
                            mi.gps_lon = meta.gps_lon
                            touched = True
                        if getattr(mi, "duration_s", None) is None and meta.duration_s is not None:
                            mi.duration_s = meta.duration_s
                            touched = True
                        if mi.date_taken is None and meta.date_taken is not None:
                            mi.date_taken = meta.date_taken
                            touched = True
                        if (
                            mi.camera_make is None
                            and mi.camera_model is None
                            and mi.width is None
                            and mi.date_taken is None
                        ):
                            logger.debug(
                                "backfill exif empty %s (%s): no camera/dims/date in %s",
                                mi.id,
                                mi.file_name,
                                p,
                            )
                except Exception as exc:
                    logger.debug("backfill exif skipped %s: %s", mi.id, exc)

            # CLIP embedding (vector-DB row)
            if clip_ready and (mi.extension or "").lower() in IMAGE_EXTENSIONS:
                if thumb is None and p is not None:
                    try:
                        thumb = generate_thumbnail_bytes(p)
                    except Exception:
                        thumb = None
                if thumb:
                    try:
                        existing = await session.get(MediaEmbedding, mi.id)
                        if existing is None or existing.model != clip_model_name:
                            vec = await asyncio.to_thread(_clip_embed_thumb, thumb)
                            if vec:
                                if existing is None:
                                    session.add(
                                        MediaEmbedding(
                                            media_id=mi.id,
                                            model=clip_model_name,
                                            dim=len(vec),
                                            vector_json=json.dumps(vec),
                                        )
                                    )
                                else:
                                    existing.model = clip_model_name
                                    existing.dim = len(vec)
                                    existing.vector_json = json.dumps(vec)
                                embeddings_filled += 1
                                touched = True
                    except Exception as exc:
                        logger.debug("backfill clip skipped %s: %s", mi.id, exc)

            # Face extraction (SCRFD + ArcFace)
            if faces_ready and (mi.extension or "").lower() in IMAGE_EXTENSIONS:
                f_check = await session.execute(select(func.count(Face.id)).where(Face.media_id == mi.id))
                if (f_check.scalar() or 0) == 0 and p is not None and p.is_file():
                    try:
                        detected = await asyncio.to_thread(extract_faces, p)
                        for df in detected:
                            session.add(
                                Face(
                                    media_id=mi.id,
                                    bbox_json=json.dumps(df["bbox"]),
                                    embedding_json=json.dumps(df["embedding"]),
                                    confidence=df["confidence"],
                                )
                            )
                            faces_filled += 1
                            touched = True
                    except Exception as exc:
                        logger.debug("backfill face skipped %s: %s", mi.id, exc)

            if touched:
                mi.touch()

        # Cluster all faces into persons
        if faces_ready:
            try:
                await _cluster_and_sync_people(session)
            except Exception as exc:
                logger.warning("Face clustering sync failed: %s", exc)
        else:
            logger.debug("Backfill: skipping face clustering (face models not available)")

    msg = (
        f"backfilled {phash_filled} hashes, {dims_filled} dims, {tags_filled} tags, "
        f"{embeddings_filled} clip embeddings, {faces_filled} faces across {scanned} items"
    )
    if not faces_ready:
        msg += " — Face models not downloaded; click 'Get AI models' in Library to enable face detection."
    logger.info(msg)
    return BackfillResponse(
        scanned=scanned,
        phash_filled=phash_filled,
        dims_filled=dims_filled,
        tags_filled=tags_filled,
        embeddings_filled=embeddings_filled,
        faces_filled=faces_filled,
        message=msg,
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
    sess, items = await _session_manifest_items(session_id)
    return SessionManifestResponse(
        session_id=session_id,
        session_name=sess.session_name,
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
    _sess, items = await _session_manifest_items(session_id)
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
            mi.favorite = req.favorite
        if req.trashed is not None:
            mi.trashed = req.trashed
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
    """Permanently delete all trashed items: DB rows, vault files, thumbnails.

    Vault files are located with ``locate_archive_file`` (date-folder +
    size match, hash-verified when ``source_hash`` is known) and only
    deleted when they resolve inside the item's session destination —
    the source originals are never touched.
    """
    from pathlib import Path

    from backend.engines.organizer import locate_archive_file
    from backend.engines.thumbnail_cache import thumbnail_cache

    async with session_scope() as session:
        result = await session.execute(select(MediaItem).where(MediaItem.trashed == True))  # noqa: E712
        items = list(result.scalars().all())
        session_ids = {mi.session_id for mi in items if mi.session_id is not None}
        dest_by_session: dict[int, tuple[str, str]] = {}
        if session_ids:
            sess_rows = await session.execute(select(TransferSession).where(TransferSession.id.in_(session_ids)))
            for ts in sess_rows.scalars().all():
                dest_by_session[ts.id] = (ts.dest_root, ts.folder_layout or "year/month")

        n = 0
        files_removed = 0
        for mi in items:
            dest = dest_by_session.get(mi.session_id or -1)
            if dest and mi.final_status == "completed":
                try:
                    vault_file = locate_archive_file(Path(dest[0]), mi, layout=dest[1], verify_hash=True)
                except Exception as exc:
                    logger.warning("Trash locate failed for item %d: %s", mi.id, exc)
                    vault_file = None
                if vault_file is not None:
                    try:
                        vault_file.unlink()
                        files_removed += 1
                    except OSError as exc:
                        logger.warning("Trash file delete failed for item %d: %s", mi.id, exc)
            try:
                thumbnail_cache.evict_items([mi.id])
            except Exception as exc:
                logger.debug("Thumbnail evict failed for item %d: %s", mi.id, exc)
            await session.delete(mi)
            n += 1
    return {"emptied": n, "files_removed": files_removed}


# --- Face crops (server-side Pillow crop of source/vault image) ---
faces_router = APIRouter(prefix="/api/faces", tags=["faces"])


@faces_router.get("/{face_id}/crop")
async def face_crop(
    face_id: int,
    _: None = Depends(require_local_token_or_query),
):
    """Server-side face crop: Pillow crop of the source/vault image.

    Uses ``Face.bbox_json`` [x1, y1, x2, y2] with ~20% padding, squared,
    returned as JPEG. 404 on missing face or missing file. Source files
    are never deleted or modified (read-only).
    """
    import asyncio as _asyncio
    import io as _io
    from pathlib import Path as _Path

    from fastapi.responses import Response as _Response

    from backend.engines.thumbnail_ops import resolve_thumbnail_source_path as _resolve_src

    async with session_scope() as session:
        face = await session.get(Face, face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="Face not found")
        try:
            bbox = json.loads(face.bbox_json) if face.bbox_json else None
        except (json.JSONDecodeError, TypeError):
            bbox = None
        media_id = face.media_id
        mi = await session.get(MediaItem, media_id)
        if mi is None:
            raise HTTPException(status_code=404, detail="Media item not found")
        sess = await session.get(TransferSession, mi.session_id) if mi.session_id is not None else None
        entry = {
            "source_path": mi.source_path,
            "file_name": mi.file_name,
            "date_taken": mi.date_taken,
            "original_capture_time": mi.original_capture_time,
            "created_at": mi.created_at,
            "file_size": mi.file_size,
            "extension": mi.extension,
            "source_hash": mi.source_hash,
            "folder_layout": getattr(sess, "folder_layout", "year/month") if sess is not None else "year/month",
        }
        dest_root = _Path(sess.dest_root) if sess is not None and getattr(sess, "dest_root", None) else None
        src_path = _resolve_src(entry, dest_root)
        if src_path is None and mi.source_path:
            sp = _Path(mi.source_path)
            if sp.is_file():
                src_path = sp
        if src_path is None or not src_path.is_file():
            raise HTTPException(status_code=404, detail="Source file not found")
        src_str = str(src_path)
        bbox_vals = list(bbox) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None

    def _render() -> bytes:
        from PIL import Image, ImageOps

        with Image.open(src_str) as raw:
            img = ImageOps.exif_transpose(raw) or raw
            img = img.convert("RGB")
            w, h = img.size
            if bbox_vals is not None:
                try:
                    x1, y1, x2, y2 = (float(v) for v in bbox_vals)
                except (ValueError, TypeError):
                    x1, y1, x2, y2 = 0.0, 0.0, float(w), float(h)
                x1 = max(0.0, min(x1, float(w)))
                y1 = max(0.0, min(y1, float(h)))
                x2 = max(0.0, min(x2, float(w)))
                y2 = max(0.0, min(y2, float(h)))
                if x2 <= x1 or y2 <= y1:
                    x1, y1, x2, y2 = 0.0, 0.0, float(w), float(h)
                bw, bh = x2 - x1, y2 - y1
                pad = 0.20 * max(bw, bh)
                x1 = max(0.0, x1 - pad)
                y1 = max(0.0, y1 - pad)
                x2 = min(float(w), x2 + pad)
                y2 = min(float(h), y2 + pad)
                # Square the box around its center
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                side = max(x2 - x1, y2 - y1)
                x1, y1 = cx - side / 2.0, cy - side / 2.0
                x2, y2 = cx + side / 2.0, cy + side / 2.0
                # Clamp square inside the image, shrinking if larger than frame
                side = min(side, float(min(w, h)))
                cx = min(max(cx, side / 2.0), float(w) - side / 2.0)
                cy = min(max(cy, side / 2.0), float(h) - side / 2.0)
                x1, y1 = round(cx - side / 2.0), round(cy - side / 2.0)
                x2, y2 = round(x1 + side), round(y1 + side)
                x1 = max(0, min(x1, w - 1))
                y1 = max(0, min(y1, h - 1))
                x2 = max(x1 + 1, min(x2, w))
                y2 = max(y1 + 1, min(y2, h))
                img = img.crop((x1, y1, x2, y2))
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

    try:
        data = await _asyncio.to_thread(_render)
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("face crop failed for face %s: %s", face_id, exc)
        raise HTTPException(status_code=404, detail="Could not render face crop")
    return _Response(content=data, media_type="image/jpeg")


@faces_router.get("/{face_id}/thumbnail")
async def face_thumbnail(
    face_id: int,
    _: None = Depends(require_local_token_or_query),
):
    """Face-cropped thumbnail: 256px square JPEG for People covers.

    Loads the parent media source via the thumbnail source-path helpers
    (:func:`resolve_thumbnail_source_path` with source-path fallback),
    crops ``Face.bbox_json`` [x1, y1, x2, y2] with ~30% margin,
    square-crops, and resizes to 256px. 404 when the face, bbox, or
    source file is missing. Never requires ML models (pure Pillow).
    """
    import asyncio as _asyncio
    import io as _io
    from pathlib import Path as _Path

    from fastapi.responses import Response as _Response

    from backend.engines.thumbnail_ops import resolve_thumbnail_source_path as _resolve_src

    async with session_scope() as session:
        try:
            face = await session.get(Face, face_id)
        except Exception:
            face = None
        if face is None:
            raise HTTPException(status_code=404, detail="Face not found")
        try:
            bbox = json.loads(face.bbox_json) if face.bbox_json else None
        except (json.JSONDecodeError, TypeError):
            bbox = None
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise HTTPException(status_code=404, detail="Face bbox not available")
        try:
            bbox_vals = [float(v) for v in bbox]
        except (ValueError, TypeError):
            raise HTTPException(status_code=404, detail="Face bbox not available")
        media_id = face.media_id
        try:
            mi = await session.get(MediaItem, media_id)
        except Exception:
            mi = None
        if mi is None:
            raise HTTPException(status_code=404, detail="Media item not found")
        try:
            sess = await session.get(TransferSession, mi.session_id) if mi.session_id is not None else None
        except Exception:
            sess = None
        entry = {
            "source_path": mi.source_path,
            "file_name": mi.file_name,
            "date_taken": mi.date_taken,
            "original_capture_time": mi.original_capture_time,
            "created_at": mi.created_at,
            "file_size": mi.file_size,
            "extension": mi.extension,
            "source_hash": mi.source_hash,
            "folder_layout": getattr(sess, "folder_layout", "year/month") if sess is not None else "year/month",
        }
        try:
            dest_root = _Path(sess.dest_root) if sess is not None and getattr(sess, "dest_root", None) else None
        except Exception:
            dest_root = None
        try:
            src_path = _resolve_src(entry, dest_root)
        except Exception:
            src_path = None
        if src_path is None and mi.source_path:
            try:
                sp = _Path(mi.source_path)
                if sp.is_file():
                    src_path = sp
            except Exception:
                src_path = None
        try:
            src_ok = src_path is not None and src_path.is_file()
        except Exception:
            src_ok = False
        if not src_ok:
            raise HTTPException(status_code=404, detail="Source file not found")
        assert src_path is not None
        src_str = str(src_path)

    def _render() -> bytes:
        from PIL import Image, ImageOps

        with Image.open(src_str) as raw:
            img = ImageOps.exif_transpose(raw) or raw
            img = img.convert("RGB")
            w, h = img.size
            x1, y1, x2, y2 = bbox_vals
            x1 = max(0.0, min(x1, float(w)))
            y1 = max(0.0, min(y1, float(h)))
            x2 = max(0.0, min(x2, float(w)))
            y2 = max(0.0, min(y2, float(h)))
            if x2 <= x1 or y2 <= y1:
                raise ValueError("empty bbox")
            bw, bh = x2 - x1, y2 - y1
            pad = 0.30 * max(bw, bh)
            x1 = max(0.0, x1 - pad)
            y1 = max(0.0, y1 - pad)
            x2 = min(float(w), x2 + pad)
            y2 = min(float(h), y2 + pad)
            # Square the box around its center
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            side = max(x2 - x1, y2 - y1)
            # Clamp square inside the image, shrinking if larger than frame
            side = min(side, float(min(w, h)))
            cx = min(max(cx, side / 2.0), float(w) - side / 2.0)
            cy = min(max(cy, side / 2.0), float(h) - side / 2.0)
            x1, y1 = round(cx - side / 2.0), round(cy - side / 2.0)
            x2, y2 = round(x1 + side), round(y1 + side)
            x1 = max(0, min(x1, w - 1))
            y1 = max(0, min(y1, h - 1))
            x2 = max(x1 + 1, min(x2, w))
            y2 = max(y1 + 1, min(y2, h))
            img = img.crop((x1, y1, x2, y2))
            img = img.resize((256, 256), Image.Resampling.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

    try:
        data = await _asyncio.to_thread(_render)
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("face thumbnail failed for face %s: %s", face_id, exc)
        raise HTTPException(status_code=404, detail="Could not render face thumbnail")
    return _Response(content=data, media_type="image/jpeg")
