// ---------------------------------------------------------------------------
// Transfera v2 — App Updater Hook
// Bridges UI components to the centralized useUpdaterStore.
// ---------------------------------------------------------------------------

import { useUpdaterStore } from "@/store/updater";
export type { UpdateStatus, UpdateInfo } from "@/store/updater";

export function useAppUpdater() {
  const status = useUpdaterStore((s) => s.status);
  const updateInfo = useUpdaterStore((s) => s.updateInfo);
  const downloadProgress = useUpdaterStore((s) => s.downloadProgress);
  const downloadedBytes = useUpdaterStore((s) => s.downloadedBytes);
  const totalBytes = useUpdaterStore((s) => s.totalBytes);
  const errorMessage = useUpdaterStore((s) => s.errorMessage);
  const autoCheckEnabled = useUpdaterStore((s) => s.autoCheckEnabled);
  const autoDownloadEnabled = useUpdaterStore((s) => s.autoDownloadEnabled);
  const lastCheckedAt = useUpdaterStore((s) => s.lastCheckedAt);

  const checkForUpdates = useUpdaterStore((s) => s.checkForUpdates);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const installAndRestart = useUpdaterStore((s) => s.installAndRestart);
  const dismiss = useUpdaterStore((s) => s.dismiss);
  const setAutoCheckEnabled = useUpdaterStore((s) => s.setAutoCheckEnabled);
  const setAutoDownloadEnabled = useUpdaterStore((s) => s.setAutoDownloadEnabled);
  const setIsSettingsOpen = useUpdaterStore((s) => s.setIsSettingsOpen);

  return {
    status,
    updateInfo,
    downloadProgress,
    downloadedBytes,
    totalBytes,
    errorMessage,
    autoCheckEnabled,
    autoDownloadEnabled,
    lastCheckedAt,
    checkForUpdates,
    downloadUpdate,
    installAndRestart,
    // Alias downloadAndInstall to downloadUpdate or installAndRestart depending on status
    downloadAndInstall: status === "downloaded" ? installAndRestart : downloadUpdate,
    dismiss,
    setAutoCheckEnabled,
    setAutoDownloadEnabled,
    setIsSettingsOpen,
  };
}
