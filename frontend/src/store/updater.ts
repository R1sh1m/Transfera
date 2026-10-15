// ---------------------------------------------------------------------------
// Transfera v2 — App Updater Store (Zustand)
// Coordinates background update checks, seamless downloads, installation,
// user preferences, and release changelog presentation.
// ---------------------------------------------------------------------------

import { create } from "zustand";
import { isTauri, restartApp, showNotification } from "@/lib/desktop";
import { updaterErrorMessage } from "./updaterErrors";
import { APP_VERSION } from "@/version";

export { updaterErrorMessage };

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
  currentVersion: string;
  updateInfo: UpdateInfo | null;
  downloadProgress: number | null;
  downloadedBytes: number | null;
  totalBytes: number | null;
  errorMessage: string | null;
  lastCheckedAt: number | null;
  autoCheckEnabled: boolean;
  autoDownloadEnabled: boolean;
  isSettingsOpen: boolean;
  isBannerHidden: boolean;

  // Actions
  checkForUpdates: (manual?: boolean) => Promise<void>;
  downloadUpdate: () => Promise<void>;
  cancelDownload: () => Promise<void>;
  installAndRestart: () => Promise<void>;
  dismiss: () => void;
  hideBanner: () => void;
  unhideBanner: () => void;
  resolveReleaseNotes: () => Promise<string | null>;
  setAutoCheckEnabled: (enabled: boolean) => void;
  setAutoDownloadEnabled: (enabled: boolean) => void;
  setIsSettingsOpen: (open: boolean) => void;
  setCurrentVersion: (version: string) => void;
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

/** Returns true if notes are empty, just the app name/version, or a generic placeholder. */
export function isPlaceholderNotes(
  body?: string | null,
  version?: string,
): boolean {
  if (!body) return true;
  const trimmed = body.trim();
  if (!trimmed) return true;
  if (/^Transfera\s*(v?\d+(\.\d+)*)?\s*$/i.test(trimmed)) return true;
  if (/^Release\s*(v?\d+(\.\d+)*)?\s*$/i.test(trimmed)) return true;
  if (/^v?\d+(\.\d+)*\s*$/i.test(trimmed)) return true;
  if (
    /^Transfera\s*(v?\d+(\.\d+)*)?\s*-\s*see the github release/i.test(trimmed)
  ) {
    return true;
  }
  if (
    /^\*\*Full Changelog\*\*:\s*https:\/\/github\.com\/[^\s]+$/i.test(trimmed)
  ) {
    return true;
  }
  if (
    version &&
    (trimmed.toLowerCase() === `transfera ${version.toLowerCase()}` ||
      trimmed.toLowerCase() === `transfera v${version.toLowerCase()}` ||
      trimmed.toLowerCase() === `v${version.toLowerCase()}` ||
      trimmed.toLowerCase() === version.toLowerCase())
  ) {
    return true;
  }
  return false;
}

/** Fetches real release notes from GitHub commit / release when latest.json has a placeholder. */
export async function fetchReleaseNotes(
  version: string,
): Promise<string | null> {
  const cleanVer = version.replace(/^v/, "");
  const tag = `v${cleanVer}`;

  // 1. Try GitHub Releases API for tag
  try {
    const res = await fetch(
      `https://api.github.com/repos/R1sh1m/Transfera/releases/tags/${tag}`,
      {
        headers: { Accept: "application/vnd.github.v3+json" },
      },
    );
    if (res.ok) {
      const data = await res.json();
      const rawBody = typeof data.body === "string" ? data.body.trim() : "";
      if (rawBody && !isPlaceholderNotes(rawBody, cleanVer)) {
        const cleaned = rawBody
          .replace(
            /\n*\*\*Full Changelog\*\*:\s*https:\/\/github\.com\/[^\s]+/gi,
            "",
          )
          .trim();
        if (cleaned && !isPlaceholderNotes(cleaned, cleanVer)) {
          return cleaned;
        }
      }
    }
  } catch (err) {
    console.debug("[updater] GitHub release tag fetch skipped:", err);
  }

  // 2. Try commit message of the release tag
  try {
    const res = await fetch(
      `https://api.github.com/repos/R1sh1m/Transfera/commits/${tag}`,
      {
        headers: { Accept: "application/vnd.github.v3+json" },
      },
    );
    if (res.ok) {
      const data = await res.json();
      const message =
        typeof data.commit?.message === "string"
          ? data.commit.message.trim()
          : "";
      if (message) {
        // Strip release commit header (e.g. "chore: release version 1.1.1")
        const parts = message.split(/\r?\n\r?\n/);
        let candidate =
          parts.length > 1
            ? parts.slice(1).join("\n\n").trim()
            : parts[0].trim();
        candidate = candidate
          .replace(/^chore(\([^)]+\))?:\s*release[^\n]*\n*/i, "")
          .trim();
        if (candidate && !isPlaceholderNotes(candidate, cleanVer)) {
          return candidate;
        }
      }
    }
  } catch (err) {
    console.debug("[updater] GitHub commit message fetch skipped:", err);
  }

  return null;
}

// Module-level reference to the active Tauri Update resource.
// Invariant: status === 'downloaded' ⟹ the current resource completed download().
let activeUpdateResource: any = null;
let downloadedFlag = false;
// Set when dismiss() is called mid-download so the in-flight promise
// completion does not resurrect the banner.
let downloadCancelled = false;
let downloadInProgress = false;

