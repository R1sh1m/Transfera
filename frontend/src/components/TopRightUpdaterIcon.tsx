// ---------------------------------------------------------------------------
// Transfera v2 — Top Right Updater Icon
// Compact title-bar indicator that hosts the background updater/installer
// when the user hides the notification banner. Clicking it unhides and resumes
// the update view from where it was left.
// ---------------------------------------------------------------------------

import { RefreshCw, Sparkles, AlertCircle } from "lucide-react";
import { useUpdaterStore } from "@/store/updater";

export default function TopRightUpdaterIcon() {
  const status = useUpdaterStore((s) => s.status);
  const updateInfo = useUpdaterStore((s) => s.updateInfo);
  const downloadProgress = useUpdaterStore((s) => s.downloadProgress);
  const isBannerHidden = useUpdaterStore((s) => s.isBannerHidden);
  const unhideBanner = useUpdaterStore((s) => s.unhideBanner);

  // When downloading: always show progress indicator in top right so the user
  // knows the download is humming in background. Clicking unhides banner / opens view.
  if (status === "downloading") {
    return (
      <button
        type="button"
        onClick={() => {
          unhideBanner();
        }}
        className="no-drag inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-action/10 text-action hover:bg-action/20 text-xs font-normal transition-all active:scale-[0.95] cursor-pointer"
        title={`Transfera update downloading (${downloadProgress ?? 0}%) — click to resume view`}
        aria-label="Update downloading in background"
      >
        <RefreshCw className="w-3 h-3 animate-spin text-action" />
        <span className="text-[11px] font-medium text-action">
          {downloadProgress !== null ? `${downloadProgress}%` : "Updating..."}
        </span>
      </button>
    );
  }

  // When downloaded: show "Update Ready" pill
  if (status === "downloaded") {
    return (
      <button
        type="button"
        onClick={() => {
          unhideBanner();
        }}
        className="no-drag inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-action text-white hover:bg-action/90 text-xs font-normal transition-all active:scale-[0.95] shadow-xs cursor-pointer"
        title="Transfera update ready — click to review and restart"
        aria-label="Transfera update ready"
      >
        <Sparkles className="w-3 h-3 text-white" />
        <span className="text-[11px] font-medium text-white">Update Ready</span>
      </button>
    );
  }

  // When update is available and banner was hidden
  if (status === "available" && isBannerHidden) {
    return (
      <button
        type="button"
        onClick={() => {
          unhideBanner();
        }}
        className="no-drag inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-action/10 text-action hover:bg-action/20 text-xs font-normal transition-all active:scale-[0.95] cursor-pointer"
        title={`Transfera v${updateInfo?.version || ""} available — click to view`}
        aria-label="Transfera update available"
      >
        <Sparkles className="w-3 h-3 text-action" />
        <span className="text-[11px] font-medium text-action">
          v{updateInfo?.version || "New"}
        </span>
      </button>
    );
  }

  // When update had an error and banner was hidden
  if (status === "error" && isBannerHidden) {
    return (
      <button
        type="button"
        onClick={() => {
          unhideBanner();
        }}
        className="no-drag inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-destructive/10 text-destructive hover:bg-destructive/20 text-xs font-normal transition-all active:scale-[0.95] cursor-pointer"
        title="Update check issue — click to view"
        aria-label="Update check issue"
      >
        <AlertCircle className="w-3 h-3 text-destructive" />
        <span className="text-[11px] font-medium text-destructive">Update</span>
      </button>
    );
  }

  return null;
}
