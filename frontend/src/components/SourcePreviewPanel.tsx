import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import {
  Check,
  ImageOff,
  Upload,
  Image,
  Film,
  CheckCircle,
  AlertTriangle,
  RefreshCw,
  X,
  Loader2,
  Lock,
  Smartphone,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { API_BASE_URL, getLocalToken } from "@/lib/api-client";
import { useTransferStore } from "@/store/transfer";
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
  mtime?: number | null;
  /** Tiny inline placeholder (data URL) from the listing for blur-up. */
  lqip?: string | null;
}

export interface SourcePreviewPanelProps {
  sourcePath?: string | null;
  deviceSource?: { device_id: string; device_path: string } | null;
  isDeviceConnected?: boolean;
  onSelectionConfirm: (selectedPaths: string[], quiet?: boolean) => void;
  onTransferStart?: (paths?: string[]) => void;
  /** Device sources only: reveal the driver picker when preview is empty. */
  onTryAnotherDriver?: () => void;
  /** Device sources only: retry reaching the device when preview is empty. */
  onRetryDriver?: () => void;
  isRetryingDriver?: boolean;
  /** Fired once per source when the preview API answers 423 (device locked)
   *  so the page can flip the device row + gate transfer actions. */
  onDeviceLocked?: () => void;
  /** Fired when a preview fetch succeeds after a lock was reported, so the
   *  page can clear its locked state (row badge, transfer gates). */
  onDeviceUnlocked?: () => void;
  /** When the page already knows the device is locked, force the lock UI
   *  (hides Import-all, disables Start transfer, hides counts). */
  deviceLocked?: boolean;
  /** Reports the backend scan id (when served from a scan cache) so session
   *  create can pin its totals to this scan. */
  onScanId?: (scanId: string | null) => void;
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

// Viewport prefetch window — wide enough that a full page of tiles starts
// its gated fetch promptly even if the scroll container's scrollTop is
// stale after a page turn (cells below the fold still prefetch).
const THUMB_ROOT_MARGIN = "600px";
// Auto-retry backoff between gated thumbnail fetches (USB relief).
const THUMB_RETRY_DELAYS = [1000, 2000, 4000];
const THUMB_MAX_AUTO_RETRIES = 3;

// Two-lane thumbnails (Windows WTS_CACHEONLY / iOS PHCachingImageManager
// preheat analogy): the fast lane serves cache-only probes that hit RAM /
// disk L2 instantly; the slow lane gates real USB extracts. Device
// extracts stay narrow; local disk is cheap.
const FAST_LANE_MAX = 6;
const SLOW_LANE_DEVICE_MAX = 3;
const SLOW_LANE_LOCAL_MAX = 6;

// Negative cache: abs_path -> timestamp of last terminal failure.
// Prevents the same grey/failed tile from re-hammering USB on every
// scroll or page revisit. Cleared on manual retry; 60s TTL.
const thumbFailCache = new Map<string, number>();
const THUMB_FAIL_TTL_MS = 60_000;

function isThumbBlocked(absPath: string): boolean {
  const t = thumbFailCache.get(absPath);
  if (t == null) return false;
  if (Date.now() - t > THUMB_FAIL_TTL_MS) {
    thumbFailCache.delete(absPath);
    return false;
  }
  return true;
}

function markThumbFailed(absPath: string): void {
  thumbFailCache.set(absPath, Date.now());
}

function clearThumbFailed(absPath: string): void {
  thumbFailCache.delete(absPath);
}

/** Clear the whole module thumb-failure negative cache so cells re-enter
 *  the fast lane after a backend escalation recovery. No UI change — the
 *  same Retry button/spinner path calls this before refetching. */
export function clearAllThumbFailed(): void {
  thumbFailCache.clear();
}

// Backend grey-fallback signals a transient USB failure with HTTP 200 +
// a placeholder JPEG. When the backend exposes it via header, treat it
// as a failure (retry affordance + negative cache) instead of "loaded".
const GREY_FALLBACK_STATUSES = new Set([
  "fallback",
  "grey",
  "placeholder",
  "pending",
  "failed",
  "error",
  "transient",
]);

function isGreyFallbackResponse(res: Response): boolean {
  const h =
    res.headers.get("X-Thumb-Status") ||
    res.headers.get("X-Thumbnail-Status") ||
    res.headers.get("X-Thumb-Fallback");
  if (!h) return false;
  const v = h.trim().toLowerCase();
  // Backend sends `fallback-<reason>` (e.g. fallback-undecodable,
  // fallback-partial-truncated) — prefix-match so reasoned fallbacks are
  // treated as failures (retry affordance) instead of "loaded" images.
  return v.startsWith("fallback") || GREY_FALLBACK_STATUSES.has(v);
}

// Manual-retry param: ?full=1 forces the backend past the persistent
// fail-cache and the Stage-1 partial/prefix probe straight to the full
// transfer read (reuses transfer logic). Cleared implicitly per attempt.
function appendFullParam(url: string): string {
  return `${url}${url.includes("?") ? "&" : "?"}full=1`;
}

// Fast-lane probe param: backends that honor `cache=only` answer L2 hits
// instantly (X-Thumb-Status: hit) and misses without touching USB;
// backends that ignore it return the full image, which the fast path
// renders directly instead of fetching twice.
function appendCacheOnlyParam(url: string): string {
  return `${url}${url.includes("?") ? "&" : "?"}cache=only`;
}

// Explicit L2-miss signal on an otherwise-OK response (e.g. placeholder
// bytes with X-Thumb-Status: miss): fall through to the slow extract lane
// instead of painting the placeholder as "loaded".
function isThumbCacheMiss(res: Response): boolean {
  const h =
    res.headers.get("X-Thumb-Status") || res.headers.get("X-Thumbnail-Status");
  if (!h) return false;
  return h.trim().toLowerCase().includes("miss");
}

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

export function getPreviewThumbnailUrl(
  absPath: string,
  deviceId?: string | null,
  size = 200,
  fileSize?: number | null,
  fileMtime?: number | null,
  full = false,
): string {
  // <img> tags can't send the X-Local-Token header, so the token rides
  // along as ?token= (accepted by the thumbnail endpoints only).
  // file_size/file_mtime let the backend validate its on-disk thumbnail
  // cache without an extra stat round trip.
  const token = getLocalToken();
  const validation =
    fileSize != null || fileMtime != null
      ? {
          ...(fileSize != null ? { file_size: String(fileSize) } : {}),
          ...(fileMtime != null ? { file_mtime: String(fileMtime) } : {}),
        }
      : {};
  if (absPath.startsWith("ios://")) {
    const raw = absPath.slice("ios://".length);
    const slashIdx = raw.indexOf("/");
    const devId = deviceId || (slashIdx === -1 ? raw : raw.slice(0, slashIdx));
    const vPath = slashIdx === -1 ? "/" : "/" + raw.slice(slashIdx + 1);
    const p = new URLSearchParams({
      device_id: devId,
      path: vPath,
      size: String(size),
      ...validation,
      ...(token ? { token } : {}),
      ...(full ? { full: "1" } : {}),
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
  /** Fast lane: cache-only probes (high concurrency, no USB I/O). */
  fastQueue: ThumbQueue;
  /** Slow lane: gated USB extracts (adaptive concurrency). */
  slowQueue: ThumbQueue;
}

function MediaThumbCell({
  item,
  isSelected,
  onToggle,
  isLikelyDuplicate,
  isFocused,
  cellIndex,
  makeThumbnailUrl,
  fastQueue,
  slowQueue,
}: MediaThumbCellProps) {
  const cellRef = useRef<HTMLDivElement>(null);
  const [loadState, setLoadState] = useState<
    "none" | "loading" | "loaded" | "error"
  >("none");
  const [imgSrc, setImgSrc] = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelQueuedRef = useRef<(() => void) | null>(null);
  const fetchAbortRef = useRef<AbortController | null>(null);
  const blobUrlRef = useRef<string | null>(null);
  const slotHeldRef = useRef(false);
  // Which lane holds the slot ("fast" cache probe vs "slow" USB extract)
  // so releaseSlot() retires the grant against the correct queue.
  const heldLaneRef = useRef<"fast" | "slow" | null>(null);
  // Queue generation held at acquire time. After a page turn bumps the
  // queue epoch, a stale cell must drop ownership WITHOUT releasing, or
  // the new generation's lane accounting goes negative and stalls.
  const slotEpochRef = useRef<number | null>(null);
  const observedRef = useRef(false);
  const mountedRef = useRef(true);
  // Manual-retry flag: the next gated fetch appends ?full=1 so the backend
  // skips its fail-cache + Stage-1 probe and forces the full transfer read.
  // Set by the Retry button, cleared on a confirmed load.
  const forceFullRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const releaseSlot = useCallback(() => {
    if (slotHeldRef.current) {
      slotHeldRef.current = false;
      const lane = heldLaneRef.current;
      heldLaneRef.current = null;
      const acquiredEpoch = slotEpochRef.current;
      slotEpochRef.current = null;
      const queue = lane === "slow" ? slowQueue : fastQueue;
      // Stale generation (page turned since acquire): drop ownership
      // without touching the new page's accounting.
      if (acquiredEpoch == null || acquiredEpoch === queue.getEpoch()) {
        queue.release();
      }
    }
  }, [fastQueue, slowQueue]);

  const revokeBlob = useCallback(() => {
    if (blobUrlRef.current) {
      try {
        URL.revokeObjectURL(blobUrlRef.current);
      } catch {
        /* already revoked */
      }
      blobUrlRef.current = null;
    }
  }, []);

  const cleanupPending = useCallback(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (cancelQueuedRef.current) {
      cancelQueuedRef.current();
      cancelQueuedRef.current = null;
    }
    if (fetchAbortRef.current) {
      try {
        fetchAbortRef.current.abort();
      } catch {
        /* ignore */
      }
      fetchAbortRef.current = null;
    }
  }, []);

  // Reset per-item state when the cell is recycled for a new path.
  const prevItemIdRef = useRef<string | null>(null);
  if (prevItemIdRef.current !== item.abs_path) {
    prevItemIdRef.current = item.abs_path;
    retryCountRef.current = 0;
    observedRef.current = false;
    slotEpochRef.current = null;
    forceFullRef.current = false;
    if (retryCount !== 0) setRetryCount(0);
    if (imgSrc !== null) setImgSrc(null);
    if (loadState !== "none") setLoadState("none");
  }

  // Forward ref so the slow lane's retry timers can re-enter through the
  // fast lane without a const-ordering cycle (invoked only from timers,
  // which always fire after the render that assigns it below).
  const beginGatedFetchRef = useRef<() => void>(() => {});

  // Slow lane: gated USB extract. Reports 503/Retry-After pressure into
  // the queue's adaptive limiter (back off) and clean hits (recover).
  const startSlowFetch = useCallback(() => {
    const absPath = item.abs_path;
    // Generation guard: retries scheduled here must not fire after a page
    // turn bumps the queue epochs (reset() clears queued but not in-flight
    // work; the catch + timer below re-check this before re-queueing).
    // Both lanes reset() together, so the fast epoch guards both.
    const epochAtStart = fastQueue.getEpoch();
    const controller = new AbortController();
    fetchAbortRef.current = controller;
    // Manual retry (?full=1) bypasses the backend fail-cache and forces the
    // full transfer read; grey-fallback 200s stay errors (retry affordance)
    // and 503/Retry-After backoff below is unchanged.
    const url = forceFullRef.current
      ? appendFullParam(makeThumbnailUrl(item))
      : makeThumbnailUrl(item);
    // Server-driven backoff: a 503 carries Retry-After (seconds); capture
    // it before throwing so the catch below can honor it instead of the
    // static USB-relief delays.
    let retryAfterMs: number | null = null;

    fetch(url, { signal: controller.signal })
      .then((res) => {
        if (!mountedRef.current) return null;
        if (!res.ok) {
          const ra = res.headers.get("Retry-After");
          if (ra != null) {
            const secs = Number(ra);
            if (Number.isFinite(secs) && secs >= 0) {
              retryAfterMs = Math.min(Math.max(secs * 1000, 500), 15000);
            }
          }
          if (res.status === 503 || retryAfterMs != null) {
            slowQueue.reportThrottled(retryAfterMs);
          }
          throw new Error(`thumb http ${res.status}`);
        }
        if (isGreyFallbackResponse(res)) {
          throw new Error("thumb grey fallback");
        }
        return res.blob();
      })
      .then((blob) => {
        if (!mountedRef.current || blob === null) return;
        // Empty bodies are the grey-fallback shape on some backends —
        // surface retry instead of a "loaded" grey tile.
        if (blob.size === 0) throw new Error("thumb empty body");
        slowQueue.reportSuccess();
        revokeBlob();
        const objectUrl = URL.createObjectURL(blob);
        blobUrlRef.current = objectUrl;
        if (!mountedRef.current) {
          try {
            URL.revokeObjectURL(objectUrl);
          } catch {
            /* ignore */
          }
          return;
        }
        setImgSrc(objectUrl);
        // <img onLoad> below confirms decode and releases the slot.
      })
      .catch((err) => {
        if (!mountedRef.current) return;
        if (err instanceof Error && err.name === "AbortError") return;
        // Off-page cell (page turned while in flight): drop the slot
        // ownership without touching the new page's accounting and never
        // schedule a retry for a cell that is no longer visible.
        if (fastQueue.getEpoch() !== epochAtStart) {
          slotHeldRef.current = false;
          heldLaneRef.current = null;
          slotEpochRef.current = null;
          fetchAbortRef.current = null;
          return;
        }
        // Free the concurrency slot between attempts so retries don't
        // pin USB throughput while backing off.
        releaseSlot();
        fetchAbortRef.current = null;
        if (retryCountRef.current < THUMB_MAX_AUTO_RETRIES) {
          const delay =
            retryAfterMs ?? THUMB_RETRY_DELAYS[retryCountRef.current] ?? 4000;
          retryAfterMs = null;
          retryTimerRef.current = setTimeout(() => {
            if (!mountedRef.current) return;
            if (fastQueue.getEpoch() !== epochAtStart) return;
            retryCountRef.current += 1;
            setRetryCount(retryCountRef.current);
            // Re-enter through the fast lane: a cache-only probe is cheap
            // and a previously extracted thumb may have landed in L2 since.
            beginGatedFetchRef.current();
          }, delay);
        } else {
          markThumbFailed(absPath);
          setLoadState("error");
        }
      });
  }, [item, makeThumbnailUrl, fastQueue, slowQueue, releaseSlot, revokeBlob]);

  // Join the slow extract lane (acquires a slow slot, fetches on grant).
  const enqueueSlowFetch = useCallback(() => {
    if (!mountedRef.current) return;
    slotHeldRef.current = true;
    heldLaneRef.current = "slow";
    slotEpochRef.current = slowQueue.getEpoch();
    setLoadState("loading");
    cancelQueuedRef.current = slowQueue.request(() => {
      cancelQueuedRef.current = null;
      if (!mountedRef.current) {
        releaseSlot();
        return;
      }
      startSlowFetch();
    });
  }, [slowQueue, startSlowFetch, releaseSlot]);

  // Fast lane: cache-only probe (Windows WTS_CACHEONLY analogy). An L2 hit
  // (X-Thumb-Status: hit) renders instantly without spending slow-lane USB
  // budget; a miss falls through to the gated extract lane. Misses never
  // consume a retry attempt.
  const startFastFetch = useCallback(() => {
    const absPath = item.abs_path;
    const epochAtStart = fastQueue.getEpoch();
    const controller = new AbortController();
    fetchAbortRef.current = controller;
    // Manual retry re-enters through the fast lane but with ?full=1 (no
    // cache=only probe — the backend forces the full transfer read).
    const url = forceFullRef.current
      ? appendFullParam(makeThumbnailUrl(item))
      : appendCacheOnlyParam(makeThumbnailUrl(item));

    fetch(url, { signal: controller.signal })
      .then((res) => {
        if (!mountedRef.current) return null;
        if (!res.ok) {
          if (res.status === 503) {
            const ra = res.headers.get("Retry-After");
            let raMs: number | null = null;
            if (ra != null) {
              const secs = Number(ra);
              if (Number.isFinite(secs) && secs >= 0) {
                raMs = Math.min(Math.max(secs * 1000, 500), 15000);
              }
            }
            slowQueue.reportThrottled(raMs);
          }
          // Miss (404/202/204/...) or throttled — slow lane extracts.
          throw new Error("thumb fast-path miss");
        }
        if (isGreyFallbackResponse(res)) {
          throw new Error("thumb fast-path miss");
        }
        // Explicit L2 miss with a body: extract for real.
        if (isThumbCacheMiss(res)) {
          throw new Error("thumb fast-path miss");
        }
        return res.blob();
      })
      .then((blob) => {
        if (!mountedRef.current || blob === null) return;
        if (blob.size === 0) throw new Error("thumb fast-path miss");
        // Hit — render immediately. (A backend that ignores ?cache=only
        // returns the full image here; paint it either way and skip the
        // slow lane entirely instead of fetching twice.)
        fastQueue.reportSuccess();
        revokeBlob();
        const objectUrl = URL.createObjectURL(blob);
        blobUrlRef.current = objectUrl;
        if (!mountedRef.current) {
          try {
            URL.revokeObjectURL(objectUrl);
          } catch {
            /* ignore */
          }
          return;
        }
        setImgSrc(objectUrl);
        // <img onLoad> below confirms decode and releases the slot.
      })
      .catch((err) => {
        if (!mountedRef.current) return;
        if (err instanceof Error && err.name === "AbortError") return;
        if (fastQueue.getEpoch() !== epochAtStart) {
          slotHeldRef.current = false;
          heldLaneRef.current = null;
          slotEpochRef.current = null;
          fetchAbortRef.current = null;
          return;
        }
        // Drop the fast slot, then join the slow extract lane.
        releaseSlot();
        fetchAbortRef.current = null;
        if (isThumbBlocked(absPath)) {
          setLoadState("error");
          return;
        }
        enqueueSlowFetch();
      });
  }, [
    item,
    makeThumbnailUrl,
    fastQueue,
    slowQueue,
    enqueueSlowFetch,
    releaseSlot,
    revokeBlob,
  ]);

  // Shared gated-fetch entry: negative-cached failures render the retry
  // affordance without spending a slot; otherwise take the fast lane
  // (cache-only probe → slow-lane extract on miss).
  const beginGatedFetch = useCallback(() => {
    if (isThumbBlocked(item.abs_path)) {
      setLoadState("error");
      return;
    }
    slotHeldRef.current = true;
    heldLaneRef.current = "fast";
    slotEpochRef.current = fastQueue.getEpoch();
    setLoadState("loading");
    cancelQueuedRef.current = fastQueue.request(() => {
      cancelQueuedRef.current = null;
      if (!mountedRef.current) {
        releaseSlot();
        return;
      }
      startFastFetch();
    });
  }, [item.abs_path, fastQueue, startFastFetch, releaseSlot]);

  useEffect(() => {
    beginGatedFetchRef.current = beginGatedFetch;
  });

  useEffect(() => {
    const el = cellRef.current;
    if (!el) return;
    if (observedRef.current) return;

    const scrollContainer =
      el.closest(".overflow-y-auto") || el.closest(".overflow-auto") || null;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting && !observedRef.current) {
          observedRef.current = true;
          beginGatedFetch();
          observer.unobserve(el);
        }
      },
      { root: scrollContainer, rootMargin: THUMB_ROOT_MARGIN },
    );
    observer.observe(el);
    // Fallback: if the observer never fires (e.g. root mis-resolution
    // leaves the entry non-intersecting after a page turn), force the
    // gated fetch so the tile can't sit in "none" (plain grey) forever.
    const fallbackTimer = setTimeout(() => {
      if (observedRef.current || !mountedRef.current) return;
      observedRef.current = true;
      observer.unobserve(el);
      beginGatedFetch();
    }, 1500);
    return () => {
      clearTimeout(fallbackTimer);
      observer.disconnect();
    };
  }, [item.abs_path, beginGatedFetch]);

