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
  X,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { API_BASE_URL, getLocalToken } from "@/lib/api-client";
import { createThumbQueue, type ThumbQueue } from "@/lib/thumb-queue";
import { PillSelect } from "./PillSelect";
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
const _PREVIEW_MAX_FILES = 5000;

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

export function getPreviewThumbnailUrl(
  absPath: string,
  deviceId?: string | null,
  size = 200,
): string {
  // <img> tags can't send the X-Local-Token header, so the token rides
  // along as ?token= (accepted by the thumbnail endpoints only).
  const token = getLocalToken();
  if (absPath.startsWith("ios://")) {
    const raw = absPath.slice("ios://".length);
    const slashIdx = raw.indexOf("/");
    const devId = deviceId || (slashIdx === -1 ? raw : raw.slice(0, slashIdx));
    const vPath = slashIdx === -1 ? "/" : "/" + raw.slice(slashIdx + 1);
    const p = new URLSearchParams({
      device_id: devId,
      path: vPath,
      size: String(size),
      ...(token ? { token } : {}),
    });
    return `${API_BASE_URL}/api/device/ios-thumbnail?${p}`;
  }
  const p = new URLSearchParams({
    path: absPath,
    size: String(size),
    ...(token ? { token } : {}),
  });
  return `${API_BASE_URL}/api/device/thumbnail?${p}`;
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
      aria-label={`${item.filename}, ${formatBytes(item.size_bytes)}, ${isSelected ? "selected" : "not selected"}`}
      title={`${item.filename} • ${formatBytes(item.size_bytes)}${item.duration_s ? ` • ${formatDuration(item.duration_s)}` : ""}`}
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

      {/* Top badges (Video / Duplicate) */}
      <div className="absolute top-1.5 left-1.5 flex items-center gap-1 z-10 pointer-events-none">
        {item.type === "video" && (
          <div className="px-1.5 py-0.5 rounded-md bg-black/60 backdrop-blur-xs text-white text-[9px] font-medium leading-none flex items-center gap-0.5">
            <Film className="w-2.5 h-2.5" />
            {item.duration_s != null
              ? formatDuration(item.duration_s)
              : "Video"}
          </div>
        )}
        {isLikelyDuplicate && (
          <div className="px-1.5 py-0.5 rounded-md bg-black/60 backdrop-blur-xs text-white text-[9px] font-medium leading-none flex items-center gap-0.5">
            <CheckCircle className="w-2.5 h-2.5 text-blue-400" />
            In library
          </div>
        )}
      </div>

      {/* Selection Checkbox */}
      <div
        role="checkbox"
        aria-checked={isSelected}
        className={cn(
          "absolute top-1.5 right-1.5 w-5 h-5 rounded-full flex items-center justify-center transition-all duration-150 z-10",
          isSelected
            ? "bg-action border-2 border-white shadow-xs"
            : "border-2 border-white/85 bg-black/20 group-hover:bg-black/40",
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

      {/* File info overlay (filename + size) */}
      <div className="absolute inset-x-0 bottom-0 p-1.5 bg-gradient-to-t from-black/80 via-black/40 to-transparent flex flex-col justify-end text-[10px] text-white leading-tight pointer-events-none">
        <div className="flex items-center justify-between gap-1">
          <span className="truncate font-medium">{item.filename}</span>
          <span className="shrink-0 text-[9px] text-white/90 font-mono">
            {formatBytes(item.size_bytes)}
          </span>
        </div>
      </div>
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
  sourcePath,
  onImportAll,
  isImporting,
}: {
  devicePath?: string | null;
  sourcePath?: string | null;
  onImportAll?: () => void;
  isImporting?: boolean;
}) {
  const displayFolder = devicePath || sourcePath;
  if (!displayFolder) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
        <ImageOff className="w-8 h-8 mb-2" />
        <p className="text-sm">No media files found in this directory</p>
      </div>
    );
  }

  const isDevice = Boolean(devicePath);

  return (
    <div className="flex flex-col items-center justify-center py-8 text-center space-y-3">
      <ImageOff className="w-8 h-8 text-muted-foreground" />
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">
          No media files directly in this folder
        </p>
        <p className="text-xs text-muted-foreground max-w-xs">
          {isDevice
            ? "Photos on iPhone live in subfolders like 100APPLE — import everything below, or pick a deeper folder."
            : "Media files may be located in subfolders — import everything under this folder to include them."}
        </p>
        {isDevice && (
          <p className="text-[11px] text-muted-foreground/80 max-w-xs">
            Note: iPhone exposes photos and videos only. Import documents from a
            folder on this PC or a USB drive.
          </p>
        )}
      </div>
      <button
        type="button"
        onClick={onImportAll}
        disabled={isImporting}
        className="inline-flex items-center gap-1.5 px-4 py-2 bg-action text-white rounded-pill text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all disabled:opacity-50"
      >
        {isImporting ? (
          <>
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading all media...
          </>
        ) : (
          <>
            <Upload className="w-3.5 h-3.5" />
            Import everything under {displayFolder}
          </>
        )}
      </button>
    </div>
  );
}

