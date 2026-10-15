// ---------------------------------------------------------------------------
// Transfera v2 — Intelligence Panels
// Timeline / Moments / Duplicates / People / Semantic search.
// Apple tokens: single Action Blue accent (bg-action), pill CTAs
// (rounded-pill), press state active:scale-[0.95], 17px body pace.
// ---------------------------------------------------------------------------

import { useEffect, useRef, useState } from "react";
import { useQueryClient, useIsMutating } from "@tanstack/react-query";
import {
  CalendarDays,
  Sparkles,
  Copy,
  Users,
  Search,
  Star,
  Trash2,
  Check,
  Loader2,
  ScanSearch,
} from "lucide-react";
import { cn, parseBackendDate } from "@/lib/utils";
import {
  useBackfill,
  useCapabilities,
  useDownloadModels,
  useDuplicateGroups,
  useMediaItem,
  useModelStatus,
  useMoments,
  usePeople,
  usePersonFaces,
  useRenamePerson,
  useResolveDuplicateGroup,
  useSemanticSearch,
  useTimeline,
} from "@/lib/queries";
import { fetchThumbnail, revokeThumbnail } from "@/lib/thumbnail-fetch";
import apiClient, { API_BASE_URL, getLocalToken } from "@/lib/api-client";
import { useTransferStore } from "@/store/transfer";
import MediaDetailModal from "@/components/MediaDetailModal";
import {
  useBackendReady,
  ENGINE_WAITING_LABEL,
} from "@/hooks/use-backend-ready";
import type { MediaItemInfo, Moment, Person } from "@/types/api";

