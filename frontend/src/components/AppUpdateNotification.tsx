// ---------------------------------------------------------------------------
// Transfera v2 — App Update Notification
// Floating liquid-glass banner for update announcements, background download
// progress, 1-click restart, and expandable release highlights.
// ---------------------------------------------------------------------------

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Sparkles,
  Download,
  RefreshCw,
  X,
  AlertCircle,
  ChevronDown,
  ChevronUp,
  Settings2,
} from "lucide-react";
import { useUpdaterStore } from "@/store/updater";
import ReleaseNotesView from "@/components/ReleaseNotesView";

export default function AppUpdateNotification() {
  const status = useUpdaterStore((s) => s.status);
  const updateInfo = useUpdaterStore((s) => s.updateInfo);
  const downloadProgress = useUpdaterStore((s) => s.downloadProgress);
  const errorMessage = useUpdaterStore((s) => s.errorMessage);
  const isBannerHidden = useUpdaterStore((s) => s.isBannerHidden);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const cancelDownload = useUpdaterStore((s) => s.cancelDownload);
  const installAndRestart = useUpdaterStore((s) => s.installAndRestart);
  const dismiss = useUpdaterStore((s) => s.dismiss);
  const hideBanner = useUpdaterStore((s) => s.hideBanner);
  const setIsSettingsOpen = useUpdaterStore((s) => s.setIsSettingsOpen);
  const checkForUpdates = useUpdaterStore((s) => s.checkForUpdates);

  const [notesExpanded, setNotesExpanded] = useState(false);

  const showBanner =
    !isBannerHidden &&
    status !== "idle" &&
    status !== "checking" &&
    status !== "up-to-date";

  return (
    <AnimatePresence>
      {showBanner && (
        <motion.div
          key="update-banner"
          initial={{ opacity: 0, y: 20, scale: 0.95 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 20, scale: 0.95 }}
          transition={{ duration: 0.2, ease: "easeOut" }}
          className="fixed bottom-6 right-6 z-50 max-w-sm w-full bg-card/95 backdrop-blur-md border border-border shadow-2xl rounded-2xl p-4 flex flex-col gap-3 text-foreground"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${
                  status === "error"
                    ? "bg-destructive/10 text-destructive"
                    : "bg-action/10 text-action"
                }`}
              >
                {status === "error" ? (
                  <AlertCircle className="w-4 h-4 text-destructive" />
                ) : (
                  <Sparkles className="w-4 h-4 text-action" />
                )}
              </div>
              <div>
                <p className="text-xs font-semibold leading-tight text-foreground">
                  {status === "available" &&
                    `Transfera v${updateInfo?.version} is available`}
                  {status === "downloading" && `Downloading update...`}
                  {status === "downloaded" && `Update ready to install!`}
                  {status === "installing" && `Installing update...`}
                  {status === "error" && `Update check issue`}
                </p>
                {status === "error" ? (
                  <p
                    className="text-[11px] text-muted-foreground mt-0.5 leading-tight break-words line-clamp-3 overflow-hidden"
                    title={errorMessage || "Could not complete update check."}
                  >
                    {errorMessage || "Could not complete update check."}
                  </p>
                ) : (
                  <p className="text-[11px] text-muted-foreground mt-0.5 leading-tight">
                    {status === "available" &&
                      "A new version with the latest features is available."}
                    {status === "downloading" &&
                      (downloadProgress !== null
                        ? `${downloadProgress}% downloaded in background`
                        : "Downloading in background...")}
                    {status === "downloaded" &&
                      "Restart Transfera to apply the update and enjoy the latest features."}
                    {status === "installing" && "Relaunching Transfera..."}
                  </p>
                )}
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => setIsSettingsOpen(true)}
                className="p-1 rounded-full text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors cursor-pointer"
                title="Update Preferences"
                aria-label="Update Preferences"
              >
                <Settings2 className="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                onClick={() => {
                  if (status === "downloading") {
                    cancelDownload();
                  } else {
                    dismiss();
                  }
                }}
                disabled={status === "installing"}
                className="p-1 rounded-full text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors disabled:opacity-0 cursor-pointer"
                title={status === "downloading" ? "Cancel update operation" : "Dismiss"}
                aria-label={status === "downloading" ? "Cancel update operation" : "Dismiss"}
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          {/* Progress bar when downloading */}
          {status === "downloading" && (
            <div className="w-full bg-muted rounded-full h-1.5 overflow-hidden">
              <div
                className="bg-action h-full transition-[width] duration-150 ease-out"
                style={{
                  width: `${Math.max(0, Math.min(100, downloadProgress ?? 0))}%`,
                }}
              />
            </div>
          )}

          {/* Expandable Release Notes */}
          {updateInfo?.version && (
            <div className="border-t border-border/50 pt-1.5">
              <button
                type="button"
                onClick={() => setNotesExpanded(!notesExpanded)}
                className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
              >
                <span>What's new in v{updateInfo.version}</span>
                {notesExpanded ? (
                  <ChevronUp className="w-3 h-3" />
                ) : (
                  <ChevronDown className="w-3 h-3" />
                )}
              </button>
              {notesExpanded && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  className="mt-1.5 p-2.5 bg-muted/30 rounded-xl border border-border/40"
                >
                  <ReleaseNotesView
                    version={updateInfo.version}
                    body={updateInfo.body}
                    maxHeightClass="max-h-36"
                    compact
                  />
                </motion.div>
              )}
            </div>
          )}

          {/* Action buttons */}
          {status === "available" && (
            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={hideBanner}
                className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground rounded-pill hover:bg-muted transition-colors active:scale-[0.95] cursor-pointer"
              >
                Later
              </button>
              <button
                type="button"
                onClick={() => downloadUpdate()}
                className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-action text-white text-xs font-normal rounded-pill hover:bg-action/90 active:scale-[0.95] transition-all shadow-xs cursor-pointer"
              >
                <Download className="w-3.5 h-3.5" />
                Download Update
              </button>
            </div>
          )}

          {status === "downloaded" && (
            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={hideBanner}
                className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground rounded-pill hover:bg-muted transition-colors active:scale-[0.95] cursor-pointer"
              >
                Later
              </button>
              <button
                type="button"
                onClick={() => installAndRestart()}
                className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-action text-white text-xs font-normal rounded-pill hover:bg-action/90 active:scale-[0.95] transition-all shadow-xs cursor-pointer"
              >
                <Sparkles className="w-3.5 h-3.5" />
                Restart &amp; Update
              </button>
            </div>
          )}

          {status === "downloading" && (
            <div className="flex items-center justify-between py-0.5 text-xs text-muted-foreground">
              <div className="flex items-center gap-1.5">
                <RefreshCw className="w-3.5 h-3.5 animate-spin text-action" />
                <span className="text-[11px]">Downloading in background...</span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={hideBanner}
                  className="text-[11px] font-medium text-action hover:underline cursor-pointer"
                  title="Hide onto top-right icon (continues in background)"
                >
                  Hide
                </button>
                <button
                  type="button"
                  onClick={cancelDownload}
                  className="text-[11px] text-muted-foreground hover:text-destructive cursor-pointer"
                  title="Cancel update download"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {status === "installing" && (
            <div className="flex items-center justify-center py-0.5 text-xs text-muted-foreground gap-1.5">
              <RefreshCw className="w-3.5 h-3.5 animate-spin text-action" />
              <span>Restarting Transfera...</span>
            </div>
          )}

          {status === "error" && (
            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={dismiss}
                className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground rounded-pill hover:bg-muted transition-colors active:scale-[0.95] cursor-pointer"
              >
                Dismiss
              </button>
              <button
                type="button"
                onClick={() => checkForUpdates(true)}
                className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-action text-white text-xs font-normal rounded-pill hover:bg-action/90 active:scale-[0.95] transition-all shadow-xs cursor-pointer"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                Retry
              </button>
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
