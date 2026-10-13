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
  let raw: string | null = null;
  if (err instanceof Error && err.message) raw = err.message;
  else if (typeof err === "string" && err) raw = err;
  else {
    try {
      const s = JSON.stringify(err);
      if (s && s !== "{}" && s !== "null") raw = s;
    } catch {
      /* fall through */
    }
  }
  if (raw) {
    const lower = raw.toLowerCase();
    // Map the updater-plugin ordering error to friendly copy.
    // Raw detail stays in console (callers log `err`); toast/store only gets friendly text.
    // Narrow match: broader "download" text would mislabel check/network
    // failures (which trigger no retry) as a re-download in progress.
    if (lower.includes("install called before")) {
      return "The update wasn't fully downloaded. Trying the download again.";
    }
    return raw;
  }
  return fallback;
}

// Module-level reference to the active Tauri Update resource.
// Invariant: status === 'downloaded' ⟹ the current resource completed download().
let activeUpdateResource: any = null;
let downloadedFlag = false;
// Set when dismiss() is called mid-download so the in-flight promise
// completion does not resurrect the banner.
let downloadCancelled = false;

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
    // Never orphan a completed download or race an in-flight one.
    // A downloaded resource must survive re-checks (launch timer, 4h
    // interval, focus/online, tray, dashboard nonce). Manual checks are
    // the only exception, and even then they must not replace a ready
    // resource for the same version (handled below).
    if (
      status === "checking" ||
      status === "downloading" ||
      status === "installing" ||
      status === "downloaded"
    ) {
      if (!(manual && status === "downloaded")) {
        return;
      }
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

      let currentVersion = "1.0.0";
      try {
        const { getVersion } = await import("@tauri-apps/api/app");
        currentVersion = await getVersion();
      } catch {
        /* fallback */
      }

      if (update && update.available) {
        // Manual re-check while a download already completed: keep the
        // ready resource when it targets the same version; only replace
        // when versions differ (resetting downloadedFlag).
        const existingInfo = get().updateInfo;
        if (
          manual &&
          downloadedFlag &&
          activeUpdateResource &&
          existingInfo &&
          existingInfo.version &&
          update.version === existingInfo.version
        ) {
          // Restore the downloaded status clobbered by the "checking" set above.
          set({ status: "downloaded", errorMessage: null });
          return;
        }
        activeUpdateResource = update;
        downloadedFlag = false;
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
        // Preserve a completed download across re-checks: a manual check
        // that reports "no update" must not orphan the ready resource.
        if (downloadedFlag && activeUpdateResource && get().updateInfo) {
          set({ status: "downloaded", errorMessage: null });
          return;
        }
        activeUpdateResource = null;
        downloadedFlag = false;
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
      // A failed re-check must not orphan a completed download.
      if (downloadedFlag && activeUpdateResource && get().updateInfo) {
        set({ status: "downloaded", errorMessage: null });
        return;
      }
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
    // Re-entrancy guard: banner + settings modal can invoke concurrently
    // on the same resource object.
    if (get().status === "downloading") return;
    if (!activeUpdateResource || !isTauri) return;

    // New download attempt supersedes any prior dismiss-during-download.
    downloadCancelled = false;
    const resource = activeUpdateResource;

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

      await resource.download((event: any) => {
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

      // Dismissed mid-download: do not resurrect the banner.
      if (downloadCancelled || activeUpdateResource !== resource) {
        downloadCancelled = false;
        return;
      }
      downloadedFlag = true;
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
      if (downloadCancelled) {
        downloadCancelled = false;
        return;
      }
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

    if (!isTauri) return;

    // Enforce download-before-install invariant: never call install() on a
    // resource that hasn't completed download(). Kick off the download
    // instead so users never see the raw plugin ordering error.
    const ready =
      !!activeUpdateResource &&
      downloadedFlag &&
      (!get().updateInfo?.version ||
        !activeUpdateResource.version ||
        activeUpdateResource.version === get().updateInfo?.version);
    if (!ready) {
      set({ errorMessage: "Update files aren't ready yet — downloading now." });
      await get().downloadUpdate();
      // downloadUpdate clears errorMessage on start; restore friendly copy
      // if the update still isn't ready.
      if (!downloadedFlag) {
        set({
          errorMessage: "Update files aren't ready yet — downloading now.",
        });
      }
      return;
    }

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
    // Dismissing mid-download cancels the banner resurrection: the
    // in-flight download promise checks downloadCancelled on completion.
    if (status === "downloading") {
      downloadCancelled = true;
      set({
        status: "idle",
        updateInfo: null,
        downloadProgress: null,
        downloadedBytes: null,
        totalBytes: null,
        errorMessage: null,
      });
      return;
    }
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
