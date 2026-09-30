// ---------------------------------------------------------------------------
// Transfera v2 — Close Guard Modal (ROUGH-3)
// Warns the user when closing the window while a media transfer or scan is
// actively running, preventing accidental cancellation and hard kills.
// Adheres strictly to DESIGN.md tokens: rounded-pill CTAs, 44px touch targets,
// active:scale-[0.95], 600-weight headline with negative tracking, 400-weight body.
// ---------------------------------------------------------------------------

import { useState, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, HardDrive, Loader2, X } from "lucide-react";
import { useTransferStore } from "@/store/transfer";
import { forceExit, onRequestClose } from "@/lib/desktop";

interface CloseGuardModalProps {
  /** If provided, parent can also programmatically trigger close check */
  isOpen?: boolean;
  onCancel?: () => void;
}

export default function CloseGuardModal({
  isOpen: externalOpen,
  onCancel: externalCancel,
}: CloseGuardModalProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [isExiting, setIsExiting] = useState(false);

  const transfer = useTransferStore((s) => s.transfer);
  const scan = useTransferStore((s) => s.scan);

  const isTransferActive =
    transfer.status === "running" ||
    transfer.status === "paused" ||
    scan.isScanning;

  const isOpen = externalOpen ?? internalOpen;

  const handleCloseRequest = useCallback(() => {
    if (!isTransferActive) {
      void forceExit();
      return;
    }
    setInternalOpen(true);
  }, [isTransferActive]);

  useEffect(() => {
    const unlisten = onRequestClose(() => {
      handleCloseRequest();
    });
    return () => {
      unlisten();
    };
  }, [handleCloseRequest]);

  // Escape key cancels close
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handleCancel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  const handleCancel = () => {
    setInternalOpen(false);
    externalCancel?.();
  };

  const handleConfirmExit = async () => {
    setIsExiting(true);
    await forceExit();
  };

  return (
    <AnimatePresence>
      {isOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          {/* Liquid-glass neutral backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={handleCancel}
            className="fixed inset-0 bg-black/60 backdrop-blur-xs"
          />

          {/* Modal Surface */}
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: 6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 6 }}
            transition={{ duration: 0.15 }}
            className="relative bg-card border border-border w-full max-w-md rounded-2xl p-6 shadow-2xl space-y-5 z-10"
          >
            {/* Header */}
            <div className="flex items-start gap-4">
              <div className="w-11 h-11 rounded-full bg-amber-500/10 border border-amber-500/20 flex items-center justify-center shrink-0">
                <AlertTriangle className="w-5 h-5 text-amber-500" />
              </div>
              <div className="flex-1 min-w-0 pr-6">
                <h3 className="text-base font-semibold text-foreground tracking-tight">
                  Transfer in Progress
                </h3>
                <p className="text-[14px] leading-relaxed text-muted-foreground mt-1">
                  A media operation is actively running. Quitting now will pause the
                  transfer. Progress is saved and will resume on next launch.
                </p>
              </div>
              <button
                onClick={handleCancel}
                aria-label="Dismiss close warning"
                className="no-drag absolute top-5 right-5 p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Active Operation Status Card */}
            <div className="rounded-xl border border-border bg-muted/40 p-3.5 space-y-2">
              <div className="flex items-center justify-between text-xs font-normal">
                <div className="flex items-center gap-2 truncate text-foreground">
                  <HardDrive className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                  <span className="truncate font-semibold">
                    {transfer.sessionName || "Media Vaulting Session"}
                  </span>
                </div>
                <span className="text-muted-foreground shrink-0 tabular-nums">
                  {scan.isScanning
                    ? `Scanning (${scan.scannedFiles} files)`
                    : `${Math.round(transfer.progressPercent)}%`}
                </span>
              </div>

              {/* Progress bar */}
              <div className="w-full h-1.5 rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full bg-action transition-all duration-300 rounded-full"
                  style={{
                    width: scan.isScanning
                      ? "100%"
                      : `${Math.min(100, Math.max(0, transfer.progressPercent))}%`,
                  }}
                />
              </div>

              <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                <span>
                  {scan.isScanning
                    ? "Cataloging device media…"
                    : `${transfer.importedFiles || transfer.completedItems} of ${transfer.totalFiles || transfer.totalItems} files verified`}
                </span>
                <span className="capitalize">{transfer.status}</span>
              </div>
            </div>

            {/* Actions (Pill CTAs, 44px min touch target, active:scale-[0.95]) */}
            <div className="flex items-center justify-end gap-3 pt-1">
              <button
                type="button"
                onClick={handleConfirmExit}
                disabled={isExiting}
                className="no-drag min-h-[44px] px-5 py-2.5 rounded-pill border border-border text-xs text-muted-foreground hover:text-destructive hover:bg-destructive/10 hover:border-destructive/30 active:scale-[0.95] transition-all flex items-center justify-center gap-2"
              >
                {isExiting ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Closing…</span>
                  </>
                ) : (
                  <span>Quit Anyway</span>
                )}
              </button>

              <button
                type="button"
                onClick={handleCancel}
                disabled={isExiting}
                className="no-drag min-h-[44px] px-5 py-2.5 rounded-pill bg-action text-white hover:bg-action/90 active:scale-[0.95] text-xs font-semibold transition-all flex items-center justify-center shadow-xs"
              >
                Keep Transferring
              </button>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}