export const useUpdaterStore = create<UpdaterState>((set, get) => ({
  status: "idle",
  currentVersion: APP_VERSION,
  updateInfo: null,
  downloadProgress: null,
  downloadedBytes: null,
  totalBytes: null,
  errorMessage: null,
  lastCheckedAt: getInitialNum(STORAGE_KEY_LAST_CHECKED),
  autoCheckEnabled: getInitialBool(STORAGE_KEY_AUTO_CHECK, true),
  autoDownloadEnabled: getInitialBool(STORAGE_KEY_AUTO_DOWNLOAD, true),
  isSettingsOpen: false,
  isBannerHidden: false,

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

      let currentVersion = get().currentVersion || APP_VERSION;
      try {
        const { getVersion } = await import("@tauri-apps/api/app");
        currentVersion = await getVersion();
      } catch {
        /* fallback to store/package version */
      }
      set({ currentVersion });

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

        let body = update.body;
        if (isPlaceholderNotes(body, update.version)) {
          try {
            const fetched = await fetchReleaseNotes(update.version);
            if (fetched) {
              body = fetched;
            }
          } catch {
            /* ignore */
          }
        }

        const info: UpdateInfo = {
          version: update.version,
          currentVersion,
          body,
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
        set({ updateInfo: null, status: "up-to-date", currentVersion });

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
        // Single surface for check failures: the AppUpdateNotification error
        // card (title + settings shortcut + dismiss) renders from this
        // status. Do NOT also push a store toast here — same text in both the
        // banner (bottom-6 right-6) and the toast stack (bottom-4 right-4)
        // renders as two overlapping cards.
        set({ status: "error", errorMessage: msg });
      } else {
        set({ status: "idle" });
      }
    }
  },

  downloadUpdate: async () => {
    // Re-entrancy guard: strictly prevent multiple concurrent download streams
    if (downloadInProgress || get().status === "downloading") {
      console.debug("[updater] Download already running — skipping duplicate request");
      return;
    }
    if (!activeUpdateResource || !isTauri) return;

    downloadInProgress = true;
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
      let lastReportedPct = 0;
      let lastDispatchTime = 0;

      await resource.download((event: any) => {
        if (!event || downloadCancelled || activeUpdateResource !== resource) return;

        if (event.event === "Started") {
          total = event.data?.contentLength || 0;
          set({ totalBytes: total });
        } else if (event.event === "Progress") {
          downloaded += event.data?.chunkLength || 0;
          if (total > 0) {
            const rawPct = Math.min(99, Math.round((downloaded / total) * 100));
            // Guarantee monotonic non-decreasing percentage so it never jumps backwards
            const pct = Math.max(lastReportedPct, rawPct);
            const now = performance.now();

            // Throttle UI updates to at most once per 80ms or on >=1% change
            // and combine downloadedBytes and downloadProgress into a single set()
            if (pct > lastReportedPct && (now - lastDispatchTime >= 80 || pct - lastReportedPct >= 1)) {
              lastReportedPct = pct;
              lastDispatchTime = now;
              set({
                downloadedBytes: downloaded,
                downloadProgress: pct,
              });
            }
          } else {
            const now = performance.now();
            if (now - lastDispatchTime >= 150) {
              lastDispatchTime = now;
              set({ downloadedBytes: downloaded });
            }
          }
        } else if (event.event === "Finished") {
          set({
            downloadProgress: 100,
            downloadedBytes: total > 0 ? total : downloaded,
          });
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
    } finally {
      downloadInProgress = false;
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

  cancelDownload: async () => {
    downloadCancelled = true;
    downloadInProgress = false;
    set({
      status: "idle",
      downloadProgress: null,
      downloadedBytes: null,
      totalBytes: null,
      errorMessage: null,
      isBannerHidden: false,
    });
    try {
      const { useTransferStore } = await import("@/store/transfer");
      useTransferStore
        .getState()
        .showNotification("info", "Update download cancelled.");
    } catch {
      /* ignore */
    }
  },

  hideBanner: () => {
    set({ isBannerHidden: true });
  },

  unhideBanner: () => {
    set({ isBannerHidden: false });
  },

  setCurrentVersion: (version: string) => {
    if (version && version !== get().currentVersion) {
      set({ currentVersion: version });
    }
  },

  dismiss: () => {
    const { status } = get();
    // If downloading, hide the banner onto the top-right icon while
    // the engine continues downloading in background. Use cancelDownload() to abort.
    if (status === "downloading") {
      set({ isBannerHidden: true });
      return;
    }
    // If downloaded, keep updateInfo so UI pills and settings know it's ready,
    // but hide the banner
    if (status === "downloaded") {
      set({ status: "idle", isBannerHidden: true });
    } else {
      set({
        status: "idle",
        updateInfo: null,
        downloadProgress: null,
        errorMessage: null,
        isBannerHidden: false,
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

  resolveReleaseNotes: async () => {
    const info = get().updateInfo;
    if (!info || !info.version) return null;
    if (info.body && !isPlaceholderNotes(info.body, info.version)) {
      return info.body;
    }
    const fetched = await fetchReleaseNotes(info.version);
    if (fetched) {
      set({ updateInfo: { ...info, body: fetched } });
      return fetched;
    }
    return info.body || null;
  },

  setIsSettingsOpen: (open: boolean) => {
    set({ isSettingsOpen: open });
  },
}));
