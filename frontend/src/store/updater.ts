// ---------------------------------------------------------------------------
// Transfera v2 — App Updater Store (Zustand)
// Coordinates background update checks, seamless downloads, installation,
// user preferences, and release changelog presentation.
// ---------------------------------------------------------------------------

import { create } from "zustand";
import { isTauri, restartApp, showNotification } from "@/lib/desktop";

export type UpdateStatus =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "downloaded"
  | "installing"
  | "up-to-date"
  | "error";

export interface UpdateInfo {
  version: string;
  currentVersion: string;
  body?: string;
  date?: string;
}

export interface UpdaterState {
  status: UpdateStatus;
  updateInfo: UpdateInfo | null;
  downloadProgress: number | null;
  downloadedBytes: number | null;
  totalBytes: number | null;
  errorMessage: string | null;
  lastCheckedAt: number | null;
  autoCheckEnabled: boolean;
  autoDownloadEnabled: boolean;
  isSettingsOpen: boolean;

  // Actions
  checkForUpdates: (manual?: boolean) => Promise<void>;
  downloadUpdate: () => Promise<void>;
  installAndRestart: () => Promise<void>;
  dismiss: () => void;
  setAutoCheckEnabled: (enabled: boolean) => void;
  setAutoDownloadEnabled: (enabled: boolean) => void;
  setIsSettingsOpen: (open: boolean) => void;
}

const STORAGE_KEY_AUTO_CHECK = "transfera_updater_auto_check";
const STORAGE_KEY_AUTO_DOWNLOAD = "transfera_updater_auto_download";
const STORAGE_KEY_LAST_CHECKED = "transfera_updater_last_checked";

function getInitialBool(key: string, defaultVal: boolean): boolean {
  try {
    const val = localStorage.getItem(key);
    if (val === null) return defaultVal;
    return val === "true";
  } catch {
    return defaultVal;
  }
}

function getInitialNum(key: string): number | null {
  try {
    const val = localStorage.getItem(key);
    return val ? parseInt(val, 10) : null;
  } catch {
    return null;
  }
}

function updaterErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  try {
    const s = JSON.stringify(err);
    if (s && s !== "{}" && s !== "null") return s;
  } catch {
    /* fall through */
  }
  return fallback;
}

// Module-level reference to the active Tauri Update resource
let activeUpdateResource: any = null;

