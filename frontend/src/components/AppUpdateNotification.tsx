import { motion, AnimatePresence } from "framer-motion";
import { Sparkles, Download, RefreshCw, X, AlertCircle } from "lucide-react";
import { useAppUpdater } from "@/hooks/use-app-updater";

export default function AppUpdateNotification() {
  const {
    status,
    updateInfo,
    downloadProgress,
    errorMessage,
    downloadAndInstall,
    dismiss,
  } = useAppUpdater(true);

  if (status === "idle" || status === "checking" || status === "up-to-date") {
    return null;
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0, y: 20, scale: 0.95 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 20, scale: 0.95 }}
        transition={{ duration: 0.2, ease: "easeOut" }}
        className="fixed bottom-6 right-6 z-50 max-w-sm w-full bg-card/95 backdrop-blur-md border border-border shadow-2xl rounded-2xl p-4 flex flex-col gap-3 text-foreground"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-action/10 text-action flex items-center justify-center shrink-0">
              {status === "error" ? (
                <AlertCircle className="w-4 h-4 text-destructive" />
              ) : (
                <Sparkles className="w-4 h-4 text-action" />
              )}
            </div>
            <div>
              <p className="text-xs font-semibold leading-tight text-foreground">
                {status === "available" && `Transfera v${updateInfo?.version} is available`}
                {status === "downloading" && `Downloading update...`}
                {status === "ready" && `Update installed!`}
                {status === "error" && `Update failed`}
              </p>
              <p className="text-[11px] text-muted-foreground mt-0.5 leading-tight">
                {status === "available" && "An in-place update is ready to install."}
                {status === "downloading" &&
                  (downloadProgress !== null ? `${downloadProgress}% completed` : "Please wait...")}
                {status === "ready" && "Relaunching Transfera now..."}
                {status === "error" && (errorMessage || "Could not complete update.")}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={dismiss}
            disabled={status === "downloading" || status === "ready"}
            className="p-1 rounded-full text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors disabled:opacity-0"
            aria-label="Dismiss"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Progress bar when downloading */}
        {status === "downloading" && (
          <div className="w-full bg-muted rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-action h-full transition-all duration-200"
              style={{ width: `${downloadProgress ?? 10}%` }}
            />
          </div>
        )}

        {/* Action buttons */}
        {status === "available" && (
          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={dismiss}
              className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground rounded-pill hover:bg-muted transition-colors active:scale-[0.95]"
            >
              Later
            </button>
            <button
              type="button"
              onClick={downloadAndInstall}
              className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-action text-white text-xs font-normal rounded-pill hover:bg-action/90 active:scale-[0.95] transition-all shadow-xs"
            >
              <Download className="w-3.5 h-3.5" />
              Update &amp; Restart
            </button>
          </div>
        )}

        {status === "downloading" && (
          <div className="flex items-center justify-center py-0.5 text-xs text-muted-foreground gap-1.5">
            <RefreshCw className="w-3.5 h-3.5 animate-spin text-action" />
            <span>Updating in-place...</span>
          </div>
        )}
      </motion.div>
    </AnimatePresence>
  );
}
