import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Check,
  ImageOff,
  Upload,
  Image,
  Film,
  SlidersHorizontal,
  CheckCircle,
  AlertTriangle,
  RefreshCw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { API_BASE_URL, getLocalToken } from "@/lib/api-client";
import { createThumbQueue, type ThumbQueue } from "@/lib/thumb-queue";
import ErrorBoundary from "./ErrorBoundary";

export interface MediaPreviewItem {
  abs_path: string;
  filename: string;
  type: "photo" | "video" | "unknown";
  size_bytes: number;
  duration_s?: number | null;
  modified_at?: string | null;
}

export interface SourcePreviewPanelProps {
  sourcePath?: string | null;
  deviceSource?: { device_id: string; device_path: string } | null;
  onSelectionConfirm: (selectedPaths: string[]) => void;
  onTransferStart?: (paths?: string[]) => void;
}

function authHeaders(
  extra: Record<string, string> = {},
): Record<string, string> {
  const token = getLocalToken();
  return token ? { ...extra, "X-Local-Token": token } : { ...extra };
}

const THUMBNAIL_SIZE = 200;
const GRID_COLUMNS = 4;

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function formatDuration(seconds?: number | null): string | null {
  if (seconds == null) return null;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function totalSelectedSize(
  items: MediaPreviewItem[],
  selectedSet: Set<string>,
): number {
  let bytes = 0;
  for (const item of items) {
    if (selectedSet.has(item.abs_path)) {
      bytes += item.size_bytes;
    }
  }
  return bytes;
}

interface MediaThumbCellProps {
  item: MediaPreviewItem;
  isSelected: boolean;
  onToggle: () => void;
  isLikelyDuplicate: boolean;
  isFocused: boolean;
  cellIndex: number;
  makeThumbnailUrl: (item: MediaPreviewItem) => string;
  thumbQueue: ThumbQueue;
}

function MediaThumbCell({
  item,
  isSelected,
  onToggle,
  isLikelyDuplicate,
  isFocused,
  cellIndex,
  makeThumbnailUrl,
  thumbQueue,
}: MediaThumbCellProps) {
  const cellRef = useRef<HTMLDivElement>(null);
  const [loadState, setLoadState] = useState<
    "none" | "loading" | "loaded" | "error"
  >("none");
  const [imgSrc, setImgSrc] = useState<string | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const slotGrantedRef = useRef(false);
  const slotHeldRef = useRef(false);

  const prevItemIdRef = useRef<string | null>(null);
  if (prevItemIdRef.current !== item.abs_path) {
    prevItemIdRef.current = item.abs_path;
    slotGrantedRef.current = false;
    slotHeldRef.current = false;
    retryCountRef.current = 0;
  }

  useEffect(() => {
    const el = cellRef.current;
    if (!el) return;
    if (slotGrantedRef.current) return;

    const scrollContainer =
      el.closest(".overflow-y-auto") || el.closest(".overflow-auto") || null;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting && !slotGrantedRef.current) {
          slotGrantedRef.current = true;
          slotHeldRef.current = true;
          const url = makeThumbnailUrl(item);
          thumbQueue.request(() => {
            setImgSrc(url);
            setLoadState("loading");
          });
          observer.unobserve(el);
        }
      },
      { root: scrollContainer, rootMargin: "1200px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [item.abs_path, makeThumbnailUrl, thumbQueue]);

  useEffect(() => {
    return () => {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      if (slotHeldRef.current) {
        slotHeldRef.current = false;
        thumbQueue.release();
      }
    };
  }, [thumbQueue]);

  const handleLoad = () => {
    if (slotHeldRef.current) {
      slotHeldRef.current = false;
      thumbQueue.release();
    }
    setLoadState("loaded");
  };

  const handleError = () => {
    if (retryCountRef.current < 3) {
      const delays = [1000, 2000, 4000];
      const delay = delays[retryCountRef.current];
      retryTimerRef.current = setTimeout(() => {
        retryCountRef.current += 1;
        const base = makeThumbnailUrl(item);
        const separator = base.includes("?") ? "&" : "?";
        setImgSrc(`${base}${separator}retry=${retryCountRef.current}`);
        setLoadState("loading");
      }, delay);
    } else {
      if (slotHeldRef.current) {
        slotHeldRef.current = false;
        thumbQueue.release();
      }
      setLoadState("error");
    }
  };

  return (
    <div
      ref={cellRef}
      role="button"
      tabIndex={0}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle();
        }
      }}
      data-cell-index={cellIndex}
      aria-label={`${item.filename}, ${isSelected ? "selected" : "not selected"}`}
      className={cn(
        "relative aspect-square rounded-lg overflow-hidden cursor-pointer group bg-muted select-none",
        isFocused &&
          "outline-2 outline-primary outline-offset-[-2px] shadow-[0_0_0_3px_rgba(0,102,204,0.25)]",
      )}
    >
      {loadState === "loading" && (
        <div className="absolute inset-0 bg-muted">
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent shimmer-animate" />
        </div>
      )}

      {imgSrc && (
        <img
          src={imgSrc}
          alt={item.filename}
          onLoad={handleLoad}
          onError={handleError}
          className={cn(
            "w-full h-full object-cover transition-opacity duration-200",
            loadState === "loaded" ? "opacity-100" : "opacity-0",
          )}
        />
      )}

      {loadState === "error" && (
        <div className="absolute inset-0 flex items-center justify-center bg-muted">
          <ImageOff className="w-5 h-5 text-muted-foreground/40" />
        </div>
      )}

      <div
        role="checkbox"
        aria-checked={isSelected}
        className={cn(
          "absolute top-1.5 right-1.5 w-5 h-5 rounded-full flex items-center justify-center transition-all duration-150",
          isSelected
            ? "bg-action border-2 border-white shadow-xs"
            : "border-2 border-white/85 bg-black/15 group-hover:bg-black/25",
        )}
      >
        <AnimatePresence mode="wait">
          {isSelected && (
            <motion.div
              key="check"
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0, opacity: 0 }}
              transition={{ duration: 0.15, ease: "easeOut" }}
            >
              <Check className="w-2.5 h-2.5 text-white stroke-[3]" />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {item.type === "video" && (
        <div className="absolute bottom-1 left-1 px-1 py-0.5 rounded bg-black/45 text-white text-[10px] leading-none flex items-center gap-0.5">
          <Film className="w-2.5 h-2.5" />
          {item.duration_s != null ? formatDuration(item.duration_s) : ""}
        </div>
      )}

      {isLikelyDuplicate && (
        <div className="absolute bottom-1 right-1 px-1 py-0.5 rounded bg-black/45 text-white text-[10px] leading-none flex items-center gap-0.5">
          <CheckCircle className="w-2.5 h-2.5" />
          In library
        </div>
      )}
    </div>
  );
}

