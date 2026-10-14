// ---------------------------------------------------------------------------
// Transfera v2 — Software Update Settings Modal
// Comprehensive update management dialog: current version, background check
// toggles, auto-download settings, live progress, and release notes.
// ---------------------------------------------------------------------------

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Sparkles,
  Download,
  RefreshCw,
  CheckCircle2,
  AlertCircle,
  X,
  ExternalLink,
  ChevronDown,
  ChevronUp,
  ShieldCheck,
} from "lucide-react";
import { useUpdaterStore } from "@/store/updater";
import { openExternal } from "@/lib/desktop";
import ReleaseNotesView from "@/components/ReleaseNotesView";

function formatTimestamp(ts: number | null): string {
  if (!ts) return "Never";
  const date = new Date(ts);
  const now = new Date();
  const isToday =
    date.getDate() === now.getDate() &&
    date.getMonth() === now.getMonth() &&
    date.getFullYear() === now.getFullYear();

  const timeStr = date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (isToday) return `Today at ${timeStr}`;
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })} at ${timeStr}`;
}

export default function UpdateSettingsModal() {
  const isSettingsOpen = useUpdaterStore((s) => s.isSettingsOpen);
  const setIsSettingsOpen = useUpdaterStore((s) => s.setIsSettingsOpen);
  const status = useUpdaterStore((s) => s.status);
  const updateInfo = useUpdaterStore((s) => s.updateInfo);
  const downloadProgress = useUpdaterStore((s) => s.downloadProgress);
  const errorMessage = useUpdaterStore((s) => s.errorMessage);
  const lastCheckedAt = useUpdaterStore((s) => s.lastCheckedAt);
  const autoCheckEnabled = useUpdaterStore((s) => s.autoCheckEnabled);
  const autoDownloadEnabled = useUpdaterStore((s) => s.autoDownloadEnabled);

  const checkForUpdates = useUpdaterStore((s) => s.checkForUpdates);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const installAndRestart = useUpdaterStore((s) => s.installAndRestart);
  const setAutoCheckEnabled = useUpdaterStore((s) => s.setAutoCheckEnabled);
  const setAutoDownloadEnabled = useUpdaterStore(
    (s) => s.setAutoDownloadEnabled,
  );

  const [notesExpanded, setNotesExpanded] = useState(false);

  if (!isSettingsOpen) return null;

  const currentVersion = updateInfo?.currentVersion || "1.0.0";

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm">
        <motion.div
          initial={{ opacity: 0, scale: 0.96, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.96, y: 10 }}
          transition={{ duration: 0.18, ease: "easeOut" }}
          className="relative w-full max-w-lg bg-card border border-border shadow-2xl rounded-2xl overflow-hidden flex flex-col max-h-[85vh]"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-card/60 backdrop-blur-md">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-action/10 text-action flex items-center justify-center">
                <Sparkles className="w-4 h-4" />
              </div>
              <div>
                <h2 className="text-sm font-semibold text-foreground">
                  Software Update
                </h2>
                <p className="text-xs text-muted-foreground">
                  Keep Transfera automatic &amp; up to date
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setIsSettingsOpen(false)}
              className="p-1.5 rounded-full text-muted-foreground hover:text-foreground hover:bg-muted/80 transition-colors"
              aria-label="Close"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Body */}
          <div className="p-6 space-y-6 overflow-y-auto flex-1">
            {/* Version & Status Card */}
            <div className="p-4 rounded-xl border border-border bg-muted/30 flex flex-col gap-3">
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-foreground">
                      Transfera v{currentVersion}
                    </span>
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-action/10 text-action">
                      <ShieldCheck className="w-3 h-3" />
                      Verified Desktop
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Last checked: {formatTimestamp(lastCheckedAt)}
                  </p>
                </div>

                {/* Primary Action Button */}
                <div>
                  {status === "checking" && (
                    <button
                      type="button"
                      disabled
                      className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-pill bg-muted text-muted-foreground text-xs opacity-75 cursor-default"
                    >
                      <RefreshCw className="w-3.5 h-3.5 animate-spin text-action" />
                      Checking...
                    </button>
                  )}

                  {status === "downloading" && (
                    <button
                      type="button"
                      disabled
                      className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-pill bg-muted text-muted-foreground text-xs opacity-75 cursor-default"
                    >
                      <RefreshCw className="w-3.5 h-3.5 animate-spin text-action" />
                      {downloadProgress !== null
                        ? `${downloadProgress}%`
                        : "Downloading..."}
                    </button>
                  )}

                  {status === "available" && (
                    <button
                      type="button"
                      onClick={() => downloadUpdate()}
                      className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] transition-all shadow-xs"
                    >
                      <Download className="w-3.5 h-3.5" />
                      Download v{updateInfo?.version}
                    </button>
                  )}

                  {status === "downloaded" && (
                    <button
                      type="button"
                      onClick={() => installAndRestart()}
                      className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-pill bg-action text-white text-xs hover:bg-action/90 active:scale-[0.95] transition-all shadow-xs"
                    >
                      <Sparkles className="w-3.5 h-3.5" />
                      Restart &amp; Install
                    </button>
                  )}

                  {(status === "idle" ||
                    status === "up-to-date" ||
                    status === "error") && (
                    <button
                      type="button"
                      onClick={() => checkForUpdates(true)}
                      className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-pill border border-border bg-card hover:bg-muted text-foreground text-xs active:scale-[0.95] transition-all"
                    >
                      <RefreshCw className="w-3.5 h-3.5 text-muted-foreground" />
                      Check for Updates
                    </button>
                  )}
                </div>
              </div>

              {/* Status Banner */}
              {status === "up-to-date" && (
                <div className="flex items-center gap-2 text-xs text-green-600 dark:text-green-400 pt-1 border-t border-border/50">
                  <CheckCircle2 className="w-3.5 h-3.5 shrink-0" />
                  <span>Transfera is up to date with the latest features.</span>
                </div>
              )}

              {status === "available" && (
                <div className="flex items-center gap-2 text-xs text-action pt-1 border-t border-border/50">
                  <Sparkles className="w-3.5 h-3.5 shrink-0" />
                  <span>
                    Transfera v{updateInfo?.version} is available to install.
                  </span>
                </div>
              )}

              {status === "downloading" && (
                <div className="space-y-1.5 pt-1 border-t border-border/50">
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>Downloading update in background...</span>
                    <span>{downloadProgress ?? 0}%</span>
                  </div>
                  <div className="w-full bg-muted rounded-full h-1.5 overflow-hidden">
                    <div
                      className="bg-action h-full transition-all duration-200"
                      style={{ width: `${downloadProgress ?? 10}%` }}
                    />
                  </div>
                </div>
              )}

              {status === "downloaded" && (
                <div className="flex items-center gap-2 text-xs text-action pt-1 border-t border-border/50">
                  <Sparkles className="w-3.5 h-3.5 shrink-0" />
                  <span>
                    Update ready! Restart now to run v{updateInfo?.version}.
                  </span>
                </div>
              )}

              {status === "error" && (
                <div className="flex items-center gap-2 text-xs text-destructive pt-1 border-t border-border/50">
                  <AlertCircle className="w-3.5 h-3.5 shrink-0" />
                  <span
                    className="break-words line-clamp-3 overflow-hidden"
                    title={
                      errorMessage || "Unable to check for updates right now."
                    }
                  >
                    {errorMessage || "Unable to check for updates right now."}
                  </span>
                </div>
              )}
            </div>

            {/* Release Notes / What's New Accordion */}
            {updateInfo?.version && (
              <div className="rounded-xl border border-border p-3.5 bg-card space-y-2">
                <button
                  type="button"
                  onClick={() => setNotesExpanded(!notesExpanded)}
                  className="w-full flex items-center justify-between text-xs font-medium text-foreground hover:text-action transition-colors"
                >
                  <span className="flex items-center gap-1.5">
                    <Sparkles className="w-3.5 h-3.5 text-action" />
                    What's New in v{updateInfo.version}
                  </span>
                  {notesExpanded ? (
                    <ChevronUp className="w-4 h-4" />
                  ) : (
                    <ChevronDown className="w-4 h-4" />
                  )}
                </button>

                {notesExpanded && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: "auto" }}
                    exit={{ opacity: 0, height: 0 }}
                    className="pt-2 border-t border-border"
                  >
                    <ReleaseNotesView
                      version={updateInfo.version}
                      body={updateInfo.body}
                      maxHeightClass="max-h-52"
                    />
                  </motion.div>
                )}
              </div>
            )}

            {/* Automation Preferences */}
            <div className="space-y-4">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Automatic Updates
              </h3>

              {/* Toggle 1: Auto-check */}
              <label className="flex items-center justify-between gap-4 p-3 rounded-xl border border-border bg-card hover:bg-muted/30 transition-colors cursor-pointer">
                <div className="space-y-0.5">
                  <span className="text-xs font-medium text-foreground block">
                    Automatically check for updates
                  </span>
                  <span className="text-[11px] text-muted-foreground block">
                    Periodically checks in the background (every 4 hours) so you
                    never miss new features.
                  </span>
                </div>
                <input
                  type="checkbox"
                  checked={autoCheckEnabled}
                  onChange={(e) => setAutoCheckEnabled(e.target.checked)}
                  className="w-4 h-4 rounded text-action focus:ring-action border-border cursor-pointer accent-[#0066cc]"
                />
              </label>

              {/* Toggle 2: Auto-download */}
              <label className="flex items-center justify-between gap-4 p-3 rounded-xl border border-border bg-card hover:bg-muted/30 transition-colors cursor-pointer">
                <div className="space-y-0.5">
                  <span className="text-xs font-medium text-foreground block">
                    Automatically download updates in the background
                  </span>
                  <span className="text-[11px] text-muted-foreground block">
                    Silently downloads new updates without interrupting your
                    work, ready for 1-click restart.
                  </span>
                </div>
                <input
                  type="checkbox"
                  checked={autoDownloadEnabled}
                  onChange={(e) => setAutoDownloadEnabled(e.target.checked)}
                  className="w-4 h-4 rounded text-action focus:ring-action border-border cursor-pointer accent-[#0066cc]"
                />
              </label>
            </div>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-6 py-3.5 border-t border-border bg-muted/20 text-xs">
            <button
              type="button"
              onClick={() =>
                openExternal("https://github.com/R1sh1m/Transfera/releases")
              }
              className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-action transition-colors"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              View Release History on GitHub
            </button>
            <button
              type="button"
              onClick={() => setIsSettingsOpen(false)}
              className="px-4 py-1.5 rounded-pill border border-border hover:bg-muted text-foreground transition-colors active:scale-[0.95]"
            >
              Done
            </button>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
