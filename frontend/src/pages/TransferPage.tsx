// ---------------------------------------------------------------------------
// Transfera v2 — Transfer Page
// Split layout: PreviewPanel (media thumbnails) + TransferMonitor (stats).
// Polling-based: REST is the source of truth, WebSocket is a bonus.
// ---------------------------------------------------------------------------

import { useEffect, useState, useRef, useMemo, useCallback } from "react";
import type { ThumbQueue } from "@/lib/thumb-queue";
import { createThumbQueue } from "@/lib/thumb-queue";
import { fetchThumbnail, revokeThumbnail } from "@/lib/thumbnail-fetch";
import { motion, AnimatePresence } from "framer-motion";
import {
  Play,
  Pause,
  X,
  ArrowLeft,
  FolderOpen,
  Image,
  Film,
  Music,
  FileText,
  Clock,
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  Loader2,
  Zap,
  HardDrive,
  RefreshCw,
  Upload,
} from "lucide-react";
import {
  useSession,
  useStartSession,
  usePauseSession,
  useCancelSession,
  useSessionProgress,
  useSessionBatches,
  useMediaList,
  useClearDeviceImportState,
} from "@/lib/queries";
import { useTransferStore } from "@/store/transfer";
import { useTransferWs } from "@/hooks/use-transfer-ws";
import {
  useBackendReady,
  ENGINE_WAITING_LABEL,
} from "@/hooks/use-backend-ready";
import { cn } from "@/lib/utils";
import { isDesktop, openPath, setTrayProgress } from "@/lib/desktop";
import { getLocalToken, API_BASE_URL } from "@/lib/api-client";
import { VerifiedCheckBadge } from "@/components/VerifiedCheckBadge";
import type { SessionProgress, RecentItemProgress } from "@/types/api";

// Keep track of session IDs that have already been started to prevent double-triggering
// on component mount/remount in React Strict Mode.
const startedSessions = new Set<number>();

const STATUS_LABELS: Record<string, string> = {
  created: "Ready",
  running: "Transferring",
  paused: "Paused",
  completed: "Completed",
  completed_with_errors: "Done (with issues)",
  failed: "Failed",
  cancelled: "Stopped",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
// Once-per-app-launch throttle for the keep-awake popup. Module scope =
// survives remounts/revisits, resets on app close/reload (fresh runtime).
const keepAwakeNotifiedSerials = new Set<string>();
function fileIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (
    [
      "jpg",
      "jpeg",
      "png",
      "gif",
      "heic",
      "heif",
      "raw",
      "tiff",
      "tif",
      "webp",
      "bmp",
      "svg",
      "cr2",
      "cr3",
      "nef",
      "arw",
      "dng",
      "avif",
      "jxl",
    ].includes(ext)
  )
    return <Image className="w-5 h-5 text-blue-400" />;
  if (["mp4", "mov", "avi", "mkv", "webm", "m4v", "3gp"].includes(ext))
    return <Film className="w-5 h-5 text-purple-400" />;
  if (["mp3", "wav", "aac", "flac", "ogg", "m4a", "wma"].includes(ext))
    return <Music className="w-5 h-5 text-green-400" />;
  if (["pdf", "doc", "docx", "txt", "md", "rtf"].includes(ext))
    return <FileText className="w-5 h-5 text-orange-400" />;
  return <FileText className="w-5 h-5 text-muted-foreground" />;
}