function ImportAllModal({
  data,
  onConfirm,
  onCancel,
  loading,
}: {
  data: {
    items: MediaPreviewItem[];
    total: number;
    photos: number;
    videos: number;
    totalSize: number;
    path: string;
  } | null;
  onConfirm: () => void;
  onCancel: () => void;
  loading: boolean;
}) {
  if (!data) return null;

  function formatBytes(bytes: number): string {
    if (bytes === 0) return "0 B";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="w-full max-w-2xl max-h-[90vh] bg-card border border-border rounded-xl overflow-hidden flex flex-col"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border p-4 bg-muted/30">
          <div className="flex items-center gap-2">
            <Upload className="w-5 h-5 text-action" />
            <h3 className="text-lg font-semibold text-foreground">
              Import everything under {data.path}
            </h3>
          </div>
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="p-1 rounded-lg text-muted-foreground hover:bg-muted hover:text-foreground transition-colors disabled:opacity-50"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Summary */}
        <div className="p-4 border-b border-border bg-muted/20 grid grid-cols-3 gap-4 text-center">
          <div className="space-y-1">
            <p className="text-2xl font-semibold text-foreground">
              {data.total}
            </p>
            <p className="text-xs text-muted-foreground">Total files</p>
          </div>
          <div className="space-y-1">
            <p className="text-2xl font-semibold text-action">
              {formatBytes(data.totalSize)}
            </p>
            <p className="text-xs text-muted-foreground">Total size</p>
          </div>
          <div className="space-y-1">
            <p className="text-2xl font-semibold text-foreground">
              {data.photos} / {data.videos}
            </p>
            <p className="text-xs text-muted-foreground">Photos / Videos</p>
          </div>
        </div>

        {/* Thumbnail grid preview */}
        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="grid grid-cols-4 gap-2">
              {Array.from({ length: 12 }).map((_, i) => (
                <div
                  key={i}
                  className="aspect-square rounded-lg bg-muted animate-pulse"
                />
              ))}
            </div>
          ) : data.items.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
              <ImageOff className="w-8 h-8 mb-2" />
              <p className="text-sm">No media files found in this directory</p>
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-xs text-muted-foreground">
                Showing all {data.items.length} files (newest first):
              </p>
              <div className="grid grid-cols-4 gap-2 max-h-[50vh] overflow-y-auto">
                {data.items.map((item) => (
                  <div
                    key={item.abs_path}
                    className="relative aspect-square rounded-lg overflow-hidden bg-muted"
                  >
                    <img
                      src={getPreviewThumbnailUrl(item.abs_path, null, 150)}
                      alt={item.filename}
                      className="w-full h-full object-cover"
                      loading="lazy"
                    />
                    <div className="absolute bottom-0 left-0 right-0 p-1 bg-gradient-to-t from-black/70 to-transparent text-[10px] text-white truncate">
                      {item.filename}
                    </div>
                    <div className="absolute top-1 right-1 flex items-center gap-1">
                      {item.type === "video" && (
                        <span className="px-1 py-0.5 bg-black/50 text-[9px] rounded">
                          Video
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="flex items-center justify-end gap-2 border-t border-border p-4 bg-muted/30">
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="px-4 py-2 text-sm font-normal text-foreground bg-muted hover:bg-muted/80 rounded-lg transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className="px-4 py-2 text-sm font-normal text-white bg-action hover:bg-action/90 active:scale-[0.95] rounded-lg transition-all disabled:opacity-50"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin mr-2" />
                Loading...
              </>
            ) : (
              "Import all"
            )}
          </button>
        </div>
      </motion.div>
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
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  const [importAllData, setImportAllData] = useState<{
    items: MediaPreviewItem[];
    total: number;
    photos: number;
    videos: number;
    totalSize: number;
    path: string;
  } | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const thumbQueueRef = useRef<ThumbQueue>(createThumbQueue(4));

  const isDefaultRecursive = Boolean(
    deviceSource &&
      (!deviceSource.device_path ||
        deviceSource.device_path === "/" ||
        deviceSource.device_path === "/DCIM" ||
        deviceSource.device_path.replace(/\/+$/, "") === "/DCIM"),
  );
  const [isRecursive, setIsRecursive] = useState(isDefaultRecursive);
  const isImportingAllRef = useRef(false);
  const prevSourceKeyRef = useRef("");
  const currentSourceKey = `${sourcePath || ""}:${deviceSource?.device_id || ""}:${deviceSource?.device_path || ""}`;

  useEffect(() => {
    if (prevSourceKeyRef.current !== currentSourceKey) {
      prevSourceKeyRef.current = currentSourceKey;
      const shouldRecurse = Boolean(
        deviceSource &&
          (!deviceSource.device_path ||
            deviceSource.device_path === "/" ||
            deviceSource.device_path === "/DCIM" ||
            deviceSource.device_path.replace(/\/+$/, "") === "/DCIM"),
      );
      setIsRecursive(shouldRecurse);
      setPage(1);
    }
  }, [currentSourceKey, deviceSource]);

  function getPreviewUrl(pageNum: number, pageSize: number, sort: string) {
    if (deviceSource) {
      const p = new URLSearchParams({
        device_id: deviceSource.device_id,
        path: deviceSource.device_path,
        page: String(pageNum),
        page_size: String(pageSize),
        sort_by: sort,
      });
      if (isRecursive) {
        p.set("recursive", "true");
      }
      return `${API_BASE_URL}/api/device/ios-preview?${p}`;
    }
    const p = new URLSearchParams({
      path: sourcePath || "",
      recursive: isRecursive ? "true" : "false",
      page: String(pageNum),
      page_size: String(pageSize),
      sort_by: sort,
    });
    return `${API_BASE_URL}/api/device/preview?${p}`;
  }

  function getThumbnailUrl(item: MediaPreviewItem) {
    return getPreviewThumbnailUrl(
      item.abs_path,
      deviceSource?.device_id,
      THUMBNAIL_SIZE,
    );
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

    if (isImportingAllRef.current) {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setPreviewError(null);
    if (page === 1) {
      setItems([]);
      setFocusedIndex(null);
    }

    const pageSize = isRecursive ? _PREVIEW_MAX_FILES : 100;
    const url = getPreviewUrl(page, pageSize, sortBy);

    fetch(url, {
      signal: controller.signal,
      headers: authHeaders(),
    })
      .then(async (res) => {
        if (!res.ok) {
          let message = "Preview fetch failed";
          try {
            const errBody = await res.json();
            const detail = errBody?.detail;
            message =
              typeof detail === "string" ? detail : detail?.message || message;
          } catch {
            /* keep default */
          }
          throw new Error(message);
        }
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
        setPreviewError(err?.message || "Preview fetch failed");
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
    isRecursive,
    retryNonce,
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

  const handleRetry = useCallback(() => {
    setPage(1);
    setRetryNonce((n) => n + 1);
  }, []);

  const handleImportAll = useCallback(async () => {
    if (!deviceSource && !sourcePath) return;
    isImportingAllRef.current = true;
    setLoading(true);
    setPreviewError(null);
    try {
      const p = new URLSearchParams({
        page: "1",
        page_size: String(_PREVIEW_MAX_FILES),
        sort_by: sortBy || "newest",
        recursive: "true",
      });

      let url = "";
      if (deviceSource) {
        p.set("device_id", deviceSource.device_id);
        p.set("path", deviceSource.device_path);
        url = `${API_BASE_URL}/api/device/ios-preview?${p}`;
      } else {
        p.set("path", sourcePath || "");
        url = `${API_BASE_URL}/api/device/preview?${p}`;
      }

      const res = await fetch(url, {
        headers: authHeaders(),
      });
      if (!res.ok) {
        let message = "Failed to load media preview";
        try {
          const errBody = await res.json();
          const detail = errBody?.detail;
          message =
            typeof detail === "string" ? detail : detail?.message || message;
        } catch {}
        throw new Error(message);
      }
      const data = await res.json();
      const loadedItems: MediaPreviewItem[] = data.items || [];
      const allPaths = loadedItems.map((item) => item.abs_path);

      setIsRecursive(true);
      setItems(loadedItems);
      setMetadata({
        total: data.total || loadedItems.length,
        photos: data.photos || 0,
        videos: data.videos || 0,
        total_size_bytes: data.total_size_bytes || 0,
      });
      setTotalPages(data.pages || 1);
      setPage(1);

      // Select all items by default
      const nextSelected = new Set(allPaths);
      selectedRef.current = nextSelected;
      setSelected(nextSelected);

      // Open import modal preview with summary and thumbnail grid
      setImportAllData({
        items: loadedItems,
        total: data.total || loadedItems.length,
        photos: data.photos || 0,
        videos: data.videos || 0,
        totalSize: data.total_size_bytes || 0,
        path: deviceSource?.device_path || sourcePath || "",
      });

      // Confirm selection to parent
      onSelectionConfirm(allPaths);
    } catch (err) {
      setPreviewError(
        err instanceof Error ? err.message : "Failed to load preview",
      );
    } finally {
      isImportingAllRef.current = false;
      setLoading(false);
    }
  }, [deviceSource, sourcePath, sortBy, onSelectionConfirm, onTransferStart]);

  const handleConfirmImportAll = useCallback(() => {
    if (!importAllData) return;
    const allPaths = importAllData.items.map((i) => i.abs_path);
    setIsRecursive(true);
    setItems(importAllData.items);
    setMetadata({
      total: importAllData.total,
      photos: importAllData.photos,
      videos: importAllData.videos,
      total_size_bytes: importAllData.totalSize,
    });
    setTotalPages(1);
    setPage(1);
    const nextSelected = new Set(allPaths);
    selectedRef.current = nextSelected;
    setSelected(nextSelected);
    setImportAllData(null);
    onSelectionConfirm(allPaths);
    onTransferStart?.(allPaths);
  }, [importAllData, onSelectionConfirm, onTransferStart]);

  const handleCancelImportAll = useCallback(() => {
    setImportAllData(null);
  }, []);

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
    <>
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
                {isRecursive && " (all subfolders)"}
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
              <span className="text-[11px] text-muted-foreground">
                (Ctrl+A)
              </span>
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
                  {f === "all" && `All (${metadata.total})`}
                  {f === "photo" && (
                    <span className="flex items-center gap-1">
                      <Image className="w-3 h-3" />
                      Photos {metadata.photos > 0 ? `(${metadata.photos})` : ""}
                    </span>
                  )}
                  {f === "video" && (
                    <span className="flex items-center gap-1">
                      <Film className="w-3 h-3" />
                      Videos {metadata.videos > 0 ? `(${metadata.videos})` : ""}
                    </span>
                  )}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              <SlidersHorizontal className="w-3 h-3 shrink-0" />
              <PillSelect
                value={sortBy}
                onChange={(val) => {
                  setSortBy(val);
                  setPage(1);
                  setItems([]);
                }}
                options={[
                  { value: "newest", label: "Newest first" },
                  { value: "oldest", label: "Oldest first" },
                  { value: "name_asc", label: "Name A–Z" },
                  { value: "name_desc", label: "Name Z–A" },
                  { value: "size_desc", label: "Largest first" },
                  { value: "size_asc", label: "Smallest first" },
                ]}
                align="right"
              />
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
              className="px-2.5 py-1 bg-action text-white rounded-pill hover:bg-action/90 active:scale-[0.95] transition-all shrink-0"
            >
              Select only new files
            </button>
          </div>
        )}

        {/* Thumbnail grid area */}
        <AnimatePresence mode="wait">
          {loading && <SkeletonGrid key="skeleton" count={20} />}

          {!loading && previewError && (
            <motion.div
              key="error"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
            >
              <div className="flex flex-col items-center justify-center py-8 text-center space-y-3">
                <div className="w-10 h-10 rounded-full bg-destructive/10 flex items-center justify-center">
                  <AlertTriangle className="w-5 h-5 text-destructive" />
                </div>
                <div className="space-y-1">
                  <p className="text-sm font-medium text-foreground">
                    Couldn&apos;t load this folder
                  </p>
                  <p className="text-xs text-muted-foreground max-w-xs">
                    {previewError}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={handleRetry}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-muted text-foreground rounded-pill text-xs font-normal hover:bg-muted/80 active:scale-[0.95] transition-all"
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                    Retry
                  </button>
                  {deviceSource && (
                    <button
                      type="button"
                      onClick={handleImportAll}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-action text-white rounded-pill text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all"
                    >
                      <Upload className="w-3.5 h-3.5" />
                      Import everything anyway
                    </button>
                  )}
                </div>
              </div>
            </motion.div>
          )}

          {!loading && !previewError && items.length === 0 && (
            <motion.div
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
            >
              <EmptyState
                devicePath={deviceSource?.device_path ?? null}
                sourcePath={sourcePath ?? null}
                onImportAll={handleImportAll}
                isImporting={loading}
              />
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
              "flex items-center gap-1.5 px-4 py-2 rounded-pill text-xs font-normal transition-all",
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
      <ImportAllModal
        data={importAllData}
        onConfirm={handleConfirmImportAll}
        onCancel={handleCancelImportAll}
        loading={loading}
      />
    </>
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