function Thumb({
  id,
  url,
  label,
}: {
  id: number;
  url?: string | null;
  label: string;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  // Lazy single fetch (fetchThumbnail already returns an object URL string).
  // Must run in an effect — fetching during render re-triggers every render.
  useEffect(() => {
    let cancelled = false;
    setSrc(null);
    setFailed(false);
    fetchThumbnail(id, url ?? undefined).then((s) => {
      if (cancelled) return;
      if (s) setSrc(s);
      else setFailed(true);
    });
    return () => {
      cancelled = true;
      revokeThumbnail(id);
    };
  }, [id, url]);
  if (failed || !src)
    return (
      <div className="aspect-square rounded-lg bg-muted flex items-center justify-center text-[10px] text-muted-foreground px-1 text-center">
        {label}
      </div>
    );
  return (
    <img
      src={src}
      alt={label}
      className="aspect-square rounded-lg object-cover w-full"
      loading="lazy"
    />
  );
}

/** Face-crop thumbnail for a person cover.
 *  Tries GET /api/faces/{face_id}/thumbnail first (backend lane); on any
 *  error degrades gracefully to the full-image Thumb, then initials.
 *  cover_bbox is accepted for forward-compat and ignored until the backend
 *  serves cropped covers (graceful degradation per contract). */
export function FaceThumb({
  faceId,
  mediaId,
  mediaUrl,
  bbox,
  label,
  className,
}: {
  faceId?: number | null;
  mediaId?: number | null;
  mediaUrl?: string | null;
  bbox?: number[] | null;
  label: string;
  className?: string;
}) {
  void bbox;
  const [faceFailed, setFaceFailed] = useState(false);
  useEffect(() => {
    setFaceFailed(false);
  }, [faceId]);
  const initials = label
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  if (faceId == null || faceFailed) {
    if (mediaId != null) {
      return <Thumb id={mediaId} url={mediaUrl} label={label} />;
    }
    return (
      <div
        className={
          className ??
          "aspect-square rounded-lg bg-muted flex items-center justify-center text-lg font-semibold text-muted-foreground"
        }
      >
        {initials || label}
      </div>
    );
  }
  return (
    <img
      src={`${API_BASE_URL}/api/faces/${faceId}/thumbnail${(() => {
        const token = getLocalToken();
        return token ? `?token=${encodeURIComponent(token)}` : "";
      })()}`}
      alt={label}
      loading="lazy"
      onError={() => setFaceFailed(true)}
      className={className ?? "aspect-square object-cover rounded-lg w-full"}
    />
  );
}

export function CapabilitiesBadge() {
  const { data } = useCapabilities();
  const backfill = useBackfill();
  // Global in-flight state (shared mutationKey ["backfill"]): disables this
  // button together with PeoplePanel's while indexing runs anywhere.
  const backfillMutations = useIsMutating({ mutationKey: ["backfill"] });
  const isIndexing = backfillMutations > 0 || backfill.isPending;
  const downloadModels = useDownloadModels();
  const backendReady = useBackendReady();
  const { data: modelStatus } = useModelStatus(false);
  const busy =
    modelStatus?.status === "downloading" ||
    modelStatus?.status === "installing-packages";
  const downloading = busy;
  const clipReady = data?.semantic_mode === "clip";
  const facesAvailable = data?.faces_available ?? false;
  const [justFinished, setJustFinished] = useState(false);
  const [backfillProgress, setBackfillProgress] = useState<string | null>(null);
  useEffect(() => {
    if (clipReady) setJustFinished(false);
  }, [clipReady]);
  const mb = modelStatus
    ? (modelStatus.downloaded_bytes / 1e6).toFixed(0)
    : "0";
  // Inverted progress: the Action Blue wash fills the pill left-to-right
  // as bytes land (polled every 2s while downloading). Track stays neutral
  // so the label keeps its muted tone — single accent, no extra colors.
  const totalBytes = modelStatus?.total_bytes ?? 0;
  const downloadedBytes = modelStatus?.downloaded_bytes ?? 0;
  const fillPct =
    totalBytes > 0 ? Math.min(100, (downloadedBytes / totalBytes) * 100) : 0;
  if (!data) return null;
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
      <span
        className={cn(
          "px-2.5 py-0.5 rounded-pill border border-border text-xs",
          clipReady
            ? "text-green-600 border-green-300 dark:border-green-800"
            : "text-muted-foreground",
        )}
        title={
          clipReady
            ? `On-device smart search active (search by scene, object, or description locally) • ${
                facesAvailable
                  ? "Face recognition enabled"
                  : "Face recognition disabled"
              }`
            : "Standard keyword search active (searches by filename and date)"
        }
      >
        {clipReady ? "Smart search (on-device)" : "Keyword search"} ·{" "}
        {facesAvailable ? "Face recognition" : "Face recognition off"}
      </span>
      {justFinished && !clipReady && (
        <span
          className="px-3 py-1 rounded-pill border border-border text-xs text-muted-foreground opacity-60 cursor-default"
          title="Models ready — enabling semantic search…"
        >
          ✓ AI search ready
        </span>
      )}
      {downloading && (
        <span
          className="relative px-2 py-0.5 rounded-pill border border-border overflow-hidden"
          title="Installing AI libraries and downloading on-board AI models in the background"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(fillPct)}
          aria-label={
            modelStatus?.status === "installing-packages"
              ? "Installing AI libraries"
              : `Downloading AI models, ${mb} megabytes`
          }
        >
          <span
            aria-hidden
            className="absolute inset-y-0 left-0 bg-action/30 transition-[width] duration-300"
            style={{ width: `${fillPct}%` }}
          />
          <span className="relative">
            {modelStatus?.status === "installing-packages"
              ? "Installing AI libraries…"
              : `Downloading AI… ${mb} MB`}
          </span>
        </span>
      )}
      {modelStatus?.status === "error" && (
        <span className="flex items-center gap-2">
          <span
            className="px-2 py-0.5 rounded-pill border border-border text-xs text-muted-foreground max-w-64 truncate"
            title={modelStatus.error ?? "AI model download failed"}
          >
            {modelStatus.error ?? "AI model download failed"}
          </span>
          <button
            onClick={() => downloadModels.mutate()}
            disabled={downloadModels.isPending || !backendReady}
            className="px-3 py-1 rounded-pill border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-muted active:scale-[0.95] disabled:opacity-40"
            title={
              backendReady
                ? "Retry downloading on-board AI models"
                : ENGINE_WAITING_LABEL
            }
          >
            {downloadModels.isPending ? "Starting…" : "Retry"}
          </button>
        </span>
      )}
      <button
        onClick={() => {
          backfill.mutate(undefined, {
            onSuccess: (response) => {
              // Show progress breakdown from response
              const parts: string[] = [];
              if (response.phash_filled !== undefined)
                parts.push(`hashes: ${response.phash_filled}`);
              if (response.dims_filled !== undefined)
                parts.push(`dims: ${response.dims_filled}`);
              if (response.tags_filled !== undefined)
                parts.push(`tags: ${response.tags_filled}`);
              if (response.faces_filled !== undefined)
                parts.push(`faces: ${response.faces_filled}`);
              if (parts.length > 0) {
                setBackfillProgress(parts.join(", "));
                // Clear after 5 seconds
                setTimeout(() => setBackfillProgress(null), 5000);
              }
              // Show face clustering toast if faces were filled
              if (response.faces_filled && response.faces_filled > 0) {
                useTransferStore
                  .getState()
                  .showNotification(
                    "success",
                    `Face clustering complete — ${response.faces_filled} people found`,
                  );
              }
            },
          });
        }}
        disabled={isIndexing || !backendReady}
        className="px-3 py-1 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] disabled:opacity-40"
        title={
          backendReady
            ? "Fill missing perceptual hashes, dimensions and tags (offline, idempotent)"
            : ENGINE_WAITING_LABEL
        }
      >
        {isIndexing ? "Indexing…" : "Index library"}
      </button>
      {backfillProgress && (
        <span className="px-2 py-0.5 rounded-pill border border-border text-xs text-muted-foreground bg-muted/50">
          {backfillProgress}
        </span>
      )}
      {!clipReady && !downloading && (
        <button
          onClick={() => downloadModels.mutate()}
          disabled={downloadModels.isPending || !backendReady}
          className="px-3 py-1 rounded-pill border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-muted active:scale-[0.95] disabled:opacity-40"
          title={
            backendReady
              ? "Download on-board AI models (~218 MB, one time) to enable semantic search, auto-tags and face grouping"
              : ENGINE_WAITING_LABEL
          }
        >
          {downloadModels.isPending ? "Starting…" : "Download AI models"}
        </button>
      )}
      {!facesAvailable && !downloading && (
        <span className="px-2 py-0.5 rounded-pill border border-border text-xs text-amber-600 dark:text-amber-400 border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30">
          Face models not installed — click Get AI models
        </span>
      )}
    </div>
  );
}