function SkeletonGrid({ count = 12 }: { count?: number }) {
  return (
    <div className="grid grid-cols-4 gap-0.5">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="aspect-square rounded-lg bg-muted relative overflow-hidden"
        >
          <div
            className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent shimmer-animate"
            style={{ animationDelay: `${i * 0.05}s` }}
          />
        </div>
      ))}
    </div>
  );
}

function EmptyState({
  devicePath,
  onImportAll,
}: {
  devicePath?: string | null;
  onImportAll?: () => void;
}) {
  if (!devicePath) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
        <ImageOff className="w-8 h-8 mb-2" />
        <p className="text-sm">No media files found in this directory</p>
      </div>
    );
  }
  // Device folder levels (e.g. iPhone /DCIM) contain only subfolders, so a
  // flat file grid is legitimately empty here — offer the recursive
  // import directly instead of a dead end. iPhones expose photos and
  // videos over AFC only; documents live in app sandboxes and cannot be
  // browsed from a device source.
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center space-y-3">
      <ImageOff className="w-8 h-8 text-muted-foreground" />
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">
          No media files directly in this folder
        </p>
        <p className="text-xs text-muted-foreground max-w-xs">
          Photos on iPhone live in subfolders like 100APPLE — import everything
          below, or pick a deeper folder.
        </p>
        <p className="text-[11px] text-muted-foreground/80 max-w-xs">
          Note: iPhone exposes photos and videos only. Import documents from a
          folder on this PC or a USB drive.
        </p>
      </div>
      <button
        type="button"
        onClick={onImportAll}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-action text-white rounded-lg text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all"
      >
        <Upload className="w-3.5 h-3.5" />
        Import everything under {devicePath}
      </button>
    </div>
  );
}

function SourcePreviewFallback({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border border-border rounded-xl p-3 bg-card">
      <div className="flex flex-col items-center justify-center py-8 text-center space-y-3">
        <div className="w-10 h-10 rounded-full bg-destructive/10 flex items-center justify-center">
          <AlertTriangle className="w-5 h-5 text-destructive" />
        </div>
        <div>
          <p className="text-sm font-semibold text-foreground">
            Preview unavailable
          </p>
          <p className="text-xs text-muted-foreground mt-1">
            Preview unavailable — you can still proceed with a full directory
            transfer.
          </p>
        </div>
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-normal hover:bg-primary/90 transition-colors"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry
        </button>
      </div>
    </div>
  );
}

