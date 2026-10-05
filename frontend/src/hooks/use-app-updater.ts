import { useState, useEffect, useCallback, useRef } from "react";
import { isTauri } from "@/lib/desktop";

export interface UpdateInfo {
  version: string;
  body?: string;
  date?: string;
}

export type UpdateStatus =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "ready"
  | "up-to-date"
  | "error";

export function useAppUpdater(autoCheck: boolean = true) {
  const [status, setStatus] = useState<UpdateStatus>("idle");
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Store the active update object from tauri-plugin-updater
  const updateHandleRef = useRef<any>(null);

  const checkForUpdates = useCallback(async (manual: boolean = false) => {
    if (!isTauri) {
      if (manual) setStatus("up-to-date");
      return;
    }

    try {
      setStatus("checking");
      setErrorMessage(null);

      const { check } = await import("@tauri-apps/plugin-updater");
      const update = await check();

      if (update && update.available) {
        updateHandleRef.current = update;
        setUpdateInfo({
          version: update.version,
          body: update.body,
          date: update.date,
        });
        setStatus("available");
      } else {
        updateHandleRef.current = null;
        setUpdateInfo(null);
        setStatus("up-to-date");
      }
    } catch (err) {
      console.warn("[updater] Update check failed:", err);
      // Don't show intrusive error unless user manually initiated the check
      if (manual) {
        setStatus("error");
        setErrorMessage(
          err instanceof Error ? err.message : "Failed to check for updates.",
        );
      } else {
        setStatus("idle");
      }
    }
  }, []);

  const downloadAndInstall = useCallback(async () => {
    if (!updateHandleRef.current || !isTauri) return;

    try {
      setStatus("downloading");
      setDownloadProgress(0);
      setErrorMessage(null);

      let totalBytes = 0;
      let downloadedBytes = 0;

      await updateHandleRef.current.downloadAndInstall((event: any) => {
        if (!event) return;
        if (event.event === "Started") {
          totalBytes = event.data?.contentLength || 0;
        } else if (event.event === "Progress") {
          downloadedBytes += event.data?.chunkLength || 0;
          if (totalBytes > 0) {
            const pct = Math.min(100, Math.round((downloadedBytes / totalBytes) * 100));
            setDownloadProgress(pct);
          }
        } else if (event.event === "Finished") {
          setDownloadProgress(100);
        }
      });

      setStatus("ready");

      // Relaunch the application into the updated version
      const { relaunch } = await import("@tauri-apps/plugin-process");
      await relaunch();
    } catch (err) {
      console.error("[updater] Download or install failed:", err);
      setStatus("error");
      setErrorMessage(
        err instanceof Error ? err.message : "Failed to install update.",
      );
    }
  }, []);

  const dismiss = useCallback(() => {
    setStatus("idle");
    setUpdateInfo(null);
    setDownloadProgress(null);
  }, []);

  // Run initial check on app startup (after 5 seconds so boot isn't contended)
  useEffect(() => {
    if (!autoCheck) return;
    const timer = setTimeout(() => {
      checkForUpdates(false);
    }, 5000);
    return () => clearTimeout(timer);
  }, [autoCheck, checkForUpdates]);

  return {
    status,
    updateInfo,
    downloadProgress,
    errorMessage,
    checkForUpdates,
    downloadAndInstall,
    dismiss,
  };
}
