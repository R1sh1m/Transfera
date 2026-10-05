// ---------------------------------------------------------------------------
// Transfera v2 — Dashboard Page
// Live system metrics, directory analysis, session management.
// ---------------------------------------------------------------------------

import { useState, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import {
  HardDrive,
  Play,
  Clock,
  CheckCircle2,
  AlertTriangle,
  RefreshCw,
  ArrowRight,
  Folder,
  FolderOpen,
  Archive,
  Loader2,
  Copy,
  ArrowRightLeft,
  Activity,
  Database,
  FileText,
  ChevronDown,
  ChevronRight,
  Trash2,
  X,
  Smartphone,
  Tablet,
  Camera,
  Wifi,
  Terminal,
  Pencil,
  Check,
} from "lucide-react";
import {
  useSessionList,
  useRecovery,
  useFolderMetadata,
  useHealth,
  useDiskSpace,
  useClearSessions,
  useDeviceBackendStatus,
  useInstallDriver,
  useInstallPymobiledevice3,
  useIOSDevices,
} from "@/lib/queries";
import { useTransferStore } from "@/store/transfer";
import { cn, extractErrorMessage, parseBackendDate } from "@/lib/utils";
import { isDesktop, openPath, openDirectory, runElevated } from "@/lib/desktop";
import { formatDevicePath, getDeviceMeta } from "@/lib/device-utils";
import type { SessionInfo, SessionStatus, IOSDeviceInfo } from "@/types/api";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / 1024 ** i).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = parseBackendDate(dateStr).getTime();
  const diffMs = now - then;
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return parseBackendDate(dateStr).toLocaleDateString();
}

// ---------------------------------------------------------------------------
// Status Badge
// ---------------------------------------------------------------------------
import { VerifiedCheckBadge } from "@/components/VerifiedCheckBadge";

const fallbackBadge = {
  color: "text-muted-foreground",
  bg: "bg-muted",
  icon: <Clock className="w-3.5 h-3.5" />,
};

const statusConfig: Record<
  SessionStatus,
  { color: string; bg: string; icon: React.ReactNode }
> = {
  created: {
    color: "text-muted-foreground",
    bg: "bg-muted",
    icon: <Clock className="w-3.5 h-3.5" />,
  },
  running: {
    color: "text-blue-600 dark:text-blue-400",
    bg: "bg-blue-50 dark:bg-blue-950",
    icon: <Play className="w-3.5 h-3.5" />,
  },
  paused: {
    color: "text-amber-600 dark:text-amber-400",
    bg: "bg-amber-50 dark:bg-amber-950",
    icon: <AlertTriangle className="w-3.5 h-3.5" />,
  },
  completed: {
    color: "text-emerald-700 dark:text-emerald-400",
    bg: "bg-emerald-50 dark:bg-emerald-950/60 border border-emerald-200/70 dark:border-emerald-800/70",
    icon: <VerifiedCheckBadge size="sm" />,
  },
  completed_with_errors: {
    color: "text-amber-600 dark:text-amber-400",
    bg: "bg-amber-50 dark:bg-amber-950",
    icon: <AlertTriangle className="w-3.5 h-3.5" />,
  },
  failed: {
    color: "text-red-600 dark:text-red-400",
    bg: "bg-red-50 dark:bg-red-950",
    icon: <AlertTriangle className="w-3.5 h-3.5" />,
  },
  cancelled: {
    color: "text-muted-foreground",
    bg: "bg-muted",
    icon: <Clock className="w-3.5 h-3.5" />,
  },
};