export const useUpdaterStore = create<UpdaterState>((set, get) => ({
  status: "idle",
  updateInfo: null,
  downloadProgress: null,
  downloadedBytes: null,
  totalBytes: null,
  errorMessage: null,
  lastCheckedAt: getInitialNum(STORAGE_KEY_LAST_CHECKED),
  autoCheckEnabled: getInitialBool(STORAGE_KEY_AUTO_CHECK, true),
  autoDownloadEnabled: getInitialBool(STORAGE_KEY_AUTO_DOWNLOAD, true),
  isSettingsOpen: false,

  checkForUpdates: async (manual = false) => {
    const { status, autoDownloadEnabled } = get();
    if (
      status === "checking" ||
      status === "downloading" ||
      status === "installing"
    ) {
      return;
    }

    set({ status: "checking", errorMessage: null });

    const now = Date.now();
    try {
      localStorage.setItem(STORAGE_KEY_LAST_CHECKED, now.toString());
    } catch {
      /* ignore */
    }
    set({ lastCheckedAt: now });

    if (!isTauri) {
      if (manual) {
        set({ status: "up-to-date" });
        const { useTransferStore } = await import("@/store/transfer");
        useTransferStore
          .getState()
          .showNotification(
            "info",
            "Transfera is up to date (web preview mode).",
          );
      } else {
        set({ status: "idle" });
      }
      return;
    }

    try {
      const { check } = await import("@tauri-apps/plugin-updater");
      const update = await check();

      let currentVersion = "2.8.5";
      try {
        const { getVersion } = await import("@tauri-apps/api/app");
        currentVersion = await getVersion();
      } catch {
        /* fallback */
      }

      if (update && update.available) {
        activeUpdateResource = update;
        const info: UpdateInfo = {
          version: update.version,
          currentVersion,
          body: update.body,
          date: update.date,
        };
        set({ updateInfo: info });

        // If background auto-download is enabled and user didn't trigger manual check,
        // start downloading immediately in background (unless active transfer is running).
        const { useTransferStore } = await import("@/store/transfer");
        const isTransferring =
          useTransferStore.getState().transfer.status === "running";

        if (autoDownloadEnabled && !manual && !isTransferring) {
          // Launch seamless background download
          await get().downloadUpdate();
        } else {
          set({ status: "available" });
        }
      } else {
        activeUpdateResource = null;
        set({ updateInfo: null, status: "up-to-date" });

        if (manual) {
          const { useTransferStore } = await import("@/store/transfer");
          useTransferStore
            .getState()
            .showNotification(
              "success",
              `You're up to date (v${currentVersion}).`,
            );
        }
      }
    } catch (err) {
      console.warn("[updater] Update check failed:", err);
      const msg = updaterErrorMessage(err, "Failed to check for updates.");
      if (manual) {
        set({ status: "error", errorMessage: msg });
        const { useTransferStore } = await import("@/store/transfer");
        useTransferStore.getState().showNotification("error", msg);
      } else {
        set({ status: "idle" });
      }
    }
  },

  downloadUpdate: async () => {
    if (!activeUpdateResource || !isTauri) return;

    set({
      status: "downloading",
      downloadProgress: 0,
      downloadedBytes: 0,
      totalBytes: null,
      errorMessage: null,
    });

    try {
      let total = 0;
      let downloaded = 0;

      await activeUpdateResource.download((event: any) => {
        if (!event) return;
        if (event.event === "Started") {
          total = event.data?.contentLength || 0;
          set({ totalBytes: total });
        } else if (event.event === "Progress") {
          downloaded += event.data?.chunkLength || 0;
          set({ downloadedBytes: downloaded });
          if (total > 0) {
            const pct = Math.min(100, Math.round((downloaded / total) * 100));
            set({ downloadProgress: pct });
          }
        } else if (event.event === "Finished") {
          set({ downloadProgress: 100 });
        }
      });

      set({
        status: "downloaded",
        downloadProgress: 100,
      });

      // Notify user non-intrusively that update is ready to install
      const targetVersion = get().updateInfo?.version || "";
      showNotification({
        title: "Transfera Update Ready",
        body: `Transfera v${targetVersion} is downloaded and ready to install.`,
        sessionId: 0,
      }).catch(() => {});
    } catch (err) {
      console.error("[updater] Download failed:", err);
      const msg = updaterErrorMessage(err, "Failed to download update.");
      set({ status: "error", errorMessage: msg });
    }
  },

  installAndRestart: async () => {
    const { useTransferStore } = await import("@/store/transfer");
    const isTransferring =
      useTransferStore.getState().transfer.status === "running";

    if (isTransferring) {
      useTransferStore
        .getState()
        .showNotification(
          "warning",
          "A transfer is currently in progress. Please pause or complete the transfer before updating.",
        );
      return;
    }

    if (!activeUpdateResource || !isTauri) return;

    try {
      set({ status: "installing", errorMessage: null });

      // In Tauri v2 Windows, install launches the updater installer and exits.
      // Option restartAfterInstall defaults to true.
      await activeUpdateResource.install({ restartAfterInstall: true });

      // Fallback relaunch in case installer didn't exit the shell
      setTimeout(async () => {
        try {
          await restartApp();
        } catch {
          /* ignore */
        }
      }, 2500);
    } catch (err) {
      console.error("[updater] Installation failed:", err);
      const msg = updaterErrorMessage(err, "Failed to install update.");
      set({ status: "error", errorMessage: msg });
    }
  },

  dismiss: () => {
    const { status } = get();
    // If downloaded, keep updateInfo so UI pills and settings know it's ready,
    // but close the toast / banner
    if (status === "downloaded") {
      set({ status: "idle" });
    } else {
      set({
        status: "idle",
        updateInfo: null,
        downloadProgress: null,
        errorMessage: null,
      });
    }
  },

  setAutoCheckEnabled: (enabled: boolean) => {
    try {
      localStorage.setItem(STORAGE_KEY_AUTO_CHECK, enabled.toString());
    } catch {
      /* ignore */
    }
    set({ autoCheckEnabled: enabled });
  },

  setAutoDownloadEnabled: (enabled: boolean) => {
    try {
      localStorage.setItem(STORAGE_KEY_AUTO_DOWNLOAD, enabled.toString());
    } catch {
      /* ignore */
    }
    set({ autoDownloadEnabled: enabled });
  },

  setIsSettingsOpen: (open: boolean) => {
    set({ isSettingsOpen: open });
  },
}));
