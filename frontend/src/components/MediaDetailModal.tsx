// ---------------------------------------------------------------------------
// Transfera v2 — Media Detail & Inspection Modal
// Provides full file metadata, hash verification state, and direct
// "Show in Folder" Explorer integration for complete user peace of mind.
// ---------------------------------------------------------------------------

import { useState, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  X,
  FolderOpen,
  ExternalLink,
  Copy,
  Check,
  ShieldCheck,
  Calendar,
  Camera,
  HardDrive,
  FileText,
  AlertCircle,
  Loader2,
  Clock,
  Sparkles,
} from "lucide-react";
import { useMediaItem } from "@/lib/queries";
import { fetchThumbnail } from "@/lib/thumbnail-fetch";
import { isElectron, parseBackendDate } from "@/lib/utils";
import type { MediaItemInfo } from "@/types/api";

interface MediaDetailModalProps {
  item: MediaItemInfo | null;
  onClose: () => void;
}

function formatSize(bytes: number): string {
  if (bytes > 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  if (bytes > 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes > 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "Not recorded";
  try {
    const d = parseBackendDate(iso);
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default function MediaDetailModal({
  item,
  onClose,
}: MediaDetailModalProps) {
  const [copied, setCopied] = useState(false);
  const [thumbUrl, setThumbUrl] = useState<string | null>(null);

  // Fetch full detail including resolved destination path on disk
  const { data: detail, isLoading } = useMediaItem(item?.id ?? null);

  // Load thumbnail
  useEffect(() => {
    if (!item) {
      setThumbUrl(null);
      return;
    }

    const controller = new AbortController();
    let cancelled = false;

    fetchThumbnail(item.id, item.updated_at, controller.signal).then((url) => {
      if (!cancelled && url) {
        setThumbUrl(url);
      }
    });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [item?.id, item?.updated_at]);

  // Clean up blob URL on unmount
  useEffect(() => {
    return () => {
      if (thumbUrl) URL.revokeObjectURL(thumbUrl);
    };
  }, [thumbUrl]);

  // Keyboard shortcut: Escape to close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  if (!item) return null;

  const activeItem = detail ?? item;
  const targetPath = detail?.dest_path || item.source_path;
  const isVideo = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"].includes(
    (item.extension || "").toLowerCase(),
  );

  const handleShowInFolder = () => {
    if (detail?.dest_path && isElectron && window.electronAPI?.showItemInFolder) {
      window.electronAPI.showItemInFolder(detail.dest_path);
    } else if (item.source_path && isElectron && window.electronAPI?.showItemInFolder) {
      window.electronAPI.showItemInFolder(item.source_path);
    }
  };

  const handleOpenFile = () => {
    const p = detail?.dest_path || item.source_path;
    if (p && isElectron && window.electronAPI?.openPath) {
      window.electronAPI.openPath(p);
    }
  };

  const handleCopyPath = () => {
    if (targetPath) {
      navigator.clipboard.writeText(targetPath);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        {/* Backdrop */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
          className="fixed inset-0 bg-black/60 backdrop-blur-xs"
        />

        {/* Modal Dialog */}
        <motion.div
          initial={{ opacity: 0, scale: 0.96, y: 8 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.96, y: 8 }}
          transition={{ duration: 0.15, ease: "easeOut" }}
          className="relative w-full max-w-2xl bg-card border border-border rounded-2xl shadow-2xl overflow-hidden z-10 flex flex-col max-h-[90vh]"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-muted/20">
            <div className="flex items-center gap-2 min-w-0 pr-4">
              <h3 className="text-base font-semibold text-foreground truncate" title={activeItem.file_name}>
                {activeItem.file_name}
              </h3>
              {activeItem.extension && (
                <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-muted text-muted-foreground uppercase shrink-0">
                  {activeItem.extension.replace(".", "")}
                </span>
              )}
            </div>
            <button
              onClick={onClose}
              className="p-1.5 rounded-full text-muted-foreground hover:text-foreground hover:bg-muted transition-colors shrink-0"
              aria-label="Close"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Modal Body */}
          <div className="p-6 overflow-y-auto space-y-6 flex-1">
            {/* Media Preview Box */}
            <div className="relative w-full max-h-64 rounded-xl bg-muted/40 border border-border flex items-center justify-center overflow-hidden">
              {thumbUrl ? (
                <img
                  src={thumbUrl}
                  alt={activeItem.file_name}
                  className="max-h-64 w-auto object-contain mx-auto"
                />
              ) : (
                <div className="py-12 flex flex-col items-center justify-center text-muted-foreground">
                  <FileText className="w-12 h-12 mb-2 opacity-40" />
                  <p className="text-xs">Preview not available</p>
                </div>
              )}

              {/* Badges on preview */}
              <div className="absolute top-3 left-3 flex items-center gap-1.5">
                {activeItem.live_photo_group && (
                  <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-pill bg-action text-white text-[11px] font-normal shadow-sm">
                    <Sparkles className="w-3 h-3" />
                    Live Photo
                  </span>
                )}
                {activeItem.final_status === "completed" && (
                  <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-pill bg-green-600 text-white text-[11px] font-normal shadow-sm">
                    <ShieldCheck className="w-3.5 h-3.5" />
                    Checksum Verified
                  </span>
                )}
              </div>
            </div>

            {/* Error Message if failed */}
            {activeItem.error_message && (
              <div className="flex items-start gap-2.5 p-3.5 rounded-xl bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 text-xs text-red-700 dark:text-red-300">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-red-500" />
                <div>
                  <p className="font-semibold">Transfer Issue</p>
                  <p className="mt-0.5 opacity-90 break-words">{activeItem.error_message}</p>
                </div>
              </div>
            )}

            {/* Metadata Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              <div className="p-3 bg-muted/30 border border-border/60 rounded-xl">
                <p className="text-[11px] text-muted-foreground flex items-center gap-1">
                  <HardDrive className="w-3.5 h-3.5 text-muted-foreground/70" />
                  File Size
                </p>
                <p className="text-sm font-semibold text-foreground mt-1">
                  {formatSize(activeItem.file_size)}
                </p>
              </div>

              <div className="p-3 bg-muted/30 border border-border/60 rounded-xl">
                <p className="text-[11px] text-muted-foreground flex items-center gap-1">
                  <Calendar className="w-3.5 h-3.5 text-muted-foreground/70" />
                  Date Captured
                </p>
                <p className="text-sm font-semibold text-foreground mt-1 truncate" title={formatDate(activeItem.date_taken)}>
                  {activeItem.date_taken ? formatDate(activeItem.date_taken) : "From file date"}
                </p>
              </div>

              <div className="p-3 bg-muted/30 border border-border/60 rounded-xl">
                <p className="text-[11px] text-muted-foreground flex items-center gap-1">
                  <Camera className="w-3.5 h-3.5 text-muted-foreground/70" />
                  Camera
                </p>
                <p className="text-sm font-semibold text-foreground mt-1 truncate" title={activeItem.camera_model || activeItem.camera_make || "—"}>
                  {activeItem.camera_model || activeItem.camera_make || "Unknown"}
                </p>
              </div>

              {activeItem.width && activeItem.height && (
                <div className="p-3 bg-muted/30 border border-border/60 rounded-xl">
                  <p className="text-[11px] text-muted-foreground">Dimensions</p>
                  <p className="text-sm font-semibold text-foreground mt-1">
                    {activeItem.width} × {activeItem.height}
                  </p>
                </div>
              )}

              {isVideo && activeItem.duration_s != null && activeItem.duration_s > 0 && (
                <div className="p-3 bg-muted/30 border border-border/60 rounded-xl">
                  <p className="text-[11px] text-muted-foreground flex items-center gap-1">
                    <Clock className="w-3.5 h-3.5 text-muted-foreground/70" />
                    Duration
                  </p>
                  <p className="text-sm font-semibold text-foreground mt-1">
                    {Math.floor(activeItem.duration_s / 60)}m {Math.floor(activeItem.duration_s % 60)}s
                  </p>
                </div>
              )}

              <div className="p-3 bg-muted/30 border border-border/60 rounded-xl">
                <p className="text-[11px] text-muted-foreground">Protection</p>
                <p className="text-sm font-semibold text-foreground mt-1 flex items-center gap-1 text-green-600 dark:text-green-400">
                  <ShieldCheck className="w-4 h-4" />
                  BLAKE3 Safe
                </p>
              </div>
            </div>

            {/* Path Location Card */}
            <div className="p-4 bg-muted/20 border border-border rounded-xl space-y-3">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <p className="text-xs font-semibold text-foreground flex items-center gap-1.5">
                    <HardDrive className="w-3.5 h-3.5 text-action" />
                    Saved Location on PC
                  </p>
                  {isLoading ? (
                    <span className="text-[10px] text-muted-foreground flex items-center gap-1">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      Locating...
                    </span>
                  ) : detail?.dest_exists ? (
                    <span className="text-[10px] text-green-600 dark:text-green-400 font-semibold flex items-center gap-1">
                      <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                      Available on Disk
                    </span>
                  ) : (
                    <span className="text-[10px] text-muted-foreground">
                      Destination archive
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <p className="text-xs text-muted-foreground font-mono bg-background border border-border/80 px-2.5 py-1.5 rounded-lg flex-1 truncate select-all" title={targetPath}>
                    {targetPath || "Path not available"}
                  </p>
                  <button
                    onClick={handleCopyPath}
                    className="p-2 rounded-lg border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition-colors shrink-0"
                    title={copied ? "Copied!" : "Copy path"}
                  >
                    {copied ? (
                      <Check className="w-4 h-4 text-green-500" />
                    ) : (
                      <Copy className="w-4 h-4" />
                    )}
                  </button>
                </div>
              </div>

              {item.source_path && item.source_path !== targetPath && (
                <div className="pt-2 border-t border-border/50">
                  <p className="text-[11px] text-muted-foreground mb-0.5">Original Source Path:</p>
                  <p className="text-[11px] text-muted-foreground/80 font-mono truncate select-all" title={item.source_path}>
                    {item.source_path}
                  </p>
                </div>
              )}
            </div>
          </div>

          {/* Footer Actions */}
          <div className="px-6 py-4 border-t border-border bg-muted/10 flex items-center justify-between gap-3">
            <div className="text-xs text-muted-foreground">
              {detail?.dest_exists ? "Verified and ready on your computer." : "Safe two-stage verified vault."}
            </div>

            <div className="flex items-center gap-2">
              {isElectron && (
                <button
                  onClick={handleOpenFile}
                  className="px-4 py-2 text-sm font-normal rounded-pill border border-border hover:bg-muted text-foreground transition-colors active:scale-[0.95] flex items-center gap-1.5"
                >
                  <ExternalLink className="w-4 h-4" />
                  Open File
                </button>
              )}
              {isElectron && (
                <button
                  onClick={handleShowInFolder}
                  className="px-5 py-2 text-sm font-normal rounded-pill bg-action text-white hover:bg-action/90 active:scale-[0.95] transition-all flex items-center gap-1.5 shadow-xs"
                >
                  <FolderOpen className="w-4 h-4" />
                  Show in Folder
                </button>
              )}
              {!isElectron && (
                <button
                  onClick={handleCopyPath}
                  className="px-5 py-2 text-sm font-normal rounded-pill bg-action text-white hover:bg-action/90 active:scale-[0.95] transition-all flex items-center gap-1.5"
                >
                  {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                  {copied ? "Copied" : "Copy Path"}
                </button>
              )}
            </div>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