export function StatusBadge({ status }: { status: SessionStatus }) {
  const c = statusConfig[status] ?? fallbackBadge;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-normal",
        c.bg,
        c.color,
      )}
      title={
        status === "completed"
          ? "All hashes verified (BLAKE3 cryptographic integrity confirmed)"
          : undefined
      }
    >
      {c.icon}
      {status === "completed"
        ? "Completed"
        : status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Directory Metrics Card (interactive — path editable from Dashboard)
// ---------------------------------------------------------------------------
interface DirMetricsCardProps {
  label: string;
  sublabel: string;
  icon: React.ReactNode;
  iconBg: string;
  path: string | null;
  onPathChange: (newPath: string) => void;
  sessionName?: string;
  transferMode?: "copy" | "move";
  connectedDevices?: IOSDeviceInfo[];
}

function DirMetricsCard({
  label,
  sublabel,
  icon,
  iconBg,
  path,
  onPathChange,
  sessionName,
  transferMode,
  connectedDevices = [],
}: DirMetricsCardProps) {
  const { isDevice, cleanPath: deviceCleanPath, meta: deviceMeta } = formatDevicePath(
    path,
    connectedDevices,
  );

  // Ready connected devices other than the currently selected one
  const otherReadyDevices = connectedDevices.filter((d) => {
    if (d.status !== "ready") return false;
    const curSerial = path
      ?.replace(/^ios:\/\//, "")
      .replace(/^wpd:\/\//, "")
      .split("/")[0]
      ?.toLowerCase();
    return d.serial.toLowerCase() !== curSerial;
  });

  const { data: metrics, isLoading } = useFolderMetadata(path);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(path ?? "");
  const inputRef = useRef<HTMLInputElement>(null);

  // Keep draft in sync when path changes externally (e.g. from Setup page)
  useEffect(() => {
    if (!editing) setDraft(path ?? "");
  }, [path, editing]);

  const commitDraft = () => {
    const trimmed = draft.trim();
    if (trimmed !== (path ?? "")) {
      onPathChange(trimmed);
    }
    setEditing(false);
  };

  const handleCancel = () => {
    setDraft(path ?? "");
    setEditing(false);
  };

  const handleBrowse = async (e?: React.MouseEvent) => {
    e?.stopPropagation();
    try {
      if (isDesktop) {
        const cleanPath = path && !isDevice ? path : undefined;
        const selected = await openDirectory(cleanPath);
        if (selected) {
          onPathChange(selected);
          setDraft(selected);
          return;
        }
      }
      // Non-desktop or cancelled
      if (!isDesktop) {
        setDraft(path ?? "");
        setEditing(true);
      }
    } catch (err) {
      console.error("Browse directory failed:", err);
      setDraft(path ?? "");
      setEditing(true);
    }
  };

  const handleClear = (e: React.MouseEvent) => {
    e.stopPropagation();
    onPathChange("");
    setDraft("");
    setEditing(false);
  };

  const effectiveIcon = isDevice ? (
    deviceMeta.iconType === "tablet" ? (
      <Tablet className={cn("w-4.5 h-4.5", deviceMeta.accentColor.text)} />
    ) : deviceMeta.iconType === "camera" ? (
      <Camera className={cn("w-4.5 h-4.5", deviceMeta.accentColor.text)} />
    ) : deviceMeta.iconType === "hard-drive" ? (
      <HardDrive className={cn("w-4.5 h-4.5", deviceMeta.accentColor.text)} />
    ) : (
      <Smartphone className={cn("w-4.5 h-4.5", deviceMeta.accentColor.text)} />
    )
  ) : (
    icon
  );

  const effectiveIconBg = isDevice ? deviceMeta.accentColor.bg : iconBg;
  const displayPath = isDevice ? deviceCleanPath : path;

  return (
    <div
      onClick={() => {
        if (!path && !editing) handleBrowse();
      }}
      className={cn(
        "bg-card border rounded-xl p-4 transition-all relative group/card",
        path
          ? "border-border shadow-xs"
          : "border-dashed border-border/80 hover:border-primary/60 hover:bg-muted/10 cursor-pointer",
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-3 mb-2.5">
        <div
          className={cn(
            "w-9 h-9 rounded-lg flex items-center justify-center shrink-0",
            effectiveIconBg,
          )}
        >
          {effectiveIcon}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-1">
            <div className="flex items-center gap-1.5 min-w-0">
              <p className="text-xs font-semibold text-foreground">{label}</p>
              {isDevice && (
                <span
                  className={cn(
                    "text-[10px] font-normal px-2 py-0.5 rounded-pill border shrink-0",
                    deviceMeta.accentColor.badgeBg,
                    deviceMeta.accentColor.badgeText,
                    deviceMeta.accentColor.border,
                  )}
                >
                  {deviceMeta.platformBadge}
                </span>
              )}
            </div>
            {sessionName && (
              <span
                className="text-[10px] font-normal text-muted-foreground bg-muted px-1.5 py-0.5 rounded truncate max-w-[100px]"
                title={sessionName}
              >
                {sessionName}
              </span>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground/80 truncate">
            {isDevice
              ? deviceMeta.categoryLabel
              : path
                ? "Click browse or edit to change"
                : sublabel}
          </p>
        </div>
      </div>

      {/* Path Display / Edit Row */}
      <div className="mb-3">
        {editing ? (
          <div
            className="flex items-center gap-1.5"
            onClick={(e) => e.stopPropagation()}
          >
            <input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitDraft();
                if (e.key === "Escape") handleCancel();
              }}
              autoFocus
              className="flex-1 text-xs bg-background border border-input rounded-md px-2 py-1 text-foreground focus:outline-none focus:ring-1 focus:ring-ring font-mono"
              placeholder="e.g. C:\Photos or D:\Backups"
            />
            <button
              type="button"
              onClick={commitDraft}
              className="p-1.5 rounded-md bg-action text-white hover:bg-action/90 transition-colors"
              title="Save path"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
            <button
              type="button"
              onClick={handleCancel}
              className="p-1.5 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              title="Cancel"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        ) : path ? (
          <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-lg bg-muted/40 border border-border/60">
            <div
              className="flex items-center gap-1.5 min-w-0 flex-1"
              title={displayPath ?? undefined}
            >
              {isDevice ? (
                deviceMeta.iconType === "tablet" ? (
                  <Tablet className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                ) : deviceMeta.iconType === "camera" ? (
                  <Camera className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                ) : deviceMeta.iconType === "hard-drive" ? (
                  <HardDrive className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                ) : (
                  <Smartphone className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                )
              ) : (
                <Folder className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
              )}
              <span className="text-xs font-mono text-foreground truncate select-all">
                {displayPath}
              </span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setDraft(path ?? "");
                  setEditing(true);
                }}
                className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                title="Type path manually"
              >
                <Pencil className="w-3 h-3" />
              </button>
              <button
                type="button"
                onClick={handleClear}
                className="p-1 rounded text-muted-foreground hover:text-red-500 hover:bg-muted transition-colors"
                title="Clear directory"
              >
                <X className="w-3 h-3" />
              </button>
            </div>
          </div>
        ) : (
          <div className="px-2.5 py-2 rounded-lg border border-dashed border-border/80 bg-muted/20 text-center">
            <p className="text-xs text-muted-foreground">No directory selected</p>
          </div>
        )}
      </div>

      {/* Action Buttons Row */}
      {!editing && (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleBrowse}
            className="no-drag flex-1 flex items-center justify-center gap-1.5 py-1.5 px-3 rounded-pill border border-border bg-muted/50 hover:bg-muted text-xs font-normal text-foreground hover:text-foreground active:scale-[0.95] transition-all"
          >
            <FolderOpen className="w-3.5 h-3.5 text-muted-foreground" />
            {path ? (isDevice ? "Change device / folder…" : "Change folder…") : "+ Select folder…"}
          </button>
          {!path && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setDraft("");
                setEditing(true);
              }}
              className="no-drag flex items-center justify-center gap-1 py-1.5 px-3 rounded-pill border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-muted active:scale-[0.95] transition-all"
              title="Type or paste path"
            >
              <Pencil className="w-3 h-3" />
              Type path
            </button>
          )}
        </div>
      )}

      {/* Quick switch between connected devices (e.g. iPhone <-> Android) */}
      {!editing && otherReadyDevices.length > 0 && (
        <div className="mt-3 pt-2.5 border-t border-border/50 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[11px] text-muted-foreground">Other connected devices:</span>
          <div className="flex items-center gap-1.5 flex-wrap">
            {otherReadyDevices.map((d) => {
              const dMeta = getDeviceMeta(d);
              return (
                <button
                  key={d.serial}
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    const prefix =
                      d.active_tier === "wpd" || d.serial.startsWith("\\\\?\\") ? "wpd://" : "ios://";
                    onPathChange(`${prefix}${d.serial}/DCIM`);
                  }}
                  className={cn(
                    "no-drag px-2.5 py-1 rounded-pill text-[11px] font-normal border transition-all active:scale-[0.95] flex items-center gap-1 hover:opacity-90",
                    dMeta.accentColor.bg,
                    dMeta.accentColor.text,
                    dMeta.accentColor.border,
                  )}
                  title={`Switch to ${dMeta.displayName}`}
                >
                  {dMeta.iconType === "tablet" ? (
                    <Tablet className="w-3 h-3" />
                  ) : dMeta.iconType === "camera" ? (
                    <Camera className="w-3 h-3" />
                  ) : (
                    <Smartphone className="w-3 h-3" />
                  )}
                  <span>Switch to {dMeta.shortName}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Metrics */}
      {isLoading && path && !isDevice ? (
        <div className="flex items-center gap-2 py-2 mt-2 pt-2 border-t border-border">
          <Loader2 className="w-3.5 h-3.5 text-muted-foreground animate-spin" />
          <span className="text-xs text-muted-foreground">
            Calculating folder size…
          </span>
        </div>
      ) : metrics ? (
        <div className="space-y-2 mt-2 pt-2 border-t border-border">
          <div className="grid grid-cols-2 gap-2">
            <div className="text-center">
              <p className="text-sm font-bold text-foreground">
                {metrics.size_human}
              </p>
              <p className="text-[10px] text-muted-foreground">Total Size</p>
            </div>
            <div className="text-center">
              <p className="text-sm font-bold text-foreground">
                {metrics.file_count.toLocaleString()}
              </p>
              <p className="text-[10px] text-muted-foreground">Files</p>
            </div>
          </div>
          {transferMode && (
            <div className="flex items-center justify-center gap-1.5 pt-1 border-t border-border">
              {transferMode === "copy" ? (
                <Copy className="w-3 h-3 text-blue-500 dark:text-blue-400" />
              ) : (
                <ArrowRightLeft className="w-3 h-3 text-amber-500 dark:text-amber-400" />
              )}
              <span
                className={cn(
                  "text-[10px] font-normal",
                  transferMode === "copy"
                    ? "text-blue-600 dark:text-blue-400"
                    : "text-amber-600 dark:text-amber-400",
                )}
              >
                {transferMode === "copy"
                  ? "Backup (Copy)"
                  : "Space Saver (Move)"}
              </span>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resume Alert
// ---------------------------------------------------------------------------
function ResumeAlert() {
  const { data: sessionList } = useSessionList(1, 100);
  const recovery = useRecovery();
  const [expanded, setExpanded] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const pausedSessions =
    sessionList?.sessions?.filter(
      // Prescan sessions are inventory-only — a scan is idempotent, there is
      // nothing to resume, so they never prompt recovery.
      (s) => !s.is_prescan && (s.status === "paused" || s.status === "created"),
    ) ?? [];

  // Reset dismiss when the underlying list changes (new sessions appear on
  // a fresh page load after a prior dismiss).
  useEffect(() => {
    setDismissed(false);
  }, [pausedSessions.length]);

  if (pausedSessions.length === 0 || dismissed) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      className="relative bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-lg p-4 mb-6"
    >
      <button
        onClick={() => setDismissed(true)}
        className="no-drag absolute top-3 right-3 text-amber-400 hover:text-amber-600 dark:hover:text-amber-200 transition-colors"
        title="Dismiss"
      >
        <X className="w-4 h-4" />
      </button>

      <div className="flex items-start gap-3">
        <div className="shrink-0 w-8 h-8 rounded-full bg-amber-100 dark:bg-amber-900 flex items-center justify-center">
          <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-200">
            Interrupted Workloads Detected
          </h3>
          <p className="text-xs text-amber-700 dark:text-amber-300 mt-1">
            {pausedSessions.length} session
            {pausedSessions.length > 1 ? "s" : ""} can be resumed.
          </p>

          {/* Expandable session list */}
          <button
            onClick={() => setExpanded(!expanded)}
            className="no-drag flex items-center gap-1 mt-2 text-xs text-amber-600 dark:text-amber-400 hover:text-amber-800 dark:hover:text-amber-200 transition-colors"
          >
            {expanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
            {expanded ? "Hide details" : "Show details"}
          </button>

          {expanded && (
            <div className="mt-2 space-y-1.5">
              {pausedSessions.map((s) => (
                <div
                  key={s.id}
                  className="flex items-center justify-between text-xs px-2.5 py-1.5 bg-amber-100/50 dark:bg-amber-900/30 rounded"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-semibold text-amber-800 dark:text-amber-200 truncate">
                      {s.session_name}
                    </span>
                    <span
                      className={cn(
                        "text-[10px] font-mono px-1.5 py-0.5 rounded",
                        s.transfer_mode === "copy"
                          ? "bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300"
                          : "bg-amber-100 dark:bg-amber-800 text-amber-700 dark:text-amber-300",
                      )}
                    >
                      {s.transfer_mode === "copy" ? "COPY" : "MOVE"}
                    </span>
                  </div>
                  <span className="text-amber-600 dark:text-amber-400 ml-2 shrink-0">
                    {s.total_items} items
                  </span>
                </div>
              ))}
            </div>
          )}

          <div className="flex gap-2 mt-2">
            <button
              onClick={() => {
                recovery.mutate(undefined, {
                  onSuccess: () => setDismissed(true),
                });
              }}
              disabled={recovery.isPending}
              className="no-drag inline-flex items-center gap-1 px-3 py-1.5 bg-amber-500 text-white rounded text-xs font-normal hover:bg-amber-600 transition-colors disabled:opacity-50"
            >
              <RefreshCw
                className={cn("w-3 h-3", recovery.isPending && "animate-spin")}
              />
              Recover All
            </button>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Confirm Dialog
// ---------------------------------------------------------------------------
function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  onConfirm,
  onCancel,
  loading,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  loading: boolean;
}) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center"
        >
          <div className="fixed inset-0 bg-black/50" onClick={onCancel} />
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            className="relative bg-card border border-border rounded-lg p-6 max-w-md w-full mx-4"
          >
            <h3 className="text-lg font-semibold text-foreground mb-2">
              {title}
            </h3>
            <p className="text-sm text-muted-foreground mb-6 whitespace-pre-line">
              {description}
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={onCancel}
                disabled={loading}
                className="px-4 py-2 text-sm font-normal text-muted-foreground hover:text-foreground transition-colors rounded-md hover:bg-muted disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={onConfirm}
                disabled={loading}
                className="px-4 py-2 text-sm font-normal text-white bg-red-600 hover:bg-red-700 rounded-md transition-colors disabled:opacity-50 inline-flex items-center gap-2"
              >
                {loading && <Loader2 className="w-4 h-4 animate-spin" />}
                {confirmLabel}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// Clear Sessions Button
// ---------------------------------------------------------------------------
function ClearSessionsButton({ sessionCount }: { sessionCount: number }) {
  const [showDialog, setShowDialog] = useState(false);
  const clearSessions = useClearSessions();

  const handleConfirm = () => {
    clearSessions.mutate(undefined, {
      onSettled: () => setShowDialog(false),
    });
  };

  if (sessionCount === 0) return null;

  return (
    <>
      <button
        onClick={() => setShowDialog(true)}
        className="no-drag inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-normal text-muted-foreground hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-950 border border-input rounded-md transition-colors"
        title="Clear session history"
      >
        <Trash2 className="w-3.5 h-3.5" />
        Clear Sessions
      </button>
      <ConfirmDialog
        open={showDialog}
        title="Clear Session History"
        description={`This will permanently remove all ${sessionCount} session(s), their batches, library items, and generated thumbnails from the app.\n\nThis only clears app records — your actual files at the transfer destination are not affected.\n\nThis action cannot be undone.`}
        confirmLabel="Clear All Sessions"
        onConfirm={handleConfirm}
        onCancel={() => setShowDialog(false)}
        loading={clearSessions.isPending}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Backend Status Card
// ---------------------------------------------------------------------------
function BackendStatusCard() {
  const {
    data: health,
    isLoading: healthLoading,
    isError: healthError,
  } = useHealth();
  const wsConnected = useTransferStore((s) => s.wsConnected);
  const sessionId = useTransferStore((s) => s.transfer.sessionId);

  const restOnline = !healthLoading && !healthError && health?.status === "ok";
  const restColor = healthLoading
    ? "bg-muted"
    : restOnline
      ? "bg-green-500"
      : "bg-red-500";
  const wsColor = wsConnected
    ? "bg-green-500"
    : sessionId !== null
      ? "bg-red-500"
      : "bg-muted-foreground/40";
  const wsTooltip = wsConnected
    ? "WebSocket connected"
    : sessionId !== null
      ? "WebSocket disconnected"
      : "No active transfer";

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <Activity className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Backend Status</p>
        <div className="flex items-center gap-3 mt-1">
          <div className="flex items-center gap-1.5">
            <span className={cn("w-2 h-2 rounded-full", restColor)} />
            <span className="text-[11px] text-muted-foreground">REST</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span
              className={cn("w-2 h-2 rounded-full", wsColor)}
              title={wsTooltip}
            />
            <span className="text-[11px] text-muted-foreground">WS</span>
          </div>
        </div>
      </div>
      {health?.version && (
        <span className="text-[10px] font-normal text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
          v{health.version}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Aggregate Stats Card
// ---------------------------------------------------------------------------
function AggregateStatsCard({ sessions }: { sessions: SessionInfo[] }) {
  const totalSessions = sessions.length;
  const totalFiles = sessions.reduce((sum, s) => sum + s.completed_items, 0);
  const totalVolume = sessions.reduce(
    (sum, s) => sum + (s.total_bytes_volume ?? 0),
    0,
  );
  const activeCount = sessions.filter(
    (s) => s.status === "running" || s.status === "paused",
  ).length;

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <Database className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Aggregate Stats</p>
        <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 mt-1">
          <span className="text-[11px] text-muted-foreground">
            {totalSessions} sessions
          </span>
          <span className="text-[11px] text-muted-foreground">
            {totalFiles.toLocaleString()} files
          </span>
          <span className="text-[11px] text-muted-foreground">
            {formatBytes(totalVolume)} vol.
          </span>
          <span className="text-[11px] text-muted-foreground">
            {activeCount > 0 ? (
              <span className="text-blue-600 dark:text-blue-400">
                {activeCount} active
              </span>
            ) : (
              "No active"
            )}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Storage Health Card
// ---------------------------------------------------------------------------
function StorageHealthCard({ destPath }: { destPath: string | null }) {
  const { data: diskSpace, isLoading } = useDiskSpace(destPath);

  const freePct = diskSpace
    ? Math.round((diskSpace.free_bytes / diskSpace.total_bytes) * 100)
    : null;

  const healthColor = !diskSpace
    ? "text-muted-foreground"
    : freePct !== null && freePct < 10
      ? "text-red-600 dark:text-red-400"
      : freePct !== null && freePct < 25
        ? "text-amber-600 dark:text-amber-400"
        : "text-green-600 dark:text-green-400";

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <HardDrive className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Storage Health</p>
        {isLoading && destPath ? (
          <div className="flex items-center gap-1.5 mt-1">
            <Loader2 className="w-3 h-3 text-muted-foreground animate-spin" />
            <span className="text-[11px] text-muted-foreground">
              Checking...
            </span>
          </div>
        ) : diskSpace ? (
          <div className="mt-1">
            <p className={cn("text-sm font-semibold", healthColor)}>
              {formatBytes(diskSpace.free_bytes)} free
            </p>
            <p className="text-[10px] text-muted-foreground">
              of {formatBytes(diskSpace.total_bytes)} total ({freePct}% free)
            </p>
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground mt-1">
            {destPath ? "Unable to read disk" : "No destination set"}
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Last Backup Card
// ---------------------------------------------------------------------------
function LastBackupCard({ sessions }: { sessions: SessionInfo[] }) {
  const lastCompleted = sessions.find((s) => s.status === "completed");

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <CheckCircle2 className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Last Backup</p>
        {lastCompleted ? (
          <div className="mt-1">
            <p
              className="text-sm font-semibold text-foreground truncate"
              title={lastCompleted.session_name}
            >
              {lastCompleted.session_name}
            </p>
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-muted-foreground">
                {lastCompleted.completed_at
                  ? timeAgo(lastCompleted.completed_at)
                  : "unknown"}
              </span>
              {lastCompleted.failed_items > 0 && (
                <span className="text-[10px] font-normal text-amber-600 dark:text-amber-400">
                  {lastCompleted.failed_items} failed
                </span>
              )}
            </div>
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground mt-1">
            No backups yet
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session Table Row
// ---------------------------------------------------------------------------
function SessionRow({ session }: { session: SessionInfo }) {
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage);
  const initTransfer = useTransferStore((s) => s.initTransfer);

  const handleResume = () => {
    initTransfer(session);
    setCurrentPage("transfer");
  };

  const handleViewReport = () => {
    if (!session.session_report_path) return;
    if (isDesktop) {
      openPath(session.session_report_path);
    } else {
      window.open(`/api/sessions/${session.id}/report?fmt=html`, "_blank");
    }
  };

  return (
    <tr className="border-b border-border last:border-b-0 hover:bg-muted/30 transition-colors">
      <td className="py-3 pl-4 pr-3">
        <StatusBadge status={session.status} />
      </td>
      <td className="py-2.5 pr-3">
        <p
          className="text-sm font-semibold text-foreground truncate max-w-[180px]"
          title={session.session_name}
        >
          {session.session_name}
        </p>
      </td>
      <td className="py-3 pr-3">
        <p
          className="text-xs text-muted-foreground truncate max-w-[220px]"
          title={session.source_root}
        >
          {session.source_root}
        </p>
      </td>
      <td className="py-3 pr-3">
        <p
          className="text-xs text-muted-foreground truncate max-w-[220px]"
          title={session.dest_root}
        >
          {session.dest_root}
        </p>
      </td>
      <td className="py-3 pr-3">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {session.completed_items.toLocaleString()} /{" "}
            {session.total_items.toLocaleString()}
          </span>
          {session.total_bytes_volume != null &&
            session.total_bytes_volume > 0 && (
              <span className="text-[10px] font-normal text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
                {formatBytes(session.total_bytes_volume)}
              </span>
            )}
        </div>
      </td>
      <td className="py-3 pr-3">
        <span className="text-xs text-muted-foreground">
          {parseBackendDate(session.created_at).toLocaleDateString()}
        </span>
      </td>
      <td className="py-3 pr-4">
        <div className="flex items-center gap-1.5">
          {session.status === "paused" && (
            <button
              onClick={handleResume}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-amber-500 text-white rounded text-xs font-normal hover:bg-amber-600 transition-colors"
            >
              <Play className="w-3 h-3" />
              Resume
            </button>
          )}
          {["completed", "completed_with_errors"].includes(session.status) &&
            session.dest_root &&
            isDesktop && (
              <button
                onClick={() => openPath(session.dest_root)}
                className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-normal hover:bg-secondary/80 transition-colors"
                title="Open destination folder in Explorer"
              >
                <FolderOpen className="w-3 h-3" />
                Folder
              </button>
            )}
          {["completed", "completed_with_errors", "failed"].includes(
            session.status,
          ) &&
            session.session_report_path && (
              <button
                onClick={handleViewReport}
                className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-normal hover:bg-secondary/80 transition-colors"
                title="Open HTML report"
              >
                <FileText className="w-3 h-3" />
                Report
              </button>
            )}
          {["failed", "cancelled", "completed_with_errors"].includes(
            session.status,
          ) &&
            !session.session_report_path && (
              <span
                className="text-xs text-muted-foreground"
                title="Report not available — session did not complete"
              >
                —
              </span>
            )}
          {["completed", "completed_with_errors", "failed"].includes(
            session.status,
          ) && (
            <button
              onClick={handleResume}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-normal hover:bg-secondary/80 transition-colors"
            >
              <ArrowRight className="w-3 h-3" />
              View
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Setup Cards (auto-activation prompts)
// ---------------------------------------------------------------------------
function DriverSetupCard({
  name,
  version,
  onDismiss,
}: {
  name: string | null;
  version: string | null;
  onDismiss: () => void;
}) {
  const installDriver = useInstallDriver();
  const queryClient = useQueryClient();
  const handleInstall = async () => {
    try {
      // Try non-elevated install via backend API first
      const result = await installDriver.mutateAsync();
      if (!result.success) {
        // If winget failed and the Tauri shell is available, try elevated install
        if (isDesktop) {
          const elevated = await runElevated({
            executable: "winget",
            args: [
              "install",
              "-e",
              "--id",
              "Apple.AppleMobileDeviceSupport",
              "--accept-package-agreements",
              "--accept-source-agreements",
              "--silent",
            ],
          });
          if (elevated.success) {
            queryClient.invalidateQueries({
              queryKey: ["device-backend-status"],
            });
            queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
            useTransferStore
              .getState()
              .showNotification(
                "success",
                "Apple Mobile Device Support installed. Please reconnect your iPhone.",
              );
            onDismiss();
            return;
          }
          useTransferStore
            .getState()
            .showNotification(
              "error",
              elevated.error ||
                `Installation failed (exit code: ${elevated.exitCode})`,
            );
          return;
        }
        useTransferStore
          .getState()
          .showNotification(
            "error",
            result.error ||
              `Installation failed (exit code: ${result.exit_code})`,
          );
        return;
      }
      useTransferStore
        .getState()
        .showNotification(
          "success",
          "Apple Mobile Device Support installed. Please reconnect your iPhone.",
        );
      onDismiss();
    } catch (err) {
      useTransferStore
        .getState()
        .showNotification("error", extractErrorMessage(err));
    }
  };

  return (
    <div className="bg-card border border-border rounded-lg p-4 flex items-start gap-3">
      <div className="shrink-0 w-8 h-8 rounded-lg bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center">
        <Smartphone className="w-4 h-4 text-purple-700 dark:text-purple-400" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-foreground">
          Enable iPhone support
        </p>
        <p className="text-xs text-muted-foreground mt-0.5">
          {name ?? "Apple Mobile Device Support"}{" "}
          {version ? `(${version}) ` : ""}
          is available via winget. Install it now for faster iPhone access.
        </p>
        <div className="flex items-center gap-2 mt-2">
          <button
            onClick={handleInstall}
            disabled={installDriver.isPending}
            className="text-xs bg-primary text-primary-foreground px-3 py-1 rounded-md hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {installDriver.isPending ? "Preparing..." : "Install Driver"}
          </button>
          <button
            onClick={onDismiss}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

function WslSetupCard({ onDismiss }: { onDismiss: () => void }) {
  const goToSetup = useTransferStore((s) => s.setCurrentPage);
  return (
    <div className="bg-card border border-border rounded-lg p-4 flex items-start gap-3">
      <div className="shrink-0 w-8 h-8 rounded-lg bg-teal-100 dark:bg-teal-900/30 flex items-center justify-center">
        <Wifi className="w-4 h-4 text-teal-700 dark:text-teal-400" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-foreground">
          Advanced iPhone Connection (Optional)
        </p>
        <p className="text-xs text-muted-foreground mt-0.5">
          If standard Apple drivers are unavailable on your PC, you can connect
          iPhones using an advanced background bridge.
        </p>
        <div className="flex items-center gap-2 mt-2">
          <button
            onClick={() => {
              goToSetup("setup");
            }}
            className="text-xs bg-primary text-primary-foreground px-3 py-1 rounded-md hover:bg-primary/90 transition-colors"
          >
            Open Advanced Setup
          </button>
          <button
            onClick={onDismiss}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

function BridgeAutoStartedNotice() {
  return (
    <div className="bg-card border border-border rounded-lg p-4 flex items-start gap-3">
      <div className="shrink-0 w-8 h-8 rounded-lg bg-green-100 dark:bg-green-900/30 flex items-center justify-center">
        <CheckCircle2 className="w-4 h-4 text-green-700 dark:text-green-400" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-foreground">
          WSL bridge started
        </p>
        <p className="text-xs text-muted-foreground mt-0.5">
          The WSL bridge was auto-started. iPhone devices connected via the
          open-source bridge are now accessible.
        </p>
      </div>
    </div>
  );
}

function Pymobiledevice3InstallCard({ onDismiss }: { onDismiss: () => void }) {
  const installPymobiledevice3 = useInstallPymobiledevice3();
  const handleInstall = async () => {
    try {
      const result = await installPymobiledevice3.mutateAsync();
      if (!result.success) {
        useTransferStore
          .getState()
          .showNotification(
            "error",
            result.message || "Failed to install iPhone support",
          );
        return;
      }
      useTransferStore
        .getState()
        .showNotification(
          "success",
          "iPhone support installed. Direct device access is ready.",
        );
      onDismiss();
    } catch (err) {
      useTransferStore
        .getState()
        .showNotification("error", extractErrorMessage(err));
    }
  };

  return (
    <div className="bg-card border border-border rounded-lg p-4 flex items-start gap-3">
      <div className="shrink-0 w-8 h-8 rounded-lg bg-amber-100 dark:bg-amber-900/30 flex items-center justify-center">
        <Terminal className="w-4 h-4 text-amber-700 dark:text-amber-400" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-foreground">
          Install iPhone Support (Free)
        </p>
        <p className="text-xs text-muted-foreground mt-0.5">
          Adds direct iPhone backup capability to Transfera without needing
          Apple software or iTunes installed.
        </p>
        <div className="flex items-center gap-2 mt-2">
          <button
            onClick={handleInstall}
            disabled={installPymobiledevice3.isPending}
            className="text-xs bg-primary text-primary-foreground px-3 py-1 rounded-md hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {installPymobiledevice3.isPending
              ? "Installing..."
              : "Install iPhone Support"}
          </button>
          <button
            onClick={onDismiss}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

const DISMISSED_CARDS_STORAGE_KEY = "transfera_dismissed_setup_cards";

function getInitialDismissedCards(): string[] {
  try {
    const raw = localStorage.getItem(DISMISSED_CARDS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------
export default function DashboardPage() {
  const { data: sessionList, isLoading } = useSessionList(1, 20);
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage);
  const sourceRoot = useTransferStore((s) => s.transfer.sourceRoot);
  const destRoot = useTransferStore((s) => s.transfer.destRoot);
  const setupSourcePath = useTransferStore((s) => s.ui.setupSourcePath);
  const setupDestPath = useTransferStore((s) => s.ui.setupDestPath);
  const setSetupSourcePath = useTransferStore((s) => s.setSetupSourcePath);
  const setSetupDestPath = useTransferStore((s) => s.setSetupDestPath);

  const { data: backendStatus } = useDeviceBackendStatus();
  const { data: iosDevices } = useIOSDevices();
  const [dismissedCards, setDismissedCards] = useState<string[]>(
    getInitialDismissedCards,
  );

  const dismissCard = (id: string) => {
    setDismissedCards((prev) => {
      const next = [...prev, id];
      try {
        localStorage.setItem(DISMISSED_CARDS_STORAGE_KEY, JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  };

  const showAppleCard =
    backendStatus?.apple_driver_installable &&
    !dismissedCards.includes("apple-driver");
  const showPymobileCard =
    backendStatus?.pymobiledevice3_installable &&
    !dismissedCards.includes("pymobiledevice3");
  const showWslCard =
    backendStatus?.wsl_setup_suggested && !dismissedCards.includes("wsl-setup");
  const showBridgeCard =
    backendStatus?.bridge_auto_started &&
    !dismissedCards.includes("bridge-started");
  const showAnySetupCard =
    showAppleCard || showPymobileCard || showWslCard || showBridgeCard;

  const latestSession = sessionList?.sessions?.[0];
  const [clearedSource, setClearedSource] = useState(false);
  const [clearedDest, setClearedDest] = useState(false);

  // Prefer setup path (editable), then live transfer, then last session
  const activeSource = clearedSource
    ? null
    : setupSourcePath || sourceRoot || latestSession?.source_root || null;
  const activeDest = clearedDest
    ? null
    : setupDestPath || destRoot || latestSession?.dest_root || null;
  const activeSessionName = latestSession?.session_name;
  const activeTransferMode = latestSession?.transfer_mode;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Your backup history and system status
        </p>
      </div>

      {/* Resume Alert */}
      <ResumeAlert />

      {/* Setup Cards (auto-activation prompts) */}
      {showAnySetupCard && (
        <div className="space-y-2">
          {showAppleCard && (
            <DriverSetupCard
              name={backendStatus?.apple_driver_package_name ?? null}
              version={backendStatus?.apple_driver_package_version ?? null}
              onDismiss={() => dismissCard("apple-driver")}
            />
          )}
          {showPymobileCard && (
            <Pymobiledevice3InstallCard
              onDismiss={() => dismissCard("pymobiledevice3")}
            />
          )}
          {showWslCard && (
            <WslSetupCard onDismiss={() => dismissCard("wsl-setup")} />
          )}
          {showBridgeCard && <BridgeAutoStartedNotice />}
        </div>
      )}

      {/* Directory Metrics — stack on phones, side-by-side from tablets up */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <DirMetricsCard
          label="Source Directory"
          sublabel="Select a source path to analyze"
          icon={
            <Folder className="w-4.5 h-4.5 text-blue-600 dark:text-blue-400" />
          }
          iconBg="bg-blue-50 dark:bg-blue-950"
          path={activeSource}
          onPathChange={(p) => {
            if (!p) setClearedSource(true);
            else setClearedSource(false);
            setSetupSourcePath(p);
          }}
          sessionName={activeSessionName}
          transferMode={activeTransferMode}
          connectedDevices={iosDevices?.devices}
        />
        <DirMetricsCard
          label="Backup Destination"
          sublabel="Select a destination path to analyze"
          icon={
            <Archive className="w-4.5 h-4.5 text-green-600 dark:text-green-400" />
          }
          iconBg="bg-green-50 dark:bg-green-950"
          path={activeDest}
          onPathChange={(p) => {
            if (!p) setClearedDest(true);
            else setClearedDest(false);
            setSetupDestPath(p);
          }}
          sessionName={activeSessionName}
          transferMode={activeTransferMode}
        />
      </div>

      {/* System Stats — 1-col on phones, 2-col on tablets, 4-col on desktop */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <BackendStatusCard />
        <AggregateStatsCard sessions={sessionList?.sessions ?? []} />
        <StorageHealthCard destPath={activeDest} />
        <LastBackupCard sessions={sessionList?.sessions ?? []} />
      </div>

      {/* Quick Start */}
      <motion.button
        whileHover={{ scale: 1.01 }}
        whileTap={{ scale: 0.99 }}
        onClick={() => setCurrentPage("setup")}
        className="no-drag w-full bg-primary text-primary-foreground rounded-pill p-4 flex items-center justify-between hover:bg-primary/90 active:scale-[0.99] transition-colors"
      >
        <div className="flex items-center gap-3">
          <HardDrive className="w-5 h-5" />
          <div className="text-left">
            <p className="text-sm font-semibold">Start New Backup</p>
            <p className="text-xs opacity-80">
              Select source and destination directories
            </p>
          </div>
        </div>
        <ArrowRight className="w-5 h-5" />
      </motion.button>

      {/* Session History Table */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-foreground">
            Recent Sessions
          </h2>
          <div className="flex items-center gap-2">
            {sessionList && sessionList.total > 20 && (
              <button
                onClick={() => setCurrentPage("library")}
                className="text-xs text-primary hover:underline"
              >
                View All
              </button>
            )}
            {sessionList && (
              <ClearSessionsButton sessionCount={sessionList.total} />
            )}
          </div>
        </div>

        {isLoading ? (
          <div className="bg-card border border-border rounded-lg p-6 space-y-3">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="flex items-center gap-4 animate-pulse">
                <div className="h-5 w-16 bg-muted rounded-full" />
                <div className="h-4 bg-muted rounded w-32" />
                <div className="h-4 bg-muted rounded w-48" />
                <div className="h-4 bg-muted rounded w-48" />
                <div className="h-4 bg-muted rounded w-20" />
                <div className="h-4 bg-muted rounded w-16" />
              </div>
            ))}
          </div>
        ) : (sessionList?.sessions?.length ?? 0) === 0 ? (
          <div className="bg-card border border-border rounded-lg p-8 text-center">
            <HardDrive className="w-10 h-10 text-muted-foreground mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">
              No sessions yet. Start your first backup!
            </p>
          </div>
        ) : (
          <div className="bg-card border border-border rounded-lg overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border bg-muted/30">
                  <th className="text-left text-xs font-normal text-muted-foreground py-2 px-4 pr-3 w-[100px]">
                    Status
                  </th>
                  <th className="text-left text-xs font-normal text-muted-foreground py-2 pr-3 w-[180px]">
                    Session
                  </th>
                  <th className="text-left text-xs font-normal text-muted-foreground py-2 pr-3">
                    Source
                  </th>
                  <th className="text-left text-xs font-normal text-muted-foreground py-2 pr-3">
                    Destination
                  </th>
                  <th className="text-left text-xs font-normal text-muted-foreground py-2 pr-3 w-[160px]">
                    Progress
                  </th>
                  <th className="text-left text-xs font-normal text-muted-foreground py-2 pr-3 w-[90px]">
                    Date
                  </th>
                  <th className="text-left text-xs font-normal text-muted-foreground py-2 pr-4 w-[120px]">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody>
                {sessionList?.sessions?.map((s) => (
                  <SessionRow key={s.id} session={s} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