  useEffect(() => {
    return () => {
      cleanupPending();
      revokeBlob();
      releaseSlot();
    };
  }, [cleanupPending, revokeBlob, releaseSlot]);

  const handleLoad = () => {
    releaseSlot();
    forceFullRef.current = false;
    setLoadState("loaded");
  };

  const handleError = () => {
    // Blob-URL decode failure (rare) follows the same bounded retry path.
    releaseSlot();
    if (retryCountRef.current < THUMB_MAX_AUTO_RETRIES) {
      const delay = THUMB_RETRY_DELAYS[retryCountRef.current] ?? 4000;
      retryTimerRef.current = setTimeout(() => {
        if (!mountedRef.current) return;
        retryCountRef.current += 1;
        setRetryCount(retryCountRef.current);
        if (isThumbBlocked(item.abs_path)) {
          setLoadState("error");
          return;
        }
        // Decode-failure retry re-enters through the fast lane (cheap
        // cache-only probe first, slow extract on miss).
        beginGatedFetch();
      }, delay);
    } else {
      markThumbFailed(item.abs_path);
      setLoadState("error");
    }
  };

  const handleManualRetry = (e: React.MouseEvent) => {
    e.stopPropagation();
    // Manual retry: drop the frontend negative caches (single tile + the
    // module all-fail cache) and force the backend full transfer read via
    // ?full=1, re-entering through the fast lane. Grey-fallback 200s keep
    // rendering the retry affordance; 503/Retry-After backoff is unchanged.
    clearThumbFailed(item.abs_path);
    clearAllThumbFailed();
    cleanupPending();
    revokeBlob();
    releaseSlot();
    retryCountRef.current = 0;
    setRetryCount(0);
    setImgSrc(null);
    forceFullRef.current = true;
    // Manual retry re-enters through the fast lane (cheap cache-only probe
    // first, slow extract on miss).
    beginGatedFetch();
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
      {loadState === "loading" && !item.lqip && (
        <div className="absolute inset-0 bg-muted">
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent shimmer-animate" />
        </div>
      )}

      {/* LQIP blur-up placeholder from the listing JSON — paints instantly
          while the fast/slow thumb fetch resolves. */}
      {!imgSrc && item.lqip && loadState !== "error" && (
        <img
          src={item.lqip}
          alt=""
          aria-hidden
          decoding="async"
          className="absolute inset-0 w-full h-full object-cover blur-[2px] scale-105"
        />
      )}

      {imgSrc && (
        <img
          src={imgSrc}
          alt={item.filename}
          loading="lazy"
          decoding="async"
          onLoad={handleLoad}
          onError={handleError}
          className={cn(
            "w-full h-full object-cover transition-opacity duration-200",
            loadState === "loaded" ? "opacity-100" : "opacity-0",
          )}
        />
      )}

      {loadState === "error" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-muted">
          <ImageOff className="w-5 h-5 text-muted-foreground/40" />
          <button
            type="button"
            onClick={handleManualRetry}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-background/80 text-[10px] text-muted-foreground hover:text-foreground border border-border transition-colors"
          >
            <RefreshCw className="w-2.5 h-2.5" />
            Retry
          </button>
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
  onTryAnotherDriver,
  onRetryDriver,
  isRetryingDriver,
}: {
  devicePath?: string | null;
  sourcePath?: string | null;
  onImportAll?: () => void;
  isImporting?: boolean;
  onTryAnotherDriver?: () => void;
  onRetryDriver?: () => void;
  isRetryingDriver?: boolean;
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
      {isDevice && (onTryAnotherDriver || onRetryDriver) && (
        <div className="space-y-1.5 pt-1">
          <div className="flex items-center justify-center gap-2">
            {onTryAnotherDriver && (
              <button
                type="button"
                onClick={onTryAnotherDriver}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-border text-foreground rounded-pill text-xs font-normal hover:bg-muted active:scale-[0.95] transition-all"
              >
                Try another driver
              </button>
            )}
            {onRetryDriver && (
              <button
                type="button"
                onClick={onRetryDriver}
                disabled={isRetryingDriver}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-border text-foreground rounded-pill text-xs font-normal hover:bg-muted active:scale-[0.95] transition-all disabled:opacity-50"
              >
                {isRetryingDriver ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="w-3.5 h-3.5" />
                )}
                {isRetryingDriver ? "Retrying..." : "Retry"}
              </button>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground/80 max-w-xs">
            An empty folder can mean the wrong driver — switching drivers or
            retrying often brings photos back.
          </p>
        </div>
      )}
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
                {data.items.length > 32
                  ? `Showing preview of first 32 of ${data.items.length} files (newest first):`
                  : `Showing all ${data.items.length} files (newest first):`}
              </p>
              <div className="grid grid-cols-4 gap-2 max-h-[50vh] overflow-y-auto">
                {data.items.slice(0, 32).map((item) => (
                  <div
                    key={item.abs_path}
                    className="relative aspect-square rounded-lg overflow-hidden bg-muted"
                  >
                    <img
                      src={getPreviewThumbnailUrl(
                        item.abs_path,
                        null,
                        150,
                        item.size_bytes,
                        item.mtime ?? null,
                      )}
                      alt={item.filename}
                      className="w-full h-full object-cover"
                      loading="lazy"
                      decoding="async"
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
  isDeviceConnected,
  onSelectionConfirm,
  onTransferStart,
  onTryAnotherDriver,
  onRetryDriver,
  isRetryingDriver,
  onDeviceLocked,
  deviceLocked: _deviceLocked,
  onScanId,
  onDeviceUnlocked,
}: SourcePreviewPanelProps) {
  const queryClient = useQueryClient();
  const [deviceDisconnected, setDeviceDisconnected] = useState(false);
  const isDeviceDisconnected = Boolean(
    (deviceSource && isDeviceConnected === false) || deviceDisconnected,
  );

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
  const showNotification = useTransferStore((s) => s.showNotification);
  const allNewPathsRef = useRef<string[] | null>(null);
  const currentScanIdRef = useRef<string | null>(null);
  const [likelyDupPaths, setLikelyDupPaths] = useState<Set<string>>(new Set());
  const [focusedIndex, setFocusedIndex] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [sortBy, setSortBy] = useState("newest");
  // Device (USB/iOS) sources default to 25/page to bound DOM + thumbnail
  // pressure; local folders keep 100/page. User override via PillSelect
  // sticks (pageSizeTouched) across source switches.
  const [pageSize, setPageSize] = useState(100);
  const [pageSizeTouched, setPageSizeTouched] = useState(false);
  // Sizes seen on any page — keeps the bottom-bar byte total correct when
  // the selection spans pages (items only holds the current page).
  const [knownSizes, setKnownSizes] = useState<Map<string, number>>(new Map());
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  // Consecutive failed preview attempts for this source (timeout or HTTP
  // error). Success resets it. After 3 in a row the error panel adds a
  // cable/unlock hint — Retry itself is never taken away.
  const [failStreak, setFailStreak] = useState(0);
  // True when the preview API answered 423 (device locked). Drives the lock
  // panel + hides Import-all / Start transfer / counts until cleared.
  const [locked, setLocked] = useState(false);
  const lockNotifiedRef = useRef(false);
  // Frozen totals per source: the backend scan cache can report a drifting
  // total across refetches — the UI pins the first value per source so the
  // header, pills and footer never drift mid-session.
  const frozenMetaRef = useRef<{ key: string; total: number } | null>(null);
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
  // Latest callbacks without re-triggering the preview fetch effect.
  const onDeviceLockedRef = useRef(onDeviceLocked);
  onDeviceLockedRef.current = onDeviceLocked;
  const onDeviceUnlockedRef = useRef(onDeviceUnlocked);
  onDeviceUnlockedRef.current = onDeviceUnlocked;
  const onScanIdRef = useRef(onScanId);
  onScanIdRef.current = onScanId;
  // Two-lane thumbnails (Windows WTS_CACHEONLY / iOS PHCachingImageManager
  // preheat analogy): a fast lane for cache-only probes that hit RAM/disk
  // L2 instantly, and a gated slow lane for real USB extracts. Device
  // extracts stay at 3 concurrent; local disk is cheap — allow 6. Both
  // lanes reset() together on page turns so their epochs stay paired.
  const isDeviceSource = Boolean(deviceSource);
  const fastThumbQueueRef = useRef<ThumbQueue>(createThumbQueue(FAST_LANE_MAX));
  const slowThumbQueueRef = useRef<ThumbQueue>(
    createThumbQueue(
      isDeviceSource ? SLOW_LANE_DEVICE_MAX : SLOW_LANE_LOCAL_MAX,
    ),
  );
  useEffect(() => {
    fastThumbQueueRef.current.setMaxConcurrent(FAST_LANE_MAX);
    slowThumbQueueRef.current.setMaxConcurrent(
      isDeviceSource ? SLOW_LANE_DEVICE_MAX : SLOW_LANE_LOCAL_MAX,
    );
  }, [isDeviceSource]);

  // Adopt the per-source default page size until the user overrides it.
  useEffect(() => {
    if (!pageSizeTouched) {
      setPageSize(isDeviceSource ? 25 : 100);
    }
  }, [isDeviceSource, pageSizeTouched]);

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
      // Drop the previous folder's selection — its abs_paths can never match
      // this folder's items (stale "N selected / 0 B" + sessions that find
      // nothing). Quiet: no "selection cleared" toast on plain navigation.
      selectedRef.current = new Set();
      setSelected(new Set());
      onSelectionConfirm([], true);
      // Drop the previous folder's grid + frozen totals + lock state here
      // (not in the fetch effect) so page/sort turns keep the stale grid
      // visible while the next page loads — no full-skeleton flash.
      setItems([]);
      setMetadata({ total: 0, photos: 0, videos: 0, total_size_bytes: 0 });
      setTotalPages(1);
      setPreviewError(null);
      setLocked(false);
      setDeviceDisconnected(false);
      setFailStreak(0);
      lockNotifiedRef.current = false;
      frozenMetaRef.current = null;
      allNewPathsRef.current = null;
      currentScanIdRef.current = null;
      onScanIdRef.current?.(null);
    }
  }, [currentSourceKey, deviceSource, onSelectionConfirm]);

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
      item.size_bytes,
      item.mtime ?? null,
    );
  }
  // Stable reference so thumb cells don't re-observe / re-fetch on every
  // parent render.
  const makeThumbnailUrl = useCallback(
    (item: MediaPreviewItem) => getThumbnailUrl(item),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [deviceSource?.device_id],
  );

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