function SourcePreviewPanelInner({
  sourcePath,
  deviceSource,
  onSelectionConfirm,
  onTransferStart,
}: SourcePreviewPanelProps) {
  const [items, setItems] = useState<MediaPreviewItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const selectedRef = useRef(selected);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  const [filter, setFilter] = useState<"all" | "photo" | "video">("all");
  const [loading, setLoading] = useState(false);
  const [metadata, setMetadata] = useState({
    total: 0,
    photos: 0,
    videos: 0,
    total_size_bytes: 0,
  });
  const [likelyDupPaths, setLikelyDupPaths] = useState<Set<string>>(new Set());
  const [focusedIndex, setFocusedIndex] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [sortBy, setSortBy] = useState("newest");

  const abortRef = useRef<AbortController | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const thumbQueueRef = useRef<ThumbQueue>(createThumbQueue(4));

  function getPreviewUrl(pageNum: number, pageSize: number, sort: string) {
    if (deviceSource) {
      const p = new URLSearchParams({
        device_id: deviceSource.device_id,
        path: deviceSource.device_path,
        page: String(pageNum),
        page_size: String(pageSize),
        sort_by: sort,
      });
      return `${API_BASE_URL}/api/device/ios-preview?${p}`;
    }
    const p = new URLSearchParams({
      path: sourcePath || "",
      recursive: "false",
      page: String(pageNum),
      page_size: String(pageSize),
      sort_by: sort,
    });
    return `${API_BASE_URL}/api/device/preview?${p}`;
  }

  function getThumbnailUrl(item: MediaPreviewItem) {
    if (deviceSource) {
      const virtualPath = item.abs_path.replace(
        `ios://${deviceSource.device_id}`,
        "",
      );
      const p = new URLSearchParams({
        device_id: deviceSource.device_id,
        path: virtualPath,
        size: String(THUMBNAIL_SIZE),
      });
      return `${API_BASE_URL}/api/device/ios-thumbnail?${p}`;
    }
    return `${API_BASE_URL}/api/device/thumbnail?path=${encodeURIComponent(item.abs_path)}&size=${THUMBNAIL_SIZE}`;
  }

  useEffect(() => {
    return () => {
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  useEffect(() => {
    if (!sourcePath && !deviceSource) {
      setItems([]);
      setSelected(new Set());
      setFocusedIndex(null);
      setPage(1);
      setTotalPages(1);
      setLoading(false);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    if (page === 1) {
      setItems([]);
      setFocusedIndex(null);
    }

    const url = getPreviewUrl(page, 100, sortBy);

    fetch(url, {
      signal: controller.signal,
      headers: authHeaders(),
    })
      .then((res) => {
        if (!res.ok) throw new Error("Preview fetch failed");
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        setItems((prev) =>
          page === 1 ? data.items || [] : [...prev, ...(data.items || [])],
        );
        if (page === 1) setFocusedIndex(null);
        setMetadata({
          total: data.total || 0,
          photos: data.photos || 0,
          videos: data.videos || 0,
          total_size_bytes: data.total_size_bytes || 0,
        });
        setTotalPages(data.pages || 1);
        setLoading(false);
      })
      .catch((err) => {
        if (err.name === "AbortError" || cancelled) return;
        setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [
    sourcePath,
    deviceSource?.device_id,
    deviceSource?.device_path,
    page,
    sortBy,
  ]);

  useEffect(() => {
    if (items.length === 0) {
      setLikelyDupPaths(new Set());
      return;
    }
    const candidates = items.slice(0, 2000).map((item) => ({
      abs_path: item.abs_path,
      filename: item.filename,
      size_bytes: item.size_bytes,
    }));
    fetch(`${API_BASE_URL}/api/duplicates/prescan`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ candidates }),
    })
      .then((res) => {
        if (!res.ok) throw new Error("Prescan failed");
        return res.json();
      })
      .then((data) => {
        setLikelyDupPaths(new Set(data.likely_duplicate_paths || []));
      })
      .catch(() => {});
  }, [items]);

  const toggleItem = useCallback(
    (absPath: string) => {
      const next = new Set(selectedRef.current);
      if (next.has(absPath)) next.delete(absPath);
      else next.add(absPath);
      selectedRef.current = next;
      setSelected(next);
      onSelectionConfirm?.(Array.from(next));
    },
    [onSelectionConfirm],
  );

  const visibleItems = useMemo(() => {
    if (filter === "all") return items;
    return items.filter((item) => item.type === filter);
  }, [items, filter]);

  useEffect(() => {
    setFocusedIndex((prev) => {
      if (prev === null) return null;
      return Math.min(prev, Math.max(0, visibleItems.length - 1));
    });
  }, [visibleItems.length]);

  const allVisibleSelected = useMemo(() => {
    if (visibleItems.length === 0) return false;
    return visibleItems.every((item) => selected.has(item.abs_path));
  }, [visibleItems, selected]);

  const handleSelectAll = useCallback(() => {
    const next = new Set(selectedRef.current);
    if (allVisibleSelected) {
      for (const item of visibleItems) next.delete(item.abs_path);
    } else {
      for (const item of visibleItems) next.add(item.abs_path);
    }
    selectedRef.current = next;
    setSelected(next);
    onSelectionConfirm?.(Array.from(next));
  }, [visibleItems, allVisibleSelected, onSelectionConfirm]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (visibleItems.length === 0) return;

      if ((e.ctrlKey || e.metaKey) && (e.key === "a" || e.key === "A")) {
        e.preventDefault();
        handleSelectAll();
        return;
      }

      if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) {
        e.preventDefault();
        setFocusedIndex((prev) => {
          if (prev === null) return 0;
          const COLS = GRID_COLUMNS;
          if (e.key === "ArrowRight") return (prev + 1) % visibleItems.length;
          if (e.key === "ArrowLeft")
            return (prev - 1 + visibleItems.length) % visibleItems.length;
          if (e.key === "ArrowDown") return (prev + COLS) % visibleItems.length;
          if (e.key === "ArrowUp")
            return (prev - COLS + visibleItems.length) % visibleItems.length;
          return prev;
        });
        return;
      }

      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        if (focusedIndex === null) return;
        const item = visibleItems[focusedIndex];
        if (item) toggleItem(item.abs_path);
      }
    },
    [visibleItems, handleSelectAll, toggleItem, focusedIndex],
  );

  const handleGridBlur = useCallback((e: React.FocusEvent) => {
    if (
      gridRef.current &&
      !gridRef.current.contains(e.relatedTarget as Node | null)
    ) {
      setFocusedIndex(null);
    }
  }, []);

  useEffect(() => {
    if (focusedIndex === null) return;
    const cell = gridRef.current?.querySelector<HTMLElement>(
      `[data-cell-index="${focusedIndex}"]`,
    );
    if (cell) cell.scrollIntoView({ block: "nearest" });
  }, [focusedIndex]);

  const selectedCount = selected.size;
  const selectedBytes = totalSelectedSize(items, selected);

  if (!sourcePath && !deviceSource) return null;

  return (
    <div className="space-y-2">
      {/* Header bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-normal text-foreground">
            Source preview
          </span>
          {!loading && (
            <span className="text-xs text-muted-foreground">
              {metadata.total} files &middot;{" "}
              {formatBytes(metadata.total_size_bytes)}
            </span>
          )}
        </div>
        {!loading && items.length > 0 && (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={handleSelectAll}
              className="text-xs font-normal text-primary hover:text-primary/80 transition-colors"
            >
              {allVisibleSelected ? "Deselect all" : "Select all"}
            </button>
            <span className="text-[11px] text-muted-foreground">(Ctrl+A)</span>
          </div>
        )}
      </div>

      {/* Filter pills + sort row */}
      {!loading && items.length > 0 && (
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1" role="tablist">
            {(["all", "photo", "video"] as const).map((f) => (
              <button
                key={f}
                type="button"
                role="tab"
                aria-selected={filter === f}
                onClick={() => setFilter(f)}
                className={cn(
                  "px-2.5 py-1 rounded-full text-[11px] font-normal transition-colors",
                  filter === f
                    ? "bg-action text-white"
                    : "bg-muted text-muted-foreground hover:bg-muted/80",
                )}
              >
                {f === "all" && "All"}
                {f === "photo" && (
                  <span className="flex items-center gap-1">
                    <Image className="w-3 h-3" />
                    Photos
                  </span>
                )}
                {f === "video" && (
                  <span className="flex items-center gap-1">
                    <Film className="w-3 h-3" />
                    Videos
                  </span>
                )}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 text-xs text-muted-foreground">
            <SlidersHorizontal className="w-3 h-3 shrink-0" />
            <select
              value={sortBy}
              onChange={(e) => {
                setSortBy(e.target.value);
                setPage(1);
                setItems([]);
              }}
              className="bg-transparent text-xs text-muted-foreground border-none outline-none cursor-pointer hover:text-foreground transition-colors"
            >
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="name_asc">Name A–Z</option>
              <option value="name_desc">Name Z–A</option>
              <option value="size_desc">Largest first</option>
              <option value="size_asc">Smallest first</option>
            </select>
          </div>
        </div>
      )}

      {/* Pre-scan banner */}
      {!loading && likelyDupPaths.size > 0 && (
        <div className="flex items-center gap-2 px-3 py-2 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 rounded-lg text-xs text-blue-700 dark:text-blue-300">
          <span className="flex-1">
            {likelyDupPaths.size} file{likelyDupPaths.size !== 1 ? "s" : ""}{" "}
            already appear to be in your library.
          </span>
          <button
            type="button"
            onClick={() => {
              const next = new Set(selectedRef.current);
              for (const item of visibleItems) {
                if (!likelyDupPaths.has(item.abs_path)) {
                  next.add(item.abs_path);
                } else {
                  next.delete(item.abs_path);
                }
              }
              selectedRef.current = next;
              setSelected(next);
              onSelectionConfirm?.(Array.from(next));
            }}
            className="px-2 py-1 bg-action text-white rounded-full hover:bg-action/90 active:scale-[0.95] transition-all shrink-0"
          >
            Select only new files
          </button>
        </div>
      )}

      {/* Thumbnail grid area */}
      <AnimatePresence mode="wait">
        {loading && <SkeletonGrid key="skeleton" count={20} />}

        {!loading && items.length === 0 && (
          <motion.div
            key="empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <EmptyState />
          </motion.div>
        )}

        {!loading && items.length > 0 && (
          <motion.div
            key="grid"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div
              ref={gridRef}
              tabIndex={0}
              onKeyDown={handleKeyDown}
              onBlur={handleGridBlur}
              role="grid"
              aria-label={`Source preview — ${items.length} files`}
              className="grid grid-cols-4 gap-0.5 focus-visible:outline-none"
            >
              {visibleItems.map((item, index) => (
                <MediaThumbCell
                  key={`${sortBy}-${page}-${item.abs_path}`}
                  item={item}
                  isSelected={selected.has(item.abs_path)}
                  onToggle={() => {
                    setFocusedIndex(index);
                    gridRef.current?.focus();
                    toggleItem(item.abs_path);
                  }}
                  isLikelyDuplicate={likelyDupPaths.has(item.abs_path)}
                  isFocused={index === focusedIndex}
                  cellIndex={index}
                  makeThumbnailUrl={getThumbnailUrl}
                  thumbQueue={thumbQueueRef.current}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Load more button */}
      {!loading && page < totalPages && (
        <button
          type="button"
          onClick={() => setPage((p) => p + 1)}
          className="w-full py-2 text-xs font-normal text-primary hover:text-primary/80 transition-colors border border-border rounded-lg"
        >
          Load more ({metadata.total - items.length} remaining)
        </button>
      )}

      {/* Bottom action bar */}
      <div className="flex items-center justify-between pt-1">
        <div>
          <p className="text-xs font-normal text-foreground">
            {selectedCount > 0 ? `${selectedCount} selected` : "0 selected"}
          </p>
          <p className="text-xs text-muted-foreground">
            {selectedCount > 0
              ? `${formatBytes(selectedBytes)} to transfer`
              : "Select files to transfer"}
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            const paths = Array.from(selected);
            onSelectionConfirm(paths);
            onTransferStart?.(paths);
          }}
          disabled={selectedCount === 0}
          className={cn(
            "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-normal transition-all",
            selectedCount > 0
              ? "bg-action text-white hover:bg-action/90 active:scale-[0.95]"
              : "bg-muted text-muted-foreground cursor-default opacity-40",
          )}
        >
          <Upload className="w-3.5 h-3.5" />
          Start transfer
        </button>
      </div>
    </div>
  );
}

export default function SourcePreviewPanel(props: SourcePreviewPanelProps) {
  const [resetKey, setResetKey] = useState(0);
  return (
    <ErrorBoundary
      key={resetKey}
      fallback={
        <SourcePreviewFallback onRetry={() => setResetKey((k) => k + 1)} />
      }
    >
      <SourcePreviewPanelInner key={resetKey} {...props} />
    </ErrorBoundary>
  );
}
