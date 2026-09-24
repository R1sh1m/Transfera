// ---------------------------------------------------------------------------
// Transfera v2 — Intelligence Panels
// Timeline / Moments / Duplicates / People / Semantic search.
// Apple tokens: single Action Blue accent (bg-action), pill CTAs
// (rounded-pill), press state active:scale-[0.95], 17px body pace.
// ---------------------------------------------------------------------------

import { useState } from "react";
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
import { cn } from "@/lib/utils";
import {
  useBackfill,
  useCapabilities,
  useDuplicateGroups,
  useMoments,
  usePeople,
  useRenamePerson,
  useResolveDuplicateGroup,
  useSemanticSearch,
  useTimeline,
} from "@/lib/queries";
import { fetchThumbnail } from "@/lib/thumbnail-fetch";

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
  useState(() => {});
  // Lazy single fetch (fetchThumbnail already returns an object URL string)
  if (!src && !failed) {
    fetchThumbnail(id, url ?? undefined).then((s) => {
      if (s) setSrc(s);
      else setFailed(true);
    });
  }
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

export function CapabilitiesBadge() {
  const { data } = useCapabilities();
  const backfill = useBackfill();
  if (!data) return null;
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <span
        className={cn(
          "px-2 py-0.5 rounded-pill border border-border",
          data.semantic_mode === "clip"
            ? "text-green-600"
            : "text-muted-foreground",
        )}
        title={`phash: ${data.phash_available}, faces: ${data.faces_available}, clip: ${data.clip_available}`}
      >
        {data.semantic_mode === "clip" ? "CLIP on-device" : "Keyword search"} ·{" "}
        {data.faces_available ? "Faces on" : "Faces off"}
      </span>
      <button
        onClick={() => backfill.mutate()}
        disabled={backfill.isPending}
        className="px-3 py-1 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] disabled:opacity-40"
        title="Fill missing perceptual hashes, dimensions and tags (offline, idempotent)"
      >
        {backfill.isPending ? "Indexing…" : "Index library"}
      </button>
    </div>
  );
}

export function SemanticSearchBar({
  onResults,
}: {
  onResults: (q: string) => void;
}) {
  const [q, setQ] = useState("");
  const { data, isFetching } = useSemanticSearch(q);
  return (
    <div className="flex items-center gap-2">
      <div className="relative flex-1">
        <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            onResults(e.target.value);
          }}
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

export function MomentsGrid() {
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
        {data.moments.slice(0, 9).map((m, i) => (
          <div key={i} className="rounded-lg border border-border bg-card p-3">
            <div className="text-sm font-semibold">
              {new Date(m.start).toLocaleDateString()} →{" "}
              {new Date(m.end).toLocaleDateString()}
            </div>
            <div className="text-xs text-muted-foreground">
              {m.count} photos · event {i + 1}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function DuplicatesPanel() {
  const { data, isLoading } = useDuplicateGroups(8);
  const resolve = useResolveDuplicateGroup();
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
              disabled={resolve.isPending}
              onClick={() =>
                resolve.mutate({
                  keep_id: keepId,
                  trash_ids: g.members
                    .map((m) => m.id)
                    .filter((id) => id !== keepId),
                })
              }
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

export function PeoplePanel() {
  const { data, isLoading } = usePeople();
  const rename = useRenamePerson();
  const [editing, setEditing] = useState<number | null>(null);
  const [name, setName] = useState("");
  if (isLoading)
    return <div className="text-sm text-muted-foreground">Loading people…</div>;
  if (!data || data.people.length === 0)
    return (
      <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        <div className="flex items-center gap-2 font-semibold text-foreground">
          <Users className="w-4 h-4 text-action" /> People
        </div>
        No face clusters yet. Install the optional SCRFD + ArcFace ONNX models
        into the models folder to enable fully-offline face detection — the API
        and this panel are already wired.
      </div>
    );
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Users className="w-4 h-4 text-action" /> People · {data.total}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {data.people.map((p) => (
          <div
            key={p.id}
            className="rounded-lg border border-border bg-card p-3"
          >
            {editing === p.id ? (
              <div className="flex items-center gap-1">
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
                className="text-sm font-semibold hover:text-action"
              >
                {p.name ?? `Person ${p.id}`}
              </button>
            )}
            <div className="text-xs text-muted-foreground">
              {p.face_count} faces
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function EmptySemanticHint() {
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <ScanSearch className="w-3 h-3" />
      Semantic search runs fully offline — keyword mode today, CLIP embeddings
      when models are installed.
    </div>
  );
}