    if (deviceSource && isDeviceConnected === false) {
      setItems([]);
      setSelected(new Set());
      setFocusedIndex(null);
      setPage(1);
      setTotalPages(1);
      setLoading(false);
      setPreviewError(null);
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
      setFocusedIndex(null);
    }

    // Always respect user-selected page size (default 100) — bounds DOM and thumbnail queue
    const effPageSize = pageSize;
    const url = getPreviewUrl(page, effPageSize, sortBy);

    // Client-side ceiling so the skeleton can never spin forever: abort a
    // hung preview after 15 s and show the error panel (with Retry) instead.
    // Cleared on response, error, or effect cleanup (device/page change also
    // aborts the in-flight request via the cleanup below).
    const timeout = setTimeout(() => {
      if (cancelled) return;
      cancelled = true;
      try {
        controller.abort();
      } catch {
        /* ignore */
      }
      setFailStreak((n) => n + 1);
      setPreviewError(
        "Preview timed out after 15 seconds. Your device may be busy — try again.",
      );
      setLoading(false);
    }, 15000);

    fetch(url, {
      signal: controller.signal,
      headers: authHeaders(),
    })
      .then(async (res) => {
        if (!res.ok) {
          let message = "Preview fetch failed";
          let isDisconnected = false;
          try {
            const errBody = await res.json();
            const detail = errBody?.detail;
            const status = errBody?.status || detail?.status || "";
            const msg =
              typeof detail === "string" ? detail : detail?.message || "";
            if (
              res.status === 404 ||
              status === "disconnected" ||
              msg.toLowerCase().includes("not connected") ||
              msg.toLowerCase().includes("not reachable")
            ) {
              isDisconnected = true;
            }
            message = msg || message;
          } catch {
            if (res.status === 404 && deviceSource) {
              isDisconnected = true;
            }
          }
          // 423 = device locked mid-preview. Flag it (lock panel, hide
          // Import-all, gate Start) and notify the page exactly once per
          // source so it can flip the device row + invalidate caches.
          if (res.status === 423) {
            if (!lockNotifiedRef.current) {
              lockNotifiedRef.current = true;
              onDeviceLockedRef.current?.();
            }
            const lockedErr = new Error(message) as Error & {
              locked?: boolean;
            };
            lockedErr.locked = true;
            throw lockedErr;
          }
          if (isDisconnected) {
            const discErr = new Error(message) as Error & {
              disconnected?: boolean;
            };
            discErr.disconnected = true;
            throw discErr;
          }
          throw new Error(message);
        }
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        clearTimeout(timeout);
        // Replace, don't append: pages are navigated, not grown. This
        // bounds the DOM and the concurrent thumbnail work per page.
        // The previous page's items stay visible until this resolves
        // (stale-while-revalidate) — pagination/sort never flashes a full
        // skeleton and never triggers a full rescan (backend scan cache).
        const freshItems = data.items || [];
        setItems(freshItems);
        if (Array.isArray(data.likely_duplicate_paths)) {
          setLikelyDupPaths(new Set(data.likely_duplicate_paths));
        }
        if (Array.isArray(data.new_paths)) {
          allNewPathsRef.current = data.new_paths;
        }
        setKnownSizes((prev) => {
          const next = new Map(prev);
          for (const it of freshItems) {
            if (it?.abs_path != null) next.set(it.abs_path, it.size_bytes || 0);
          }
          return next;
        });
        if (page === 1) setFocusedIndex(null);
        // Freeze totals per source: pin the first reported total so later
        // refetches can't drift the header/pills/footer mid-session.
        // Page count derives from the frozen total + current page size.
        const freezeKey = `${currentSourceKey}|${isRecursive}`;
        const incomingTotal = data.total || 0;
        let displayTotal = incomingTotal;
        if (!frozenMetaRef.current || frozenMetaRef.current.key !== freezeKey) {
          frozenMetaRef.current = { key: freezeKey, total: incomingTotal };
          setMetadata({
            total: incomingTotal,
            photos: data.photos || 0,
            videos: data.videos || 0,
            total_size_bytes: data.total_size_bytes || 0,
          });
          currentScanIdRef.current =
            typeof data.scan_id === "string" ? data.scan_id : null;
          onScanIdRef.current?.(
            typeof data.scan_id === "string" ? data.scan_id : null,
          );
        } else {
          displayTotal = frozenMetaRef.current.total;
        }
        setTotalPages(
          Math.max(1, Math.ceil(Math.max(displayTotal, 1) / effPageSize)),
        );
        setLocked(false);
        setFailStreak(0);
        // Recovered after a lock: tell the page so it clears the row badge
        // and transfer gates.
        if (lockNotifiedRef.current) {
          lockNotifiedRef.current = false;
          onDeviceUnlockedRef.current?.();
        }
        setLoading(false);
      })
      .catch((err) => {
        if (err.name === "AbortError" || cancelled) return;
        clearTimeout(timeout);
        if ((err as { locked?: boolean }).locked) {
          setLocked(true);
        } else if ((err as { disconnected?: boolean }).disconnected) {
          setDeviceDisconnected(true);
        } else {
          setFailStreak((n) => n + 1);
        }
        setPreviewError(err?.message || "Preview fetch failed");
        setLoading(false);
      });

