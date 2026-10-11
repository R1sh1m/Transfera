// ---------------------------------------------------------------------------
// Transfera v2 — Updater Lifecycle Hook
// Coordinates periodic background update polling, window-focus rechecks,
// network reconnect triggers, and system-tray check events.
// ---------------------------------------------------------------------------

import { useEffect } from "react";
import { useUpdaterStore } from "@/store/updater";
import { useTransferStore } from "@/store/transfer";
import { onCheckUpdates } from "@/lib/desktop";

const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // Check every 4 hours in the background
const FOCUS_MIN_ELAPSED_MS = 2 * 60 * 60 * 1000; // At least 2 hours between focus re-checks

export function useUpdaterLifecycle() {
  const autoCheckEnabled = useUpdaterStore((s) => s.autoCheckEnabled);
  const lastCheckedAt = useUpdaterStore((s) => s.lastCheckedAt);
  const checkForUpdates = useUpdaterStore((s) => s.checkForUpdates);
  const updateCheckNonce = useTransferStore((s) => s.ui.updateCheckNonce);

  // 1. Initial delayed check on app launch (5s calm period so boot is responsive)
  useEffect(() => {
    if (!autoCheckEnabled) return;
    const timer = setTimeout(() => {
      checkForUpdates(false);
    }, 5000);
    return () => clearTimeout(timer);
  }, [autoCheckEnabled, checkForUpdates]);

  // 2. Periodic background check every 4 hours while app stays running
  useEffect(() => {
    if (!autoCheckEnabled) return;
    const interval = setInterval(() => {
      checkForUpdates(false);
    }, CHECK_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [autoCheckEnabled, checkForUpdates]);

  // 3. Focus and online listener: check when app is restored after being idle
  useEffect(() => {
    if (!autoCheckEnabled) return;

    const handleFocusOrOnline = () => {
      const now = Date.now();
      const last = lastCheckedAt || 0;
      if (now - last >= FOCUS_MIN_ELAPSED_MS) {
        checkForUpdates(false);
      }
    };

    window.addEventListener("focus", handleFocusOrOnline);
    window.addEventListener("online", handleFocusOrOnline);

    return () => {
      window.removeEventListener("focus", handleFocusOrOnline);
      window.removeEventListener("online", handleFocusOrOnline);
    };
  }, [autoCheckEnabled, lastCheckedAt, checkForUpdates]);

  // 4. System tray event: user clicked "Check for Updates..." in Windows taskbar tray
  useEffect(() => {
    const unlisten = onCheckUpdates(() => {
      checkForUpdates(true);
    });
    return () => {
      unlisten();
    };
  }, [checkForUpdates]);

  // 5. In-app manual triggers (e.g. Dashboard Refresh button bumping updateCheckNonce)
  useEffect(() => {
    if (updateCheckNonce > 0) {
      checkForUpdates(true);
    }
  }, [updateCheckNonce, checkForUpdates]);
}