function formatEta(ms: number | null | "done") {
  if (ms === "done") return "--";
  if (ms === null || ms <= 0) return "Calculating...";
  const totalSec = Math.ceil(ms / 1000);
  if (totalSec < 60) return `${totalSec}s remaining`;
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (min < 60) return `${min}m ${sec}s remaining`;
  const hr = Math.floor(min / 60);
  const remMin = min % 60;
  return `${hr}h ${remMin}m remaining`;
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "";
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatElapsed(ms: number) {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const min = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0)
    return `${h}:${String(min).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${min}:${String(s).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// PreviewThumbnail — Lazy loaded thumbnail with concurrency queue
// Supports fallback chain: library thumbnail → device preview thumbnail
// ---------------------------------------------------------------------------
function PreviewThumbnail({
  itemId,
  updatedAt,
  name,
  thumbQueue,
  sourceRoot,
}: {
  itemId: number | null;
  updatedAt?: string | Date;
  name: string;
  thumbQueue: ThumbQueue;
  sourceRoot?: string | null;
}) {
  const [imgSrc, setImgSrc] = useState<string | null>(null);
  const [loadState, setLoadState] = useState<
    "idle" | "loading" | "loaded" | "error" | "generating"
  >("idle");
  const [retryCount, setRetryCount] = useState(0);
  const [isIntersecting, setIsIntersecting] = useState(false);
  const cellRef = useRef<HTMLDivElement>(null);
  const slotHeld = useRef(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      if (imgSrc) {
        URL.revokeObjectURL(imgSrc);
      }
    };
  }, [imgSrc]);

  useEffect(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    if (slotHeld.current) {
      slotHeld.current = false;
      thumbQueue.release();
    }
    setImgSrc(null);
    setLoadState("idle");
    setRetryCount(0);
    setIsIntersecting(false);
  }, [itemId, updatedAt, thumbQueue]);

  useEffect(() => {
    if (!itemId || isIntersecting) return;
    const el = cellRef.current;
    if (!el) return;

    const obs = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          setIsIntersecting(true);
          obs.unobserve(el);
        }
      },
      { rootMargin: "600px" },
    );

    obs.observe(el);
    return () => obs.disconnect();
  }, [itemId, isIntersecting]);

  // Build device preview thumbnail URL from session source info
  const buildDevicePreviewUrl = useCallback(
    (updatedAt?: string | Date) => {
      if (!sourceRoot) return null;
      const token = getLocalToken();
      const t = updatedAt ? new Date(updatedAt).getTime() : "";
      const params = new URLSearchParams({
        ...(token ? { token } : {}),
        ...(t ? { t: String(t) } : {}),
      });
      if (sourceRoot.startsWith("ios://")) {
        const raw = sourceRoot.slice("ios://".length);
        const slashIdx = raw.indexOf("/");
        const devId = slashIdx === -1 ? raw : raw.slice(0, slashIdx);
        const vPath = slashIdx === -1 ? "/" : "/" + raw.slice(slashIdx + 1);
        params.set("device_id", devId);
        params.set("path", vPath);
        return `${API_BASE_URL}/api/device/ios-thumbnail?${params}`;
      }
      params.set("path", sourceRoot);
      return `${API_BASE_URL}/api/device/thumbnail?${params}`;
    },
    [sourceRoot],
  );

  useEffect(() => {
    if (
      !itemId ||
      !isIntersecting ||
      loadState === "loaded" ||
      loadState === "error"
    )
      return;

    let cancelled = false;
    const controller = new AbortController();
    abortControllerRef.current = controller;

    const run = async () => {
      slotHeld.current = true;
      thumbQueue.request(async () => {
        if (cancelled) {
          slotHeld.current = false;
          thumbQueue.release();
          return;
        }

        setLoadState("loading");
        try {
          // First try: library thumbnail
          const libraryUrl = await fetchThumbnail(
            itemId,
            updatedAt,
            controller.signal,
          );
          if (cancelled) {
            slotHeld.current = false;
            thumbQueue.release();
            return;
          }

          if (libraryUrl) {
            setImgSrc(libraryUrl);
            setLoadState("loaded");
            slotHeld.current = false;
            thumbQueue.release();
            return;
          }

          // Library thumbnail not ready — try device preview fallback
          const deviceUrl = buildDevicePreviewUrl(updatedAt);
          if (deviceUrl) {
            setLoadState("generating");
            // Fetch device preview thumbnail directly
            const res = await fetch(deviceUrl, { signal: controller.signal });
            if (cancelled) {
              slotHeld.current = false;
              thumbQueue.release();
              return;
            }
            if (res.ok) {
              // Gray placeholders arrive as 200 with a fallback header —
              // never render them as loaded images; fall through to retry.
              const greyHeader = (res.headers.get("X-Thumb-Status") || "")
                .trim()
                .toLowerCase();
              if (!greyHeader.startsWith("fallback")) {
                const blob = await res.blob();
                if (blob.size > 0) {
                  const objectUrl = URL.createObjectURL(blob);
                  setImgSrc(objectUrl);
                  setLoadState("loaded");
                  slotHeld.current = false;
                  thumbQueue.release();
                  return;
                }
              }
            }
          }

          // Neither available yet
          slotHeld.current = false;
          thumbQueue.release();

          if (retryCount < 10) {
            timeoutRef.current = setTimeout(() => {
              if (cancelled) return;
              setRetryCount((c) => c + 1);
            }, 2000);
          } else {
            setLoadState("error");
          }
        } catch {
          slotHeld.current = false;
          thumbQueue.release();

          if (cancelled) return;

          if (retryCount < 10) {
            timeoutRef.current = setTimeout(() => {
              if (cancelled) return;
              setRetryCount((c) => c + 1);
            }, 2000);
          } else {
            setLoadState("error");
          }
        }
      });
    };

    run();

    return () => {
      cancelled = true;
      controller.abort();
      if (itemId) revokeThumbnail(itemId);
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
      if (slotHeld.current) {
        slotHeld.current = false;
        thumbQueue.release();
      }
    };
  }, [
    itemId,
    isIntersecting,
    retryCount,
    thumbQueue,
    updatedAt,
    buildDevicePreviewUrl,
  ]);

  if (!itemId) {
    return (
      <div className="w-full h-full flex items-center justify-center">
        {fileIcon(name)}
      </div>
    );
  }

  return (
    <div ref={cellRef} className="w-full h-full min-h-[80px]">
      {loadState === "loading" && (
        <div className="w-full h-20 animate-pulse bg-muted" />
      )}
      {loadState === "generating" && (
        <div className="w-full h-20 flex items-center justify-center bg-muted">
          <span className="text-[10px] text-muted-foreground flex items-center gap-1">
            <Loader2 className="w-3 h-3 animate-spin" />
            Generating preview…
          </span>
        </div>
      )}
      {imgSrc && loadState === "loaded" && (
        <img
          src={imgSrc}
          alt={name}
          className="w-full h-auto block object-cover"
        />
      )}
      {(loadState === "error" || loadState === "idle") && (
        <div className="w-full h-20 flex items-center justify-center opacity-40 bg-muted">
          {fileIcon(name)}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// PreviewPanel — Live file transfer preview from polling data
// Items are accumulated keyed by item_id so thumbnails never disappear
// once they appear — polls only add new entries to the view.
// ---------------------------------------------------------------------------
function PreviewPanel({ progress }: { progress: SessionProgress | undefined }) {
  const status = useTransferStore((s) => s.transfer.status);
  const storeSessionId = useTransferStore((s) => s.transfer.sessionId);
  const sessionId = progress?.session_id ?? storeSessionId;
  const completedItems =
    progress?.imported_files ?? progress?.completed_items ?? 0;
  const totalItems = progress?.total_files ?? progress?.total_items ?? 0;
  const isRunning = status === "running";

  // Get session source info for device preview fallback
  const sourceRoot = useTransferStore((s) => s.transfer.sourceRoot);

  const thumbQueue = useMemo(() => createThumbQueue(8), []);
  useEffect(
    () => () => {
      thumbQueue.reset();
    },
    [thumbQueue],
  );

  // Accumulate items so they stay visible once they appear.
  // Polls return the N most-recently-updated items with thumbnails,
  // which means earlier items silently fall out of the server response.
  // By accumulating in a Map keyed on item_id we preserve every item
  // that has ever been reported — new polls only ADD entries.
  const [itemMap, setItemMap] = useState<Map<number, RecentItemProgress>>(
    new Map(),
  );

  useEffect(() => {
    if (!progress) {
      setItemMap(new Map());
      return;
    }
    if (!progress.recent_items?.length) return;
    setItemMap((prev) => {
      const next = new Map(prev);
      for (const item of progress.recent_items) {
        next.set(item.item_id, item);
      }
      return next;
    });
  }, [progress]);

  // Sort by most recently updated so the current/latest item is first
  const recentFiles = useMemo(() => {
    return [...itemMap.values()]
      .sort(
        (a, b) =>
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      )
      .slice(0, 30);
  }, [itemMap]);

  // Fallback: when the backend never sent recent_items (itemMap stays empty)
  // but the session has files, fall back to vaulted/completed media items so
  // the preview is not blank after e.g. 25/25 completed. recent_items
  // accumulation above stays primary; this is only rendered when it is empty.
  const { data: fallbackData } = useMediaList({
    sessionId: sessionId ?? undefined,
    finalStatus: "completed",
    pageSize: 30,
  });

  const fallbackFiles = useMemo<RecentItemProgress[]>(() => {
    if (recentFiles.length > 0) return [];
    if (!progress) return [];
    const total = progress.total_files ?? progress.total_items ?? 0;
    if (total <= 0) return [];
    return (fallbackData?.items ?? [])
      .map((item) => ({
        item_id: item.id,
        file_name: item.file_name,
        hop1_status: item.hop1_status,
        hop2_status: item.hop2_status,
        thumbnail_url: item.thumbnail_url ?? null,
        updated_at: item.updated_at,
      }))
      .sort(
        (a, b) =>
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      )
      .slice(0, 30);
  }, [recentFiles.length, progress, fallbackData]);

  const displayFiles = recentFiles.length > 0 ? recentFiles : fallbackFiles;

  return (
    <div className="flex-1 bg-card border border-border rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
          <HardDrive className="w-4 h-4 text-primary" />
          Media Preview
        </h3>
        <span className="text-xs text-muted-foreground">
          {completedItems} / {totalItems} files
        </span>
      </div>

      <div className="p-4 h-[calc(100%-48px)] overflow-y-auto">
        {displayFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3">
            {isRunning ? (
              <>
                <Loader2 className="w-8 h-8 animate-spin opacity-40" />
                <p className="text-xs">Processing files...</p>
              </>
            ) : status === "failed" ? (
              <>
                <Image className="w-10 h-10 opacity-20" />
                <p className="text-xs text-center leading-relaxed">
                  Nothing transferred — pick your files
                  <br />
                  again from the setup preview.
                </p>
                <button
                  onClick={() =>
                    useTransferStore.getState().setCurrentPage("setup")
                  }
                  className="no-drag inline-flex items-center gap-1.5 px-3 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-normal hover:bg-primary/90 transition-colors"
                  title="Go back to setup and re-select files from the preview"
                >
                  <ArrowLeft className="w-3 h-3" />
                  Choose files again
                </button>
              </>
            ) : (
              <>
                <Image className="w-10 h-10 opacity-20" />
                <p className="text-xs">No preview available</p>
              </>
            )}
          </div>
        ) : (
          <div className="columns-3 gap-2">
            <AnimatePresence initial={false}>
              {displayFiles.map((file) => {
                const isFirst = file.item_id === displayFiles[0]?.item_id;
                return (
                  <motion.div
                    key={file.item_id}
                    initial={{ opacity: 0, scale: 0.9 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.9 }}
                    transition={{ duration: 0.18 }}
                    className={cn(
                      "break-inside-avoid mb-2 rounded-md overflow-hidden relative transition-colors",
                      isFirst
                        ? "bg-primary/10 ring-2 ring-primary"
                        : "bg-muted hover:bg-muted/80",
                    )}
                  >
                    <PreviewThumbnail
                      itemId={file.item_id}
                      updatedAt={file.updated_at}
                      name={file.file_name}
                      thumbQueue={thumbQueue}
                      sourceRoot={sourceRoot}
                    />
                    <span className="text-[10px] text-muted-foreground truncate w-full text-center px-1 py-0.5 block">
                      {file.file_name}
                    </span>
                  </motion.div>
                );
              })}
            </AnimatePresence>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TransferCompleteSummary — Post-transfer confirmation
// ---------------------------------------------------------------------------
interface TransferCompleteSummaryProps {
  completedItems: number;
  failedItems: number;
  totalBytesVolume: number | null | undefined;
  transferMode: "copy" | "move";
  destRoot: string;
}

function TransferCompleteSummary({
  completedItems,
  failedItems,
  totalBytesVolume,
  transferMode,
  destRoot,
}: TransferCompleteSummaryProps) {
  const hasSuccess = completedItems > 0;
  const sizeText =
    totalBytesVolume != null ? ` (${formatBytes(totalBytesVolume)})` : "";

  return (
    <div className="p-3 bg-muted/50 rounded-md space-y-2">
      {hasSuccess ? (
        <div className="flex items-start gap-2.5">
          <VerifiedCheckBadge size="lg" className="mt-0.5" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <p className="text-sm font-semibold text-foreground">
                {completedItems} file{completedItems !== 1 ? "s" : ""}
                {sizeText} safely backed up and verified
              </p>
              <VerifiedCheckBadge
                size="sm"
                showLabel
                labelText="BLAKE3 Verified"
                className="text-[10px]"
              />
            </div>
            {failedItems > 0 && (
              <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
                {failedItems} file{failedItems !== 1 ? "s" : ""} could not be
                verified — see details below
              </p>
            )}
            <p className="text-xs text-muted-foreground mt-1.5 leading-relaxed">
              {transferMode === "copy"
                ? `These files have been copied and verified at ${destRoot}. It is now safe to delete them from your device if you'd like to free up space.`
                : "These files have been moved and verified — they were already removed from your device automatically."}
            </p>
          </div>
        </div>
      ) : (
        <div className="flex items-start gap-2.5">
          <AlertTriangle className="w-5 h-5 text-amber-500 mt-0.5 shrink-0" />
          <div>
            <p className="text-sm font-semibold text-foreground">
              No files were successfully transferred
            </p>
            <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
              {failedItems} file{failedItems !== 1 ? "s" : ""} could not be
              verified — see details below
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Transfer failure helpers — short actionable copy up front, technical
// detail (raw URIs, cutoff, serial) only inside the Details expander.
// ---------------------------------------------------------------------------
type FailureKind =
  "walk-empty" | "cutoff" | "stale-selection" | "no-media" | "generic";

function parseFailureKind(message: string): FailureKind {
  const m = (message || "").toLowerCase();
  if (
    m.includes("0 files listed") ||
    m.includes("couldn't read any files") ||
    m.includes("could not read any files")
  )
    return "walk-empty";
  if (
    m.includes("since the last import") ||
    m.includes("cutoff active") ||
    m.includes("no new files")
  )
    return "cutoff";
  if (
    m.includes("matched files on the device") ||
    m.includes("re-select them from the preview") ||
    m.includes("re-select")
  )
    return "stale-selection";
  if (m.includes("no media files found")) return "no-media";
  return "generic";
}

function friendlySourceLabel(sourceRoot: string): string {
  if (!sourceRoot) return "this source";
  if (sourceRoot.startsWith("ios://")) return "your iPhone";
  if (sourceRoot.startsWith("wpd://")) return "your device";
  // Local folder: show just the folder name, not the whole path.
  const trimmed = sourceRoot.replace(/[/\\]+$/, "");
  const parts = trimmed.split(/[/\\]/);
  const last = parts[parts.length - 1];
  return last ? `“${last}”` : "this folder";
}

function parseDeviceSerial(sourceRoot: string): string | null {
  if (!sourceRoot.startsWith("ios://") && !sourceRoot.startsWith("wpd://"))
    return null;
  const without = sourceRoot.split("://")[1] ?? "";
  const serial = without.split("/")[0];
  return serial || null;
}

// ---------------------------------------------------------------------------
// TransferFailureCard — 2-line summary + action buttons.
// Raw error text / URIs / cutoff / serial live only in <details>.
// ---------------------------------------------------------------------------
function TransferFailureCard({
  session,
  sourceRoot,
  onBackToSetup,
  onViewReport,
  hasReport,
}: {
  session: ReturnType<typeof useSession>["data"];
  sourceRoot: string;
  onBackToSetup: () => void;
  onViewReport: () => void;
  hasReport: boolean;
}) {
  const [forgetConfirm, setForgetConfirm] = useState(false);
  const clearDeviceState = useClearDeviceImportState();
  const raw = (session as unknown as Record<string, unknown>) ?? {};
  // Backend contract: error_message is the short user-facing text, and
  // error_debug_detail carries machine detail. Fall back to the legacy
  // report-JSON keys (user_message/debug_detail) when present.
  const userMessage =
    (session?.error_message ?? "") ||
    (typeof raw["user_message"] === "string"
      ? (raw["user_message"] as string)
      : "");
  const debugDetail =
    (session as unknown as { error_debug_detail?: string | null } | undefined)
      ?.error_debug_detail ||
    (typeof raw["debug_detail"] === "string" && raw["debug_detail"]
      ? (raw["debug_detail"] as string)
      : null) ||
    null;
  // Scan diagnostics live only in <details> — never in the 2-line summary.
  const scan = session as unknown as
    | {
        scan_scanned_total?: number | null;
        scan_skipped_by_cutoff?: number | null;
        scan_walk_total?: number | null;
        scan_cutoff_iso?: string | null;
        scan_serial_verdict?: string | null;
      }
    | null
    | undefined;
  const scanLines: string[] = [];
  if (
    typeof scan?.scan_scanned_total === "number" &&
    scan.scan_scanned_total > 0
  )
    scanLines.push(`scanned: ${scan.scan_scanned_total}`);
  if (typeof scan?.scan_walk_total === "number" && scan.scan_walk_total > 0)
    scanLines.push(`walked: ${scan.scan_walk_total}`);
  if (
    typeof scan?.scan_skipped_by_cutoff === "number" &&
    scan.scan_skipped_by_cutoff > 0
  )
    scanLines.push(`skipped by cutoff: ${scan.scan_skipped_by_cutoff}`);
  if (scan?.scan_cutoff_iso) scanLines.push(`cutoff: ${scan.scan_cutoff_iso}`);
  if (scan?.scan_serial_verdict)
    scanLines.push(`serial: ${scan.scan_serial_verdict}`);
  const scanDetail = scanLines.length > 0 ? scanLines.join(" · ") : null;
  const kind = parseFailureKind(userMessage);
  const sourceLabel = friendlySourceLabel(sourceRoot);
  const serial = parseDeviceSerial(sourceRoot);

  let title = "This transfer couldn't finish.";
  let hint = "Check the details below, then go back to setup and try again.";
  if (kind === "walk-empty") {
    title = "Couldn't reach your iPhone — it may be locked.";
    hint =
      "Unlock it, tap Trust if asked, then check the preview and try again.";
  } else if (kind === "cutoff") {
    title = "Nothing new since your last import.";
    hint =
      "Import everything anyway, or forget the last import and start fresh.";
  } else if (kind === "stale-selection") {
    title = "Those selected files didn't match this time.";
    hint = "Pick them again from the preview — the list may have changed.";
  } else if (kind === "no-media") {
    title = `No photos or videos found in ${sourceLabel}.`;
    hint = "Try a deeper folder, or import everything below it.";
  }

  const goSetupFullImport = () => {
    const st = useTransferStore.getState();
    st.setSetupOnlyNewMode(false);
    onBackToSetup();
  };

  const handleForget = () => {
    if (!serial) {
      goSetupFullImport();
      return;
    }
    if (!forgetConfirm) {
      setForgetConfirm(true);
      return;
    }
    clearDeviceState.mutate(serial, {
      onSuccess: () => {
        useTransferStore.getState().setSetupOnlyNewMode(false);
        setForgetConfirm(false);
        onBackToSetup();
      },
      onError: () => setForgetConfirm(false),
    });
  };

  return (
    <div className="p-3 bg-red-500/5 border border-red-200/20 dark:border-red-900/20 rounded-md space-y-2">
      <p className="text-xs font-semibold text-red-600 dark:text-red-400 flex items-center gap-1.5">
        <AlertCircle className="w-3.5 h-3.5 shrink-0" />
        {title}
      </p>
      <p className="text-[11px] text-muted-foreground leading-relaxed">
        {hint}
      </p>

      <div className="flex items-center gap-2 pt-1 flex-wrap">
        {kind === "cutoff" ? (
          <>
            <button
              onClick={goSetupFullImport}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-primary text-primary-foreground rounded text-xs font-normal hover:bg-primary/90 transition-colors"
              title="Go back to setup with full-import mode so everything copies again"
            >
              <Upload className="w-3 h-3" />
              Import anyway
            </button>
            <button
              onClick={handleForget}
              disabled={clearDeviceState.isPending}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-normal hover:bg-secondary/80 transition-colors disabled:opacity-50"
              title="Clear the last-import marker so the next run scans everything"
            >
              {clearDeviceState.isPending ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <RefreshCw className="w-3 h-3" />
              )}
              {forgetConfirm ? "Confirm forget?" : "Forget last import"}
            </button>
          </>
        ) : kind === "walk-empty" ? (
          <button
            onClick={onBackToSetup}
            className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-primary text-primary-foreground rounded text-xs font-normal hover:bg-primary/90 transition-colors"
            title="Go back to setup — the preview re-scans the device first"
          >
            <RefreshCw className="w-3 h-3" />
            Check preview &amp; retry
          </button>
        ) : kind === "stale-selection" ? (
          <button
            onClick={onBackToSetup}
            className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-primary text-primary-foreground rounded text-xs font-normal hover:bg-primary/90 transition-colors"
            title="Go back to setup and pick files again from the preview"
          >
            <ArrowLeft className="w-3 h-3" />
            Re-select from preview
          </button>
        ) : (
          <button
            onClick={onBackToSetup}
            className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-primary text-primary-foreground rounded text-xs font-normal hover:bg-primary/90 transition-colors"
            title="Return to setup with the same source and destination"
          >
            <ArrowLeft className="w-3 h-3" />
            Back to setup
          </button>
        )}
        {hasReport && (
          <button
            onClick={onViewReport}
            className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-normal hover:bg-secondary/80 transition-colors"
            title="Open HTML report"
          >
            <FileText className="w-3 h-3" />
            Open report
          </button>
        )}
      </div>

      <details className="pt-1 group">
        <summary className="text-[11px] text-muted-foreground hover:text-foreground cursor-pointer select-none transition-colors">
          Details
        </summary>
        <div className="mt-1.5 p-2 bg-muted/50 rounded text-[10px] font-mono text-muted-foreground break-words whitespace-pre-wrap leading-relaxed">
          {userMessage || "No additional error details were reported."}
          {debugDetail && (
            <span className="block mt-1.5 pt-1.5 border-t border-border">
              {debugDetail}
            </span>
          )}
          {scanDetail && (
            <span className="block mt-1 opacity-80">{scanDetail}</span>
          )}
          {session?.id != null && (
            <span className="block mt-1 opacity-70">session #{session.id}</span>
          )}
        </div>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TransferMonitor — Performance stats and progress (polling-based)
// ---------------------------------------------------------------------------
function TransferMonitor(_props: { progress: SessionProgress | undefined }) {
  const transfer = useTransferStore((s) => s.transfer);
  const wsConnected = useTransferStore((s) => s.wsConnected);
  const { sessionId } = transfer;
  const { data: session } = useSession(sessionId);
  const { data: batches } = useSessionBatches(sessionId);

  // Fetch failed items
  const { data: failedItemsData } = useMediaList({
    sessionId: sessionId ?? undefined,
    finalStatus: "failed",
    pageSize: 100,
  });

  const activeBatch = transfer.activeBatch;
  const rawTotal = transfer.totalFiles || transfer.totalItems || 0;
  const isEmptySession = rawTotal === 0;
  // Display denominator only — never used as a divisor. Shows 0 honestly
  // for empty sessions instead of masking it with a fallback of 1.
  const totalFiles = rawTotal;
  const isCancelled = transfer.status === "cancelled";
  const isCompleted = transfer.status === "completed";
  const isCompletedWithErrors = transfer.status === "completed_with_errors";
  const isFailed = transfer.status === "failed";
  const isPaused = transfer.status === "paused";
  const isTerminal = [
    isCompleted,
    isCompletedWithErrors,
    isFailed,
    isCancelled,
  ].some(Boolean);

  // Failure reason is rendered by TransferFailureCard (2-line summary +
  // actions, technical detail behind a Details expander).

  // Follow SessionRow handleViewReport pattern in DashboardPage:
  // desktop opens the report path directly, browser opens the report endpoint.
  const handleViewReport = () => {
    const reportPath = session?.session_report_path;
    if (!reportPath) return;
    if (isDesktop) {
      openPath(reportPath);
    } else {
      const token = getLocalToken();
      const tokenParam = token ? `&token=${encodeURIComponent(token)}` : "";
      const sid = session?.id ?? sessionId;
      if (sid == null) return;
      window.open(
        `/api/sessions/${sid}/report?fmt=html${tokenParam}`,
        "_blank",
      );
    }
  };

  // Recovery path: seed setup with this transfer's roots, then go to setup.
  // Uses existing store actions only — no new store fields.
  const handleBackToSetup = () => {
    const st = useTransferStore.getState();
    if (transfer.sourceRoot) st.setSetupSourcePath(transfer.sourceRoot);
    if (transfer.destRoot) st.setSetupDestPath(transfer.destRoot);
    st.setCurrentPage("setup");
  };

  // Use server-provided progress_percent for the main bar
  // On completion: animate to 100% — unless the session was empty, in
  // which case forcing 100% is misleading, so stay at 0%.
  // On cancel/failure: freeze at last value.
  let displayPct = transfer.progressPercent ?? 0;
  if ((isCompleted || isCompletedWithErrors) && !isEmptySession) {
    displayPct = 100;
  }
  if (isEmptySession) {
    displayPct = 0;
  }

  // Use server-computed timing values (updated each poll)
  const elapsedMs = transfer.elapsed;
  const speed = transfer.speed;
  const etaMs = isTerminal ? "done" : transfer.eta;

  // Determine phase text
  let phaseText = "";
  if (isEmptySession && !isFailed && !isCancelled)
    phaseText = "No files to transfer — empty session";
  else if (transfer.status === "created") phaseText = "Ready to start";
  else if (transfer.status === "running" && activeBatch) {
    if (activeBatch.hop1Status === "transferring")
      phaseText = `Caching file ${activeBatch.completedItems} of ${activeBatch.totalItems}`;
    else if (
      activeBatch.hop1Status === "completed" &&
      activeBatch.hop2Status !== "completed"
    )
      phaseText = `Importing file ${activeBatch.completedItems} of ${activeBatch.totalItems}`;
    else phaseText = `Processing batch ${activeBatch.batchNumber}...`;
  } else if (transfer.status === "running") phaseText = "Processing...";
  else if (transfer.status === "paused") phaseText = "Paused";
  else if (isCompleted) phaseText = "Transfer complete";
  else if (isCompletedWithErrors) phaseText = "Transfer completed with errors";
  else if (isFailed) phaseText = "Transfer failed";
  else if (isCancelled) phaseText = "Transfer cancelled";
  else phaseText = transfer.status;

  // Sync progress to Windows taskbar overlay via tray IPC (Tauri shell only)
  useEffect(() => {
    if (!isDesktop) return;
    if (isTerminal) {
      if (isFailed) {
        setTrayProgress(1, "error");
      } else {
        setTrayProgress(null);
      }
    } else if (transfer.status === "running" || transfer.status === "paused") {
      const fraction = Math.max(
        0,
        Math.min(1, (transfer.progressPercent ?? 0) / 100),
      );
      setTrayProgress(
        fraction,
        transfer.status === "paused" ? "paused" : "normal",
      );
    }
  }, [transfer.progressPercent, transfer.status, isTerminal, isFailed]);

  return (
    <div className="w-80 bg-card border border-border rounded-lg flex flex-col">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
          <Zap className="w-4 h-4 text-primary" />
          Transfer Monitor
          {!wsConnected && transfer.status === "running" && (
            <span className="ml-auto text-[10px] text-muted-foreground">
              (polling)
            </span>
          )}
        </h3>
      </div>

      <div className="flex-1 p-4 space-y-4 overflow-y-auto">
        {/* Overall Progress — cumulative, never resets between batches */}
        <div>
          <div className="flex justify-between text-xs text-muted-foreground mb-1">
            <span>Overall Progress</span>
            <span className="font-semibold text-foreground">{displayPct}%</span>
          </div>
          <div className="w-full h-3 bg-muted rounded-full overflow-hidden">
            <motion.div
              className={cn(
                "h-full rounded-full transition-colors",
                isCompleted
                  ? "bg-green-500"
                  : isCompletedWithErrors
                    ? "bg-amber-500"
                    : isFailed
                      ? "bg-red-500"
                      : "bg-primary",
              )}
              initial={{ width: 0 }}
              animate={{ width: `${displayPct}%` }}
              transition={{ duration: isTerminal ? 0.8 : 0.3 }}
            />
          </div>
          <div className="flex justify-between text-[11px] text-muted-foreground mt-1">
            <span>
              Cached {transfer.cachedFiles} / {totalFiles}
              {" · "}
              Transferred {transfer.importedFiles} / {totalFiles}
            </span>
            <span>{transfer.failedFiles} failed</span>
          </div>
          {transfer.totalBatches > 0 && (
            <div className="text-[10px] text-muted-foreground mt-0.5">
              Batch {transfer.currentBatch} of {transfer.totalBatches}
            </div>
          )}
        </div>

        {/* Phase / Status — summary on completion, phase text otherwise */}
        {isCompleted || isCompletedWithErrors ? (
          <TransferCompleteSummary
            completedItems={transfer.importedFiles}
            failedItems={transfer.failedFiles}
            totalBytesVolume={session?.total_bytes_volume}
            transferMode={transfer.transferMode as "copy" | "move"}
            destRoot={transfer.destRoot}
          />
        ) : (
          <div className="p-3 bg-muted/50 rounded-md">
            <p className="text-xs font-normal text-foreground">{phaseText}</p>
            {transfer.currentFileName && (
              <p className="text-[11px] text-muted-foreground mt-1 truncate">
                {transfer.currentFileName}
              </p>
            )}
          </div>
        )}

        {/* Failed details — 2-line summary + actions, tech behind Details */}
        {isFailed && (
          <TransferFailureCard
            session={session}
            sourceRoot={transfer.sourceRoot}
            onBackToSetup={handleBackToSetup}
            onViewReport={handleViewReport}
            hasReport={Boolean(session?.session_report_path)}
          />
        )}

        {/* Step Progress */}
        {activeBatch && (
          <div className="space-y-3">
            <div className="p-3 bg-muted/50 rounded-md">
              <div className="flex items-center justify-between mb-1">
                <span
                  className="text-xs font-normal text-foreground"
                  title="Hop 1: Reading files into temporary cache with BLAKE3 checksum"
                >
                  Step 1: Reading files
                </span>
                <span className="text-xs text-muted-foreground">
                  {activeBatch.hop1Progress}%
                </span>
              </div>
              <div className="w-full h-1.5 bg-muted rounded-full overflow-hidden">
                <motion.div
                  className="h-full bg-action rounded-full"
                  animate={{ width: `${activeBatch.hop1Progress}%` }}
                />
              </div>
            </div>

            <div className="p-3 bg-muted/50 rounded-md">
              <div className="flex items-center justify-between mb-1">
                <span
                  className="text-xs font-normal text-foreground"
                  title="Hop 2: Verifying checksums and saving safely to destination"
                >
                  Step 2: Verifying &amp; saving
                </span>
                <span className="text-xs text-muted-foreground">
                  {activeBatch.hop2Progress}%
                </span>
              </div>
              <div className="w-full h-1.5 bg-muted rounded-full overflow-hidden">
                <motion.div
                  className="h-full bg-green-500 rounded-full"
                  animate={{ width: `${activeBatch.hop2Progress}%` }}
                />
              </div>
            </div>
          </div>
        )}

        {/* Stats */}
        <div className="grid grid-cols-2 gap-2">
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
              Speed
            </p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {transfer.status === "running" && speed === 0 && elapsedMs < 2000
                ? "Starting..."
                : transfer.status === "running" && speed === 0
                  ? "Calculating..."
                  : speed > 0
                    ? speed >= 1.0
                      ? `${speed.toFixed(1)} files/sec`
                      : `${(speed * 60).toFixed(0)} files/min`
                    : "--"}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
              ETA
            </p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {formatEta(etaMs)}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
              Elapsed
            </p>
            <p className="text-sm font-semibold text-foreground mt-0.5 flex items-center gap-1.5">
              {transfer.status === "created" || elapsedMs === 0
                ? "--"
                : formatElapsed(elapsedMs)}
              {isPaused && (
                <span className="text-[10px] font-bold text-amber-500 uppercase">
                  Paused
                </span>
              )}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
              Status
            </p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {STATUS_LABELS[transfer.status] || transfer.status}
            </p>
          </div>
        </div>

        {/* Batch List */}
        {batches && batches.batches.length > 0 && (
          <div>
            <p className="text-xs font-semibold text-foreground mb-2">
              Batches
            </p>
            <div className="space-y-1.5">
              {batches.batches.map((b) => (
                <div
                  key={b.id}
                  className="flex items-center justify-between text-xs px-2 py-1.5 bg-muted/50 rounded"
                >
                  <span className="text-foreground">
                    Batch {b.batch_number}
                  </span>
                  <div className="flex items-center gap-1.5">
                    <span className="text-muted-foreground">
                      {b.completed_items}/{b.total_items}
                    </span>
                    {b.status === "completed" ? (
                      <CheckCircle2 className="w-3 h-3 text-green-500" />
                    ) : b.status === "processing" ? (
                      <Loader2 className="w-3 h-3 text-blue-500 animate-spin" />
                    ) : b.status === "failed" ? (
                      <AlertCircle className="w-3 h-3 text-red-500" />
                    ) : (
                      <Clock className="w-3 h-3 text-muted-foreground" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Failed Items list */}
        {failedItemsData && failedItemsData.items.length > 0 && (
          <div className="space-y-2 mt-4 pt-3 border-t border-border">
            <p className="text-xs font-semibold text-red-600 dark:text-red-400 flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5" />
              Failed Items ({failedItemsData.items.length})
            </p>
            <div className="max-h-48 overflow-y-auto space-y-1.5 border border-red-200/20 dark:border-red-900/20 rounded-md p-2.5 bg-red-500/5 dark:bg-red-500/5">
              {failedItemsData.items.map((item) => (
                <div key={item.id} className="text-[11px] leading-relaxed">
                  <div
                    className="font-semibold text-foreground truncate"
                    title={item.file_name}
                  >
                    {item.file_name}
                  </div>
                  <div className="text-red-600 dark:text-red-400 break-words font-mono text-[10px] mt-0.5">
                    {item.error_message || "Unknown error"}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TransferPage
// ---------------------------------------------------------------------------
export default function TransferPage() {
  const transfer = useTransferStore((s) => s.transfer);
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage);
  const initTransfer = useTransferStore((s) => s.initTransfer);
  const updateFromPolling = useTransferStore((s) => s.updateFromPolling);
  const resetTransfer = useTransferStore((s) => s.resetTransfer);
  const setCompletedSnapshot = useTransferStore((s) => s.setCompletedSnapshot);
  const completedSnapshot = useTransferStore((s) => s.ui.completedSnapshot);
  const duplicates = useTransferStore((s) => s.duplicates);

  const { data: session } = useSession(transfer.sessionId);
  const { data: progress } = useSessionProgress(transfer.sessionId);
  const startSession = useStartSession();
  const pauseSession = usePauseSession();
  const cancelSession = useCancelSession();
  // Centralized engine-readiness flag — mutation buttons grey out + tooltip
  // while the engine starts; navigation and cached views stay interactive.
  const backendReady = useBackendReady();

  const [confirmCancel, setConfirmCancel] = useState(false);
  const cancelTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const initialisedRef = useRef(false);
  const autoStartedRef = useRef(false);
  // Connect to WS for real-time events (bonus, not required)
  useTransferWs(transfer.sessionId);

  // Cleanup timers on unmount — do NOT reset transfer state
  useEffect(() => {
    return () => {
      if (cancelTimerRef.current) clearTimeout(cancelTimerRef.current);
      // NOTE: Intentionally NOT calling resetTransfer() here.
      // The completed transfer state is preserved so the user can navigate back
      // to this page and review their transfer results.
    };
  }, []);

  // Sync session data into store once on mount (initial values from REST)
  useEffect(() => {
    if (session && !initialisedRef.current) {
      initialisedRef.current = true;
      initTransfer(session);
    }
  }, [session, initTransfer]);

  // Auto-start: when a fresh session (status 'created') is loaded, immediately
  // call the start endpoint so the user doesn't need to click "Start" manually.
  useEffect(() => {
    if (
      session &&
      initialisedRef.current &&
      session.status === "created" &&
      transfer.sessionId &&
      !startedSessions.has(transfer.sessionId)
    ) {
      startedSessions.add(transfer.sessionId);
      autoStartedRef.current = true;
      const sid = transfer.sessionId;
      // If the start request fails, release the guards so a manual
      // retry via the Start button is possible (and the id doesn't
      // leak in the module-level set forever).
      startSession.mutate(sid, {
        onError: () => {
          startedSessions.delete(sid);
          autoStartedRef.current = false;
        },
      });
    }
  }, [session, transfer.sessionId, startSession]);

  // Prune finished session ids from the module-level guard set so it
  // can't grow without bound across many transfers in one app lifetime.
  useEffect(() => {
    if (
      transfer.sessionId &&
      (transfer.status === "completed" ||
        transfer.status === "completed_with_errors")
    ) {
      startedSessions.delete(transfer.sessionId);
    }
  }, [transfer.sessionId, transfer.status]);

  // Capture the completed snapshot when a transfer reaches a terminal state.
  // This freezes the final state into the store so the page shows results even
  // after navigating away and back.
  useEffect(() => {
    if (
      transfer.status === "completed" ||
      transfer.status === "completed_with_errors" ||
      transfer.status === "failed"
    ) {
      setCompletedSnapshot({ ...transfer });

      if (
        transfer.sessionId &&
        (transfer.status === "completed" ||
          transfer.status === "completed_with_errors")
      ) {
        useTransferStore
          .getState()
          .setLastCompletedSessionId(transfer.sessionId);
      }

      if (!useTransferStore.getState().ui.notification) {
        if (transfer.status === "completed") {
          // Suppress the misleading "0 files transferred successfully"
          // toast for empty (0-item) sessions.
          const total = transfer.totalFiles || transfer.totalItems || 0;
          if (transfer.importedFiles > 0 || total > 0) {
            useTransferStore
              .getState()
              .showNotification(
                "success",
                `${transfer.importedFiles} file${transfer.importedFiles !== 1 ? "s" : ""} transferred successfully.`,
              );
          }
        } else if (transfer.status === "completed_with_errors") {
          useTransferStore
            .getState()
            .showNotification(
              "warning",
              `${transfer.importedFiles} transferred, ${transfer.failedFiles} failed.`,
            );
        } else if (transfer.status === "failed") {
          useTransferStore
            .getState()
            .showNotification("error", "Check the errors.");
        }
      }
    }
  }, [
    transfer.status,
    transfer.sessionId,
    transfer.importedFiles,
    transfer.failedFiles,
    transfer.totalFiles,
    transfer.totalItems,
    setCompletedSnapshot,
  ]);

  // Sync polling data into store (authoritative source for all live fields).
  // No auto-navigation — the snapshot-capture effect handles terminal states.
  useEffect(() => {
    if (!progress) return;
    updateFromPolling(progress);
  }, [progress, updateFromPolling]);

  // On mount: if there's no active session but there IS a completed snapshot,
  // restore it so the page shows the last transfer result.
  useEffect(() => {
    if (!transfer.sessionId && completedSnapshot && !initialisedRef.current) {
      initialisedRef.current = true;
    }
  }, [transfer.sessionId, completedSnapshot]);

  // Esc key to go back (safe guard for mid-transfer)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (transfer.status === "running") {
          return;
        }
        handleBack();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [transfer.status]);

  const handleBack = () => {
    // Do NOT reset transfer state — user can come back to see results
    setCurrentPage("dashboard");
  };

  const handleStart = () => {
    // Terminal states (failed/completed/cancelled) cannot be started via
    // POST /sessions/{id}/start (backend 400s) — guide back to setup instead
    // of firing a doomed request. Only created (start) and paused (resume)
    // are valid start sources.
    if (transfer.status !== "created" && transfer.status !== "paused") {
      useTransferStore
        .getState()
        .showNotification(
          "info",
          transfer.status === "failed"
            ? "This transfer failed and can't be resumed. Go back to setup to start a new transfer."
            : "This transfer has already finished. Go back to setup to start a new transfer.",
        );
      return;
    }
    if (transfer.sessionId) {
      // If auto-start already initiated for a fresh (created) session, don't
      // double-start. Paused sessions (Resume) are unaffected by this guard.
      if (
        transfer.status === "created" &&
        (autoStartedRef.current || startedSessions.has(transfer.sessionId))
      )
        return;
      if (transfer.status === "created") {
        startedSessions.add(transfer.sessionId);
      }
      const sid = transfer.sessionId;
      startSession.mutate(sid, {
        onError: () => {
          // Release the auto-start guards so the user can retry.
          startedSessions.delete(sid);
          autoStartedRef.current = false;
        },
      });
    }
  };

  const handlePause = () => {
    if (transfer.sessionId) pauseSession.mutate(transfer.sessionId);
  };

  const handleCancel = async () => {
    if (!confirmCancel) {
      setConfirmCancel(true);
      cancelTimerRef.current = setTimeout(() => setConfirmCancel(false), 5000);
      return;
    }
    if (cancelTimerRef.current) clearTimeout(cancelTimerRef.current);
    setConfirmCancel(false);
    if (transfer.sessionId) {
      await cancelSession.mutateAsync(transfer.sessionId);
      resetTransfer();
      setCurrentPage("dashboard");
    }
  };

  const isRunning = transfer.status === "running";
  const isPaused = transfer.status === "paused";
  const isIdle = transfer.status === "created";
  // No session loaded (fresh navigation): action buttons would be inert —
  // hide them instead of rendering dead controls.
  const hasSession = transfer.sessionId !== null;
  const isFinished = [
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
  ].includes(transfer.status);

  // Show keep-awake reminder for iOS device transfers
  const isIOSDevice = transfer.sourceRoot?.startsWith("ios://") ?? false;
  const showKeepAwake = isIOSDevice && (isRunning || isPaused);

  // Canonical device serial from an ios://<serial>/… source root (mirrors
  // backend canonical_device_serial: embedded 40-hex UDID wins, else
  // lowercased raw id). Used for the once-per-app-launch popup throttle.
  const iosSerial = useMemo(() => {
    if (!transfer.sourceRoot?.startsWith("ios://")) return null;
    const raw = transfer.sourceRoot.slice("ios://".length);
    const slashIdx = raw.indexOf("/");
    const devId = (slashIdx === -1 ? raw : raw.slice(0, slashIdx)).trim();
    if (!devId) return null;
    const m = devId.match(/[0-9a-fA-F]{40}/);
    return (m?.[0] ?? devId).toLowerCase();
  }, [transfer.sourceRoot]);

  const keepAwakeNotifiedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!showKeepAwake || !iosSerial) return;
    if (keepAwakeNotifiedRef.current === iosSerial) return;
    keepAwakeNotifiedRef.current = iosSerial;
    if (keepAwakeNotifiedSerials.has(iosSerial)) return;
    keepAwakeNotifiedSerials.add(iosSerial);
    // The inline keep-awake checklist strip above stays always visible.
    useTransferStore
      .getState()
      .showNotification(
        "warning",
        "Keep your iPhone unlocked — preview and transfer pause if it locks. Your phone's auto-lock can't be turned off from this PC, so tap the screen now and then to keep it awake.",
      );
  }, [showKeepAwake, iosSerial]);

  return (
    <div className="h-full flex flex-col space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            onClick={handleBack}
            className="no-drag p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold text-foreground">
                {transfer.sessionName || "Transfer"}
              </h1>
              {transfer.status === "completed" && (
                <VerifiedCheckBadge size="sm" showLabel labelText="Verified" />
              )}
            </div>
            <div className="flex items-center gap-2 mt-0.5">
              <p className="text-xs text-muted-foreground">
                {transfer.sourceRoot.startsWith("ios://")
                  ? "iPhone"
                  : transfer.sourceRoot.startsWith("wpd://")
                    ? "Device"
                    : transfer.sourceRoot}{" "}
                {"->"} {transfer.destRoot}
              </p>
              <span
                className={cn(
                  "text-[10px] font-mono px-1.5 py-0.5 rounded",
                  transfer.transferMode === "copy"
                    ? "bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300"
                    : "bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300",
                )}
              >
                {transfer.transferMode === "copy" ? "COPY" : "MOVE"}
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {isIdle && hasSession && (
            <button
              onClick={handleStart}
              disabled={startSession.isPending || !backendReady}
              title={backendReady ? undefined : ENGINE_WAITING_LABEL}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-normal hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {startSession.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <>
                  <Play className="w-4 h-4" />
                  Start
                </>
              )}
            </button>
          )}
          {isRunning && (
            <button
              onClick={handlePause}
              disabled={pauseSession.isPending || !backendReady}
              title={backendReady ? undefined : ENGINE_WAITING_LABEL}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-amber-500 text-white rounded-md text-sm font-normal hover:bg-amber-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Pause className="w-4 h-4" />
              Pause
            </button>
          )}
          {isPaused && (
            <button
              onClick={handleStart}
              disabled={startSession.isPending || !backendReady}
              title={backendReady ? undefined : ENGINE_WAITING_LABEL}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-normal hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {startSession.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <>
                  <Play className="w-4 h-4" />
                  Resume
                </>
              )}
            </button>
          )}
          {!isFinished && hasSession && (
            <button
              onClick={handleCancel}
              disabled={cancelSession.isPending || !backendReady}
              title={
                !backendReady
                  ? ENGINE_WAITING_LABEL
                  : confirmCancel
                    ? "Click again to confirm stopping transfer"
                    : "Stop transfer"
              }
              className={cn(
                "no-drag inline-flex items-center gap-1.5 px-3 py-2 rounded-md text-sm font-normal transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
                confirmCancel
                  ? "bg-red-600 text-white hover:bg-red-700"
                  : "bg-destructive text-destructive-foreground hover:bg-destructive/90",
              )}
            >
              <X className="w-4 h-4" />
              <span>{confirmCancel ? "Confirm stop?" : "Cancel"}</span>
            </button>
          )}
          {isFinished && (
            <div className="flex items-center gap-2">
              {isDesktop && transfer.destRoot && (
                <button
                  onClick={() => openPath(transfer.destRoot)}
                  className="no-drag inline-flex items-center gap-1.5 px-3.5 py-2 bg-secondary text-secondary-foreground rounded-pill text-sm font-normal hover:bg-secondary/80 transition-colors active:scale-[0.95]"
                  title="Open destination folder on this computer"
                >
                  <FolderOpen className="w-4 h-4" />
                  Open Folder
                </button>
              )}
              <button
                onClick={() => setCurrentPage("library")}
                className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-action text-white rounded-pill text-sm font-normal hover:bg-action/90 transition-colors active:scale-[0.95]"
              >
                View Library
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Duplicates Paused Banner */}
      <AnimatePresence>
        {isPaused && duplicates.report && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="flex items-center gap-2.5 px-4 py-2.5 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg text-sm text-amber-700 dark:text-amber-300">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              <span className="flex-1">
                Duplicate files detected — {duplicates.report.summary}.
              </span>
              {!duplicates.isOpen && (
                <button
                  onClick={() =>
                    useTransferStore
                      .getState()
                      .openDuplicates(duplicates.report!)
                  }
                  className="no-drag px-3 py-1 bg-amber-600 text-white rounded-md text-xs font-normal hover:bg-amber-700 transition-colors"
                >
                  Review
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* iOS paused — locked means paused, not failed; resume reuses session */}
      <AnimatePresence>
        {isPaused && isIOSDevice && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="flex items-start gap-2.5 px-4 py-3 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg text-sm">
              <AlertTriangle className="w-4 h-4 shrink-0 text-amber-500 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="font-semibold text-amber-700 dark:text-amber-300 text-sm">
                  Transfer paused — not failed
                </p>
                <p className="text-xs text-amber-700/80 dark:text-amber-300/80 mt-0.5 leading-relaxed">
                  If your iPhone locked, unlock it and tap Trust once, then hit
                  Resume. Pauses, not failed — Resume reuses the session and
                  skips completed files (current file restarts; preview
                  re-scans).
                </p>
                <ul className="text-[11px] text-amber-700/80 dark:text-amber-300/80 mt-1.5 space-y-0.5 leading-snug list-disc list-inside">
                  <li>Tap the screen now and then to keep it awake</li>
                  <li>
                    iPhone Settings &gt; Display &gt; Auto-Lock &gt; Never
                    (while transferring)
                  </li>
                  <li>Use a good cable + rear USB port</li>
                  <li>Tap Trust once when asked</li>
                </ul>
                <p className="text-[11px] text-amber-700/70 dark:text-amber-300/70 mt-1.5">
                  Brief hiccups retry on their own — only a lock or cable pull
                  pauses the transfer.
                </p>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* iOS running — keep-awake checklist strip */}
      <AnimatePresence>
        {showKeepAwake && !isPaused && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="flex items-start gap-2.5 px-4 py-2.5 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg text-sm">
              <Clock className="w-4 h-4 shrink-0 text-blue-500 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-blue-700 dark:text-blue-300">
                  Keep your iPhone awake
                </p>
                <p className="text-[11px] text-blue-600 dark:text-blue-400 mt-0.5 leading-relaxed">
                  Tap the screen now and then · Settings &gt; Display &gt;
                  Auto-Lock &gt; Never · good cable + rear USB port · tap Trust
                  once. If the phone locks, it pauses, not failed — Resume
                  reuses the session and skips completed files (current file
                  restarts; preview re-scans).
                </p>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Content */}
      <div className="flex-1 flex gap-4 min-h-0">
        <PreviewPanel progress={progress} />
        <TransferMonitor progress={progress} />
      </div>
    </div>
  );
}