    return () => {
      cancelled = true;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [
    sourcePath,
    deviceSource?.device_id,
    deviceSource?.device_path,
    isDeviceConnected,
    page,
    pageSize,
    sortBy,
    isRecursive,
    retryNonce,
  ]);

  // Debounced + abortable so the prescan never stampedes the gated
  // thumbnail fetches on page turns — it fires 1s after the grid settles.
  useEffect(() => {
    if (items.length === 0) {
      setLikelyDupPaths(new Set());
      allNewPathsRef.current = null;
      return;
    }
    // If the preview response already provided whole-source duplicate data, skip per-page prescan
    if (allNewPathsRef.current !== null) {
      return;
    }
    const scanId = currentScanIdRef.current;
    const bodyPayload = scanId
      ? { scan_id: scanId }
      : {
          candidates: items.slice(0, 2000).map((item) => ({
            abs_path: item.abs_path,
            filename: item.filename,
            size_bytes: item.size_bytes,
          })),
        };
    const controller = new AbortController();
    const timer = setTimeout(() => {
      fetch(`${API_BASE_URL}/api/duplicates/prescan`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(bodyPayload),
        signal: controller.signal,
      })
        .then((res) => {
          if (!res.ok) throw new Error("Prescan failed");
          return res.json();
        })
        .then((data) => {
          setLikelyDupPaths(new Set(data.likely_duplicate_paths || []));
          if (Array.isArray(data.new_paths)) {
            allNewPathsRef.current = data.new_paths;
          }
        })
        .catch((err) => {
          if (err instanceof Error && err.name === "AbortError") return;
        });
    }, 1000);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [items]);