export function SemanticSearchBar({
  onResults,
  value,
  onChange,
}: {
  onResults?: (q: string) => void;
  /** Controlled mode: LibraryPage owns the single unified query state. */
  value?: string;
  onChange?: (q: string) => void;
}) {
  const [inner, setInner] = useState("");
  const q = value ?? inner;
  const setQ = (v: string) => {
    if (onChange) onChange(v);
    else setInner(v);
    onResults?.(v);
  };
  const { data, isFetching } = useSemanticSearch(q);
  return (
    <div className="flex items-center gap-2">
      <div className="relative flex-1">
        <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder='Try "screenshot", "canon", "sunset"…'
          className="w-full h-11 rounded-pill border border-border bg-card pl-9 pr-4 text-[17px] placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-action"
        />
      </div>
      {isFetching && (
        <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
      )}
      {data && q.trim().length > 1 && (
        <span className="text-xs text-muted-foreground whitespace-nowrap">
          {data.total} hits · {data.mode}
        </span>
      )}
    </div>
  );
}

export function TimelineStrip() {
  const { data, isLoading } = useTimeline("month");
  if (isLoading)
    return (
      <div className="text-sm text-muted-foreground">Loading timeline…</div>
    );
  if (!data || data.buckets.length === 0)
    return (
      <div className="text-sm text-muted-foreground">
        No dated media yet — import to build your timeline.
      </div>
    );
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <CalendarDays className="w-4 h-4 text-action" /> Timeline · {data.total}{" "}
        items
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2">
        {data.buckets.map((b) => (
          <div
            key={b.key}
            className="rounded-lg border border-border bg-card p-2"
          >
            <div className="text-xs font-semibold">{b.key}</div>
            <div className="text-xs text-muted-foreground">{b.count} items</div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Window event fired when a moment is selected without an onSelect handler,
 *  so the library view can filter to that date range without prop drilling.
 *  Subscribe: window.addEventListener(MOMENT_SELECT_EVENT, (e) => ...). */
export const MOMENT_SELECT_EVENT = "transfera:moment-select";

export function MomentsGrid({
  onSelect,
}: {
  onSelect?: (moment: Moment) => void;
}) {
  const { data, isLoading } = useMoments();
  if (isLoading)
    return (
      <div className="text-sm text-muted-foreground">Finding moments…</div>
    );
  if (!data || data.moments.length === 0)
    return (
      <div className="text-sm text-muted-foreground">
        Moments appear after your first imports.
      </div>
    );
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Sparkles className="w-4 h-4 text-action" /> Moments · {data.total}{" "}
        events
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
        {data.moments.slice(0, 9).map((m, i) => {
          const label = `${parseBackendDate(m.start).toLocaleDateString()} → ${parseBackendDate(m.end).toLocaleDateString()}`;
          const handleSelect = () => {
            if (onSelect) {
              onSelect(m);
              return;
            }
            // Null-safe default: broadcast the range so the library view can
            // set its search/filter to that date range (LibraryPage owns its
            // filter state, so this stays decoupled until it subscribes).
            window.dispatchEvent(
              new CustomEvent<Moment>(MOMENT_SELECT_EVENT, { detail: m }),
            );
          };
          return (
            <button
              key={i}
              type="button"
              onClick={handleSelect}
              aria-label={`Show ${m.count} photos from ${label}`}
              title={`Filter library to ${label}`}
              className="rounded-lg border border-border bg-card p-3 text-left hover:border-action hover:bg-action/5 active:scale-[0.98] transition-all cursor-pointer"
            >
              <div className="text-sm font-semibold">{label}</div>
              <div className="text-xs text-muted-foreground">
                {m.count} photos · event {i + 1}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function DuplicatesPanel() {
  const { data, isLoading } = useDuplicateGroups(8);
  const resolve = useResolveDuplicateGroup();
  const backendReady = useBackendReady();
  const [kept, setKept] = useState<Record<number, number>>({});
  if (isLoading)
    return (
      <div className="text-sm text-muted-foreground">
        Scanning for near-duplicates…
      </div>
    );
  if (!data || data.groups.length === 0)
    return (
      <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        <div className="flex items-center gap-2 font-semibold text-foreground">
          <Copy className="w-4 h-4 text-action" /> Duplicate Center
        </div>
        No near-duplicate groups found. Exact duplicates are still caught during
        transfer; run “Index library” above to fill perceptual hashes for older
        items.
      </div>
    );
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Copy className="w-4 h-4 text-action" /> Duplicate Center ·{" "}
        {data.total_groups} groups
      </div>
      {data.groups.slice(0, 12).map((g, gi) => {
        const keepId: number =
          kept[gi] ?? g.suggested_keeper_id ?? g.members[0]?.id ?? 0;
        if (!keepId) return null;
        return (
          <div
            key={gi}
            className="rounded-lg border border-border bg-card p-3 space-y-2"
          >
            <div className="text-xs text-muted-foreground">
              Group {gi + 1} · {g.members.length} similar · suggested keeper #
              {g.suggested_keeper_id}
            </div>
            <div className="grid grid-cols-3 sm:grid-cols-5 gap-2">
              {g.members.map((m) => (
                <button
                  key={m.id}
                  onClick={() => setKept((k) => ({ ...k, [gi]: m.id }))}
                  className={cn(
                    "rounded-lg border p-1 text-left active:scale-[0.95] transition",
                    keepId === m.id
                      ? "border-action ring-2 ring-action/30"
                      : "border-border",
                  )}
                  title={m.file_name}
                >
                  <Thumb id={m.id} url={m.thumbnail_url} label={m.file_name} />
                  <div className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground truncate">
                    {keepId === m.id && (
                      <Star className="w-3 h-3 text-action" />
                    )}
                    <span className="truncate">#{m.id}</span>
                  </div>
                </button>
              ))}
            </div>
            <button
              disabled={resolve.isPending || !backendReady}
              onClick={() =>
                resolve.mutate({
                  keep_id: keepId,
                  trash_ids: g.members
                    .map((m) => m.id)
                    .filter((id) => id !== keepId),
                })
              }
              title={backendReady ? undefined : ENGINE_WAITING_LABEL}
              className="px-3 py-1.5 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] disabled:opacity-40 flex items-center gap-1"
            >
              <Trash2 className="w-3 h-3" /> Keep #{keepId}, move rest to Trash
            </button>
          </div>
        );
      })}
    </div>
  );
}

/** Window event fired when a person is selected without an onSelect handler,
 *  so the library view can filter to that person's photos without prop
 *  drilling. Mirrors MOMENT_SELECT_EVENT.
 *  Subscribe: window.addEventListener(PERSON_SELECT_EVENT, (e) => ...). */
export const PERSON_SELECT_EVENT = "transfera:person-select";

export interface PersonSelectDetail {
  person_id: number;
  media_ids: number[];
}

export function PeoplePanel({
  onSelect,
}: {
  onSelect?: (detail: PersonSelectDetail) => void;
}) {
  const { data, isLoading } = usePeople();
  const rename = useRenamePerson();
  const backfill = useBackfill();
  // Same shared indexing state as CapabilitiesBadge — both buttons disable
  // together while a backfill runs from either surface.
  const backfillMutations = useIsMutating({ mutationKey: ["backfill"] });
  const isIndexing = backfillMutations > 0 || backfill.isPending;
  const downloadModels = useDownloadModels();
  const backendReady = useBackendReady();
  const { data: modelStatus } = useModelStatus(downloadModels.isPending);
  const { data: capabilities } = useCapabilities();
  const [editing, setEditing] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [navigating, setNavigating] = useState<number | null>(null);
  const facesAvailable = capabilities?.faces_available ?? false;
  const qc = useQueryClient();
  const [mergeMode, setMergeMode] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [targetId, setTargetId] = useState<number | null>(null);
  const [merging, setMerging] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  const toggleSelect = (personId: number) => {
    setMergeError(null);
    setSelected((prev) => {
      const next = prev.includes(personId)
        ? prev.filter((id) => id !== personId)
        : [...prev, personId];
      // Keep the explicit target if still selected; otherwise fall back to
      // the smallest id (default target) on next render.
      setTargetId((t) =>
        t !== null && next.includes(t)
          ? t
          : next.length > 0
            ? Math.min(...next)
            : null,
      );
      return next;
    });
  };
  const effectiveTarget =
    targetId !== null && selected.includes(targetId)
      ? targetId
      : selected.length > 0
        ? Math.min(...selected)
        : null;
  const canMerge = selected.length >= 2 && effectiveTarget !== null && !merging;

  const handleMerge = async () => {
    if (!canMerge || effectiveTarget === null) return;
    const source_ids = selected.filter((id) => id !== effectiveTarget);
    if (source_ids.length === 0) return;
    setMerging(true);
    setMergeError(null);
    try {
      await apiClient.post("/intelligence/people/merge", {
        target_id: effectiveTarget,
        source_ids,
      });
      await qc.invalidateQueries({ queryKey: ["people"] });
      setSelected([]);
      setTargetId(null);
      setMergeMode(false);
      useTransferStore.getState().showNotification("success", "People merged");
    } catch (e) {
      setMergeError(
        e instanceof Error ? e.message : "Merge failed — try again",
      );
    } finally {
      setMerging(false);
    }
  };

  const handlePersonClick = async (personId: number) => {
    if (mergeMode) {
      // Merge mode hijacks card clicks into select-toggles; the controlled
      // onSelect path below stays untouched for normal mode.
      toggleSelect(personId);
      return;
    }
    if (onSelect) {
      // Controlled mode: let the owner resolve faces if it wants.
      // Fetch here so the detail always carries media_ids.
      try {
        const { data } = await apiClient.get(
          `/intelligence/people/${personId}`,
        );
        const faces = (data?.faces ?? []) as { media_id: number }[];
        onSelect({
          person_id: personId,
          media_ids: faces.map((f) => f.media_id),
        });
      } catch {
        onSelect({ person_id: personId, media_ids: [] });
      }
      return;
    }
    // Default: broadcast so the library view can filter to this person.
    setNavigating(personId);
    try {
      const { data } = await apiClient.get(`/intelligence/people/${personId}`);
      const faces = (data?.faces ?? []) as { media_id: number }[];
      window.dispatchEvent(
        new CustomEvent<PersonSelectDetail>(PERSON_SELECT_EVENT, {
          detail: {
            person_id: personId,
            media_ids: faces.map((f) => f.media_id),
          },
        }),
      );
    } catch {
      window.dispatchEvent(
        new CustomEvent<PersonSelectDetail>(PERSON_SELECT_EVENT, {
          detail: { person_id: personId, media_ids: [] },
        }),
      );
    } finally {
      setNavigating(null);
    }
  };
  if (isLoading)
    return <div className="text-sm text-muted-foreground">Loading people…</div>;
  if (!data || data.people.length === 0) {
    const modelsReady = modelStatus?.ready ?? false;
    const modelsBusy =
      modelStatus?.status === "downloading" ||
      modelStatus?.status === "installing-packages";
    return (
      <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        <div className="flex items-center gap-2 font-semibold text-foreground mb-1">
          <Users className="w-4 h-4 text-action" /> People
        </div>
        <p>
          No face clusters yet. Index your library to scan media and group faces
          automatically.
        </p>
        {!modelsReady && modelStatus && (
          <p className="mt-1 text-xs">
            {modelsBusy
              ? `AI face models ${modelStatus.status === "installing-packages" ? "installing" : "downloading"}…`
              : `${modelStatus.missing.length} AI model(s) not installed — face grouping needs them.`}
          </p>
        )}
        {!facesAvailable && !modelsBusy && modelStatus && (
          <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
            Face models not installed — click "Get AI models" to download.
          </p>
        )}
        <div className="mt-3 flex items-center gap-2 flex-wrap">
          <button
            onClick={() => backfill.mutate()}
            disabled={isIndexing || !backendReady}
            className="px-3 py-1.5 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] disabled:opacity-40"
            title={
              backendReady
                ? "Fill missing perceptual hashes, dimensions and tags, then group faces (offline, idempotent)"
                : ENGINE_WAITING_LABEL
            }
          >
            {isIndexing ? "Indexing…" : "Index library"}
          </button>
          {!modelsReady && !modelsBusy && (
            <button
              onClick={() => downloadModels.mutate()}
              disabled={downloadModels.isPending || !backendReady}
              className="px-3 py-1.5 rounded-pill border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-muted active:scale-[0.95] disabled:opacity-40"
              title={
                backendReady
                  ? "Download on-board AI models (~218 MB, one time) to enable face grouping"
                  : ENGINE_WAITING_LABEL
              }
            >
              {downloadModels.isPending ? "Starting…" : "Get AI models"}
            </button>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Users className="w-4 h-4 text-action" /> People · {data.total}
        <button
          type="button"
          onClick={() => {
            setMergeMode((m) => !m);
            setSelected([]);
            setTargetId(null);
            setMergeError(null);
          }}
          aria-pressed={mergeMode}
          title={
            mergeMode
              ? "Exit merge mode"
              : "Select two or more clusters of the same person to merge"
          }
          className={cn(
            "ml-auto px-3 py-1 rounded-pill border text-xs font-medium active:scale-[0.95] transition",
            mergeMode
              ? "bg-action text-white border-action hover:bg-action/90"
              : "border-border text-muted-foreground hover:text-foreground hover:bg-muted",
          )}
        >
          {mergeMode ? "Done" : "Merge…"}
        </button>
      </div>
      {mergeMode && (
        <div className="rounded-lg border border-border bg-card p-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">
            {selected.length === 0
              ? "Tap cards to select clusters of the same person"
              : `${selected.length} selected`}
          </span>
          {selected.length > 0 && (
            <label className="flex items-center gap-1.5 text-muted-foreground">
              Keep as
              <select
                value={effectiveTarget ?? ""}
                onChange={(e) => setTargetId(Number(e.target.value))}
                aria-label="Merge target person"
                className="h-8 rounded-md border border-border bg-card px-2 text-xs text-foreground"
              >
                {selected
                  .slice()
                  .sort((a, b) => a - b)
                  .map((id) => {
                    const person = data.people.find((pp) => pp.id === id);
                    return (
                      <option key={id} value={id}>
                        {person?.name ?? `Person ${id}`}
                      </option>
                    );
                  })}
              </select>
            </label>
          )}
          <button
            type="button"
            onClick={handleMerge}
            disabled={!canMerge}
            title={
              selected.length < 2
                ? "Select at least 2 clusters to merge"
                : `Merge into ${effectiveTarget !== null ? (data.people.find((pp) => pp.id === effectiveTarget)?.name ?? `Person ${effectiveTarget}`) : ""}`
            }
            className="px-3 py-1.5 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] disabled:opacity-40 flex items-center gap-1"
          >
            {merging ? (
              <>
                <Loader2 className="w-3 h-3 animate-spin" /> Merging…
              </>
            ) : (
              "Merge selected"
            )}
          </button>
          {mergeError && (
            <span role="alert" className="text-xs text-red-600">
              {mergeError}
            </span>
          )}
        </div>
      )}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {data.people.map((p) => {
          const displayName = p.name ?? `Person ${p.id}`;
          const isSelected = selected.includes(p.id);
          return (
            <div
              key={p.id}
              className={cn(
                "rounded-lg border bg-card p-3 relative",
                mergeMode && isSelected
                  ? "border-action ring-2 ring-action/30"
                  : "border-border",
              )}
            >
              {mergeMode && (
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={isSelected}
                  aria-label={`Select ${displayName} for merge`}
                  title={`Select ${displayName} for merge`}
                  onClick={() => toggleSelect(p.id)}
                  className={cn(
                    "absolute left-2 top-2 z-10 w-5 h-5 rounded-md border flex items-center justify-center active:scale-[0.95] transition",
                    isSelected
                      ? "bg-action border-action text-white"
                      : "bg-card border-border text-transparent hover:border-action",
                  )}
                >
                  <Check className="w-3.5 h-3.5" />
                </button>
              )}
              <button
                type="button"
                onClick={() => handlePersonClick(p.id)}
                disabled={navigating === p.id}
                aria-label={`Show ${p.face_count === 1 ? "1 face" : `${p.face_count} faces`} of ${displayName} in library`}
                title={`Filter library to ${displayName}`}
                className="block w-full text-left active:scale-[0.98] transition-transform cursor-pointer disabled:opacity-60"
              >
                <FaceThumb
                  faceId={p.cover_face_id}
                  mediaId={p.cover_media_id}
                  mediaUrl={p.cover_thumbnail_url}
                  bbox={p.cover_bbox}
                  label={displayName}
                />
              </button>
              {editing === p.id ? (
                <div className="mt-2 flex items-center gap-1">
                  <input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Name this person"
                    className="flex-1 h-8 rounded-md border border-border px-2 text-sm"
                  />
                  <button
                    onClick={() => {
                      rename.mutate({ id: p.id, name: name || null });
                      setEditing(null);
                    }}
                    className="p-1.5 rounded-md bg-action text-white active:scale-[0.95]"
                  >
                    <Check className="w-3 h-3" />
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => {
                    setEditing(p.id);
                    setName(p.name ?? "");
                  }}
                  className="mt-2 text-sm font-semibold hover:text-action"
                >
                  {displayName}
                </button>
              )}
              <div className="text-xs text-muted-foreground">
                {p.face_count === 1 ? "1 face" : `${p.face_count} faces`}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function PersonDetail({
  personId,
  onBack,
}: {
  personId: number;
  onBack: () => void;
}) {
  const facesQuery = usePersonFaces(personId);
  const { data: people } = usePeople();
  const person: Person | undefined = people?.people.find(
    (p) => p.id === personId,
  );
  // Full-detail modal for a clicked photo (internal state — the modal fetches
  // the rest via useMediaItem; the stub below only covers the fetch window).
  const [selectedMediaId, setSelectedMediaId] = useState<number | null>(null);
  const { data: selectedDetail } = useMediaItem(selectedMediaId);
  // Clicking a face mounts/retargets this panel below the fold — bring it
  // into view so its photos are reliably seen (controlled onSelect mode).
  const detailRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    detailRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [personId]);
  const displayName = person?.name ?? `Person ${personId}`;
  const faces = facesQuery.data?.faces ?? [];
  // Dedupe media_ids — one card per photo, fetched directly via Thumb
  // (never via the library vault filter).
  const seen = new Set<number>();
  const mediaCards: { media_id: number; thumbnail_url?: string | null }[] = [];
  for (const f of faces) {
    if (seen.has(f.media_id)) continue;
    seen.add(f.media_id);
    mediaCards.push({ media_id: f.media_id, thumbnail_url: f.thumbnail_url });
  }
  const selectedCard =
    mediaCards.find((m) => m.media_id === selectedMediaId) ?? null;
  const modalItem: MediaItemInfo | null =
    selectedDetail ??
    (selectedCard
      ? {
          id: selectedCard.media_id,
          source_path: "",
          file_name: `Photo #${selectedCard.media_id}`,
          file_size: 0,
          hop1_status: "completed",
          hop2_status: "completed",
          final_status: "completed",
          thumbnail_status: "ready",
          thumbnail_url: selectedCard.thumbnail_url ?? undefined,
          created_at: "",
          updated_at: "",
        }
      : null);
  return (
    <div
      ref={detailRef}
      className="rounded-lg border border-border bg-card p-3 space-y-3"
    >
      <div className="flex items-center gap-3">
        <div className="w-16 h-16 shrink-0 overflow-hidden">
          <FaceThumb
            faceId={person?.cover_face_id}
            mediaId={person?.cover_media_id ?? mediaCards[0]?.media_id}
            mediaUrl={
              person?.cover_thumbnail_url ?? mediaCards[0]?.thumbnail_url
            }
            bbox={person?.cover_bbox}
            label={displayName}
            className="aspect-square object-cover rounded-lg w-16 h-16"
          />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold truncate">{displayName}</div>
          <div className="text-xs text-muted-foreground">
            {facesQuery.isLoading
              ? "Loading photos…"
              : `${faces.length} ${faces.length === 1 ? "face" : "faces"} across ${mediaCards.length} ${mediaCards.length === 1 ? "photo" : "photos"}`}
          </div>
        </div>
        <button
          type="button"
          onClick={onBack}
          className="px-3 py-1 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] shrink-0"
        >
          ← Back · Clear
        </button>
      </div>
      {facesQuery.isLoading ? (
        <div className="text-sm text-muted-foreground">Loading photos…</div>
      ) : facesQuery.isError ? (
        <div className="text-sm text-muted-foreground">
          Couldn&apos;t load this person&apos;s photos.
        </div>
      ) : mediaCards.length === 0 ? (
        <div className="text-sm text-muted-foreground">
          No photos found for {displayName}.
        </div>
      ) : (
        <div className="grid grid-cols-3 sm:grid-cols-5 gap-2">
          {mediaCards.map((m) => (
            <button
              key={m.media_id}
              type="button"
              onClick={() => setSelectedMediaId(m.media_id)}
              aria-label={`Open photo number ${m.media_id} full details`}
              title={`Open photo #${m.media_id}`}
              className="rounded-lg text-left cursor-pointer transition-transform active:scale-[0.97] focus:outline-none focus-visible:ring-2 focus-visible:ring-action"
            >
              <Thumb
                id={m.media_id}
                url={m.thumbnail_url}
                label={`#${m.media_id}`}
              />
            </button>
          ))}
        </div>
      )}
      <MediaDetailModal
        item={modalItem}
        onClose={() => setSelectedMediaId(null)}
      />
    </div>
  );
}

export function EmptySemanticHint() {
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <ScanSearch className="w-3 h-3" />
      Smart search runs fully offline — searches filenames today, visual AI
      search by photo content when models are installed.
    </div>
  );
}