  // Next-page prefetch (iOS PHCachingImageManager preheat analogy): once
  // page N has rendered and the grid is idle, warm the browser HTTP cache
  // with the adjacent listing JSON (N+1, plus N-1 when N > 1) so a page
  // turn rarely waits on a USB scan. Fire-and-forget with its own
  // AbortController — responses are dropped; the point is priming cache.
  useEffect(() => {
    if (loading || items.length === 0 || previewError) return;
    if (totalPages <= 1) return;
    const targets: number[] = [];
    if (page + 1 <= totalPages) targets.push(page + 1);
    if (page > 1) targets.push(page - 1);
    if (targets.length === 0) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      for (const t of targets) {
        fetch(getPreviewUrl(t, pageSize, sortBy), {
          signal: controller.signal,
          headers: authHeaders(),
        }).catch(() => {
          /* prefetch is best-effort */
        });
      }
    }, 1200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // getPreviewUrl closes over the source key parts listed below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    loading,
    items.length,
    previewError,
    page,
    totalPages,
    pageSize,
    sortBy,
    isRecursive,
    sourcePath,
    deviceSource?.device_id,
    deviceSource?.device_path,
  ]);

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

  // True global toggle state: everything across all pages is selected.
  // Single-page sources (totalPages <= 1) collapse to the visible check so
  // the toggle still reads "Deselect all" without requiring an exact count.
  const isGloballySelected = useMemo(() => {
    if (metadata.total > 0 && selected.size >= metadata.total) return true;
    if (totalPages <= 1 && allVisibleSelected) return true;
    return false;
  }, [selected, metadata.total, totalPages, allVisibleSelected]);
  const [isSelectingAll, setIsSelectingAll] = useState(false);

  const handleSelectAll = useCallback(async () => {
    // Deselect path — clears the whole cross-page selection.
    if (isGloballySelected) {
      const next = new Set<string>();
      selectedRef.current = next;
      setSelected(next);
      onSelectionConfirm?.([]);
      return;
    }
    // Fast path: import-all modal data already holds every path.
    if (importAllData && importAllData.items.length > 0) {
      const targets =
        filter === "all"
          ? importAllData.items.map((i) => i.abs_path)
          : importAllData.items
              .filter((i) => i.type === filter)
              .map((i) => i.abs_path);
      const next = new Set(targets);
      selectedRef.current = next;
      setSelected(next);
      onSelectionConfirm?.(targets);
      return;
    }
    // Fast path: whole-source new-paths list already covers the total
    // (fresh source where everything is new) — no extra fetch needed.
    if (
      metadata.total > 0 &&
      allNewPathsRef.current !== null &&
      allNewPathsRef.current.length >= metadata.total
    ) {
      const targets = allNewPathsRef.current;
      const next = new Set(targets);
      selectedRef.current = next;
      setSelected(next);
      onSelectionConfirm?.(targets);
      return;
    }
    // Global path: fetch every abs_path across pages (import-all data
    // path), then select. Falls back to the current page on any failure.
    if (metadata.total > visibleItems.length && metadata.total > 0) {
      setIsSelectingAll(true);
      try {
        const want = Math.min(metadata.total, _PREVIEW_MAX_FILES);
        const p = new URLSearchParams({
          page: "1",
          page_size: String(want),
          sort_by: sortBy || "newest",
        });
        let url = "";
        if (deviceSource) {
          p.set("device_id", deviceSource.device_id);
          p.set("path", deviceSource.device_path);
          if (isRecursive) p.set("recursive", "true");
          url = `${API_BASE_URL}/api/device/ios-preview?${p}`;
        } else {
          p.set("path", sourcePath || "");
          p.set("recursive", isRecursive ? "true" : "false");
          url = `${API_BASE_URL}/api/device/preview?${p}`;
        }
        const res = await fetch(url, { headers: authHeaders() });
        if (!res.ok) throw new Error("select-all fetch failed");
        const data = await res.json();
        const loaded: MediaPreviewItem[] = data.items || [];
        const scoped =
          filter === "all" ? loaded : loaded.filter((it) => it.type === filter);
        const targets = scoped.map((it) => it.abs_path);
        if (targets.length > 0) {
          setKnownSizes((prev) => {
            const nextMap = new Map(prev);
            for (const it of scoped) {
              if (it?.abs_path != null)
                nextMap.set(it.abs_path, it.size_bytes || 0);
            }
            return nextMap;
          });
          const next = new Set(targets);
          selectedRef.current = next;
          setSelected(next);
          onSelectionConfirm?.(targets);
          return;
        }
      } catch {
        /* fall through to per-page fallback below */
      } finally {
        setIsSelectingAll(false);
      }
    }
    // Per-page fallback (small sources, fetch failure, or total unknown).
    const next = new Set(selectedRef.current);
    if (allVisibleSelected) {
      for (const item of visibleItems) next.delete(item.abs_path);
    } else {
      for (const item of visibleItems) next.add(item.abs_path);
    }
    selectedRef.current = next;
    setSelected(next);
    onSelectionConfirm?.(Array.from(next));
  }, [
    visibleItems,
    allVisibleSelected,
    isGloballySelected,
    importAllData,
    metadata.total,
    filter,
    sortBy,
    deviceSource,
    sourcePath,
    isRecursive,
    onSelectionConfirm,
  ]);

  const handleSelectPage = useCallback(() => {
    const next = new Set(selectedRef.current);
    if (allVisibleSelected) {
      for (const item of visibleItems) next.delete(item.abs_path);
    } else {
      for (const item of visibleItems) next.add(item.abs_path);
    }
    setKnownSizes((prev) => {
      const nextMap = new Map(prev);
      for (const it of visibleItems) {
        if (it?.abs_path != null) nextMap.set(it.abs_path, it.size_bytes || 0);
      }
      return nextMap;
    });
    selectedRef.current = next;
    setSelected(next);
    onSelectionConfirm?.(Array.from(next));
  }, [visibleItems, allVisibleSelected, onSelectionConfirm]);

  const handleSelectOnlyNew = useCallback(() => {
    let targets: string[] = [];
    if (allNewPathsRef.current !== null) {
      targets = allNewPathsRef.current;
    } else {
      // Fallback: visible items that are not in likelyDupPaths
      targets = visibleItems
        .filter((it) => !likelyDupPaths.has(it.abs_path))
        .map((it) => it.abs_path);
    }

    if (targets.length === 0) {
      selectedRef.current = new Set();
      setSelected(new Set());
      onSelectionConfirm([], true);
      const totalCount = metadata.total || visibleItems.length;
      showNotification(
        "info",
        `No new files found — all ${totalCount > 0 ? totalCount.toLocaleString() : ""} files already appear in your library.`.replace(
          "  ",
          " ",
        ),
      );
      return;
    }

    const next = new Set(targets);
    selectedRef.current = next;
    setSelected(next);
    onSelectionConfirm(targets, true);

    const dupCount = likelyDupPaths.size;
    showNotification(
      "info",
      `Selected ${targets.length.toLocaleString()} new file${targets.length !== 1 ? "s" : ""}${
        dupCount > 0
          ? ` (${dupCount.toLocaleString()} duplicate${dupCount !== 1 ? "s" : ""} excluded)`
          : ""
      }.`,
    );
  }, [
    visibleItems,
    likelyDupPaths,
    metadata.total,
    onSelectionConfirm,
    showNotification,
  ]);

  const handleRetry = useCallback(() => {
    // Preserve page position: only bump the nonce so the same page
    // refetches — never reset to page 1 (items stay as the stale grid).
    setLocked(false);
    setDeviceDisconnected(false);
    lockNotifiedRef.current = false;
    // Drop the thumb-failure negative cache so previously-failed tiles
    // re-enter the fast lane instead of rendering stale grey placeholders.
    clearAllThumbFailed();
    queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
    queryClient.invalidateQueries({ queryKey: ["device-preview"] });
    setRetryNonce((n) => n + 1);
  }, [queryClient]);

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
      setItems(loadedItems.slice(0, pageSize));
      setMetadata({
        total: data.total || loadedItems.length,
        photos: data.photos || 0,
        videos: data.videos || 0,
        total_size_bytes: data.total_size_bytes || 0,
      });
      setTotalPages(
        Math.max(1, Math.ceil((data.total || loadedItems.length) / pageSize)),
      );
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
  }, [
    deviceSource,
    sourcePath,
    sortBy,
    pageSize,
    onSelectionConfirm,
    onTransferStart,
  ]);

  const handleConfirmImportAll = useCallback(() => {
    if (!importAllData) return;
    const allPaths = importAllData.items.map((i) => i.abs_path);
    setIsRecursive(true);
    setItems(importAllData.items.slice(0, pageSize));
    setMetadata({
      total: importAllData.total,
      photos: importAllData.photos,
      videos: importAllData.videos,
      total_size_bytes: importAllData.totalSize,
    });
    setTotalPages(Math.max(1, Math.ceil(importAllData.total / pageSize)));
    setPage(1);
    const nextSelected = new Set(allPaths);
    selectedRef.current = nextSelected;
    setSelected(nextSelected);
    setImportAllData(null);
    onSelectionConfirm(allPaths);
    onTransferStart?.(allPaths);
  }, [importAllData, pageSize, onSelectionConfirm, onTransferStart]);

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
  const selectedBytes = useMemo(() => {
    const lookup = new Map(knownSizes);
    for (const it of items) lookup.set(it.abs_path, it.size_bytes);
    let bytes = 0;
    for (const p of selected) bytes += lookup.get(p) ?? 0;
    return bytes;
  }, [items, knownSizes, selected]);

  const goToPage = useCallback(
    (n: number) => {
      const clamped = Math.max(1, Math.min(totalPages, n));
      if (clamped === page) return;
      // Drop queued (not-yet-granted) thumb fetches on both lanes; granted
      // fetches abort via cell unmount (AbortController + blob revoke).
      // Also abort any in-flight preview page fetch so offscreen work stops
      // immediately.
      fastThumbQueueRef.current.reset();
      slowThumbQueueRef.current.reset();
      if (abortRef.current) {
        try {
          abortRef.current.abort();
        } catch {
          /* ignore */
        }
      }
      setFocusedIndex(null);
      setPage(clamped);
    },
    [page, totalPages],
  );

  // Page/sort/page-size turns swap the grid contents in place — reset the
  // inner scroll container (the grid div itself is the .overflow-y-auto
  // scroller; scrollIntoView on it is a no-op) so the new page's top
  // cells intersect the thumbnail observer.
  useEffect(() => {
    if (gridRef.current) gridRef.current.scrollTop = 0;
  }, [page, sortBy, pageSize]);

  const pageList = useMemo(() => {
    // Windowed page numbers: 1 … p-1 p p+1 … N
    const pages = new Set<number>([1, totalPages, page - 1, page, page + 1]);
    return [...pages]
      .filter((p) => p >= 1 && p <= totalPages)
      .sort((a, b) => a - b);
  }, [page, totalPages]);

  if (!sourcePath && !deviceSource) return null;

  return (
    <>
      <div className="space-y-2">
        {/* Header bar */}
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-foreground">
              Source preview
            </span>
            {isDeviceDisconnected && (
              <span className="text-xs text-muted-foreground">
                &middot; Device not connected
              </span>
            )}
            {!isDeviceDisconnected && metadata.total > 0 && !locked && (
              <span className="text-xs text-muted-foreground">
                {metadata.total} file{metadata.total !== 1 ? "s" : ""} &middot;{" "}
                {formatBytes(metadata.total_size_bytes)}
                {isRecursive && " (all subfolders)"}
              </span>
            )}
          </div>
          {!isDeviceDisconnected && items.length > 0 && !locked && (
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleSelectPage}
                title={
                  allVisibleSelected
                    ? "Deselect items on the current page"
                    : `Select all ${visibleItems.length} items on the current page`
                }
                className="text-xs font-normal text-muted-foreground hover:text-foreground transition-colors inline-flex items-center gap-1"
              >
                {allVisibleSelected
                  ? metadata.total > visibleItems.length
                    ? "Deselect page"
                    : "Deselect all"
                  : metadata.total > visibleItems.length
                    ? `Select page (${visibleItems.length})`
                    : `Select all (${visibleItems.length})`}
              </button>
              {metadata.total > visibleItems.length && (
                <button
                  type="button"
                  onClick={handleSelectAll}
                  disabled={isSelectingAll}
                  title={
                    isGloballySelected
                      ? "Clear the whole cross-page selection"
                      : `Select all ${metadata.total} files across pages`
                  }
                  className="text-xs font-normal text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50 inline-flex items-center gap-1"
                >
                  {isSelectingAll && (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  )}
                  {isGloballySelected
                    ? "Deselect all"
                    : `Select all (${metadata.total})`}
                </button>
              )}
              {selectedCount > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    const paths = Array.from(selected);
                    onSelectionConfirm(paths);
                    onTransferStart?.(paths);
                  }}
                  className="inline-flex items-center gap-1.5 px-3 py-1 bg-action hover:bg-action/90 text-white rounded-pill text-xs font-normal active:scale-[0.95] transition-all"
                  title={`Transfer ${selectedCount} selected file${selectedCount !== 1 ? "s" : ""}`}
                >
                  <Upload className="w-3.5 h-3.5" />
                  Transfer selected ({selectedCount})
                </button>
              )}
              <button
                type="button"
                onClick={handleImportAll}
                className={cn(
                  "inline-flex items-center gap-1.5 px-3 py-1 rounded-pill text-xs font-normal active:scale-[0.95] transition-all",
                  selectedCount === 0
                    ? "bg-action hover:bg-action/90 text-white"
                    : "border border-border text-foreground hover:bg-muted",
                )}
                title={`Import all ${metadata.total} files without scrolling`}
              >
                <Upload className="w-3.5 h-3.5" />
                Import all ({metadata.total})
              </button>
            </div>
          )}
        </div>

        {/* Filter pills + sort row */}
        {items.length > 0 && (
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
              <PillSelect
                value={String(pageSize)}
                onChange={(val) => {
                  setPageSize(Number(val) || 100);
                  setPageSizeTouched(true);
                  fastThumbQueueRef.current.reset();
                  slowThumbQueueRef.current.reset();
                  setPage(1);
                }}
                options={[
                  { value: "25", label: "25 / page" },
                  { value: "50", label: "50 / page" },
                  { value: "100", label: "100 / page" },
                ]}
                align="right"
              />
              <PillSelect
                value={sortBy}
                onChange={(val) => {
                  setSortBy(val);
                  setPage(1);
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
        {likelyDupPaths.size > 0 && (
          <div className="flex items-center gap-2 px-3 py-2 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 rounded-lg text-xs text-blue-700 dark:text-blue-300">
            <span className="flex-1">
              {likelyDupPaths.size} file{likelyDupPaths.size !== 1 ? "s" : ""}{" "}
              already appear to be in your library.
            </span>
            <button
              type="button"
              onClick={handleSelectOnlyNew}
              className="px-2.5 py-1 bg-action text-white rounded-pill hover:bg-action/90 active:scale-[0.95] transition-all shrink-0"
            >
              Select only new files
            </button>
          </div>
        )}

        {/* Thumbnail grid area — stale-while-revalidate: the previous
            page stays mounted while the next page loads (thin top bar
            signals the load). The full skeleton shows only on first load
            or a source change (both start with items empty). A fetch
            error never wipes items, so the stale grid + page position
            survive and Retry refetches in place. */}
        {loading && items.length > 0 && (
          <div
            className="h-1 rounded-full bg-muted overflow-hidden"
            role="progressbar"
            aria-label="Loading preview page"
          >
            <div className="h-full w-1/3 rounded-full bg-action/60 animate-pulse" />
          </div>
        )}
        <AnimatePresence>
          {loading && items.length === 0 && (
            <SkeletonGrid key="skeleton" count={20} />
          )}

          {!loading && isDeviceDisconnected && (
            <motion.div
              key="disconnected"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
            >
              <div className="flex flex-col items-center justify-center py-10 text-center space-y-3">
                <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center">
                  <Smartphone className="w-6 h-6 text-muted-foreground" />
                </div>
                <div className="space-y-1">
                  <p className="text-sm font-semibold text-foreground">
                    Device not connected
                  </p>
                  <p className="text-xs text-muted-foreground max-w-sm">
                    Connect your iPhone, iPad, Android phone, or digital camera
                    via USB cable and ensure the device is unlocked to preview
                    media.
                  </p>
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <button
                    type="button"
                    onClick={handleRetry}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-muted text-foreground rounded-pill text-xs font-normal hover:bg-muted/80 active:scale-[0.95] transition-all cursor-pointer"
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                    Scan again
                  </button>
                </div>
              </div>
            </motion.div>
          )}

          {!loading && !isDeviceDisconnected && previewError && (
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
                  {locked && (
                    <div className="mt-3 flex flex-col items-center gap-2 text-xs text-muted-foreground max-w-xs">
                      <p className="flex items-center gap-1.5 font-semibold text-amber-700 dark:text-amber-300">
                        <Lock className="w-3.5 h-3.5 shrink-0" />
                        iPhone locked — preview paused, not failed
                      </p>
                      <p className="text-[11px] leading-snug">
                        Unlock your iPhone and tap Trust once, then hit Retry
                        below. Pauses, not failed — Resume reuses the session
                        and skips completed files (current file restarts;
                        preview re-scans).
                      </p>
                      <ul className="text-left text-[11px] leading-snug space-y-0.5 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg px-3 py-2 w-full">
                        <li>• Tap the screen now and then to keep it awake</li>
                        <li>
                          • iPhone Settings &gt; Display &gt; Auto-Lock &gt;
                          Never (while transferring)
                        </li>
                        <li>• Use a good cable + rear USB port</li>
                        <li>• Tap Trust once when asked</li>
                      </ul>
                      <p className="text-[11px] text-muted-foreground/80 leading-snug">
                        Slow previews retry on their own — after 15 seconds we
                        keep this page so you can retry without losing your
                        place.
                      </p>
                    </div>
                  )}
                  {failStreak >= 3 && !locked && (
                    <p className="text-[11px] text-amber-600 dark:text-amber-400 max-w-xs">
                      Check your USB cable and make sure the iPhone is unlocked.
                    </p>
                  )}
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
                  {deviceSource && !locked && (
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

          {!loading &&
            !isDeviceDisconnected &&
            !previewError &&
            items.length === 0 && (
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
                  onTryAnotherDriver={onTryAnotherDriver}
                  onRetryDriver={onRetryDriver}
                  isRetryingDriver={isRetryingDriver}
                />
              </motion.div>
            )}

          {items.length > 0 && (
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
                className="grid grid-cols-4 gap-1 focus-visible:outline-none max-h-[460px] overflow-y-auto rounded-xl border border-border/40 p-1.5 bg-muted/10 scroll-smooth"
              >
                {visibleItems.map((item, index) => (
                  <MediaThumbCell
                    key={item.abs_path}
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
                    makeThumbnailUrl={makeThumbnailUrl}
                    fastQueue={fastThumbQueueRef.current}
                    slowQueue={slowThumbQueueRef.current}
                  />
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Pagination — pages are navigated, not grown, so the DOM and
            the concurrent thumbnail work stay bounded per page while the
            selection (and its byte total) spans pages */}
        {!isDeviceDisconnected && !previewError && totalPages > 1 && (
          <div className="flex items-center justify-center gap-1.5 pt-1">
            <button
              type="button"
              onClick={() => goToPage(page - 1)}
              disabled={page <= 1}
              className="px-2.5 py-1 text-xs rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-40 disabled:cursor-default"
            >
              ‹ Prev
            </button>
            {pageList.map((p, i, arr) => (
              <span key={p} className="flex items-center gap-1.5">
                {i > 0 && p - (arr[i - 1] ?? p) > 1 && (
                  <span className="text-xs text-muted-foreground">…</span>
                )}
                <button
                  type="button"
                  onClick={() => goToPage(p)}
                  aria-current={p === page ? "page" : undefined}
                  className={cn(
                    "min-w-7 px-2 py-1 text-xs rounded-full transition-colors",
                    p === page
                      ? "bg-action text-white"
                      : "text-muted-foreground hover:text-foreground hover:bg-muted border border-transparent",
                  )}
                >
                  {p}
                </button>
              </span>
            ))}
            <button
              type="button"
              onClick={() => goToPage(page + 1)}
              disabled={page >= totalPages}
              className="px-2.5 py-1 text-xs rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-40 disabled:cursor-default"
            >
              Next ›
            </button>
            <span className="text-[11px] text-muted-foreground ml-1">
              Page {page} of {totalPages}
            </span>
          </div>
        )}

        {/* Bottom action bar */}
        {isDeviceDisconnected && !locked && (
          <div className="flex items-center justify-between pt-1">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Smartphone className="w-3.5 h-3.5 shrink-0" />
              <span>Connect a device to preview and transfer</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-pill text-xs font-normal border border-border text-muted-foreground cursor-not-allowed opacity-40"
              >
                <Upload className="w-3.5 h-3.5" />
                Import all
              </button>
              <button
                type="button"
                disabled
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-pill text-xs font-normal bg-muted text-muted-foreground cursor-not-allowed opacity-40"
              >
                <Upload className="w-3.5 h-3.5" />
                Start transfer
              </button>
            </div>
          </div>
        )}

        {!locked && !isDeviceDisconnected && (
          <div className="flex items-center justify-between pt-1">
            <div>
              <p className="text-xs font-normal text-foreground">
                {selectedCount > 0
                  ? `${selectedCount} selected`
                  : `0 of ${items.length} selected`}
              </p>
              <p className="text-xs text-muted-foreground">
                {selectedCount > 0
                  ? `${formatBytes(selectedBytes)} to transfer`
                  : `${metadata.total} total files in folder (${formatBytes(metadata.total_size_bytes)})`}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleImportAll}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-pill text-xs font-normal border border-border text-foreground hover:bg-muted active:scale-[0.95] transition-all"
              >
                <Upload className="w-3.5 h-3.5 text-action" />
                Import all ({metadata.total})
              </button>
              <button
                type="button"
                onClick={() => {
                  const paths = Array.from(selected);
                  onSelectionConfirm(paths);
                  onTransferStart?.(paths);
                }}
                disabled={selectedCount === 0}
                className={cn(
                  "inline-flex items-center gap-1.5 px-4 py-2 rounded-pill text-xs font-normal transition-all",
                  selectedCount > 0
                    ? "bg-action text-white hover:bg-action/90 active:scale-[0.95]"
                    : "bg-muted text-muted-foreground cursor-default opacity-40",
                )}
              >
                <Upload className="w-3.5 h-3.5" />
                Start transfer {selectedCount > 0 ? `(${selectedCount})` : ""}
              </button>
            </div>
          </div>
        )}
        {locked && (
          <div className="flex items-center justify-between pt-1 gap-3">
            <div className="flex items-start gap-2 text-xs text-muted-foreground">
              <Lock className="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-600 dark:text-amber-400" />
              <span>
                <span className="font-semibold text-amber-700 dark:text-amber-300">
                  Device locked — paused, not failed.
                </span>{" "}
                Unlock iPhone, tap Trust once, then retry — pauses, not failed.
                Resume reuses the session and skips completed files (current
                file restarts; preview re-scans).
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-pill text-xs font-normal border border-border text-muted-foreground cursor-default opacity-40"
              >
                <Upload className="w-3.5 h-3.5" />
                Import all
              </button>
              <button
                type="button"
                disabled
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-pill text-xs font-normal bg-muted text-muted-foreground cursor-default opacity-40"
              >
                <Upload className="w-3.5 h-3.5" />
                Start transfer
              </button>
            </div>
          </div>
        )}
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
