// ---------------------------------------------------------------------------
// Transfera v2 — Desktop Bridge (Tauri shell + browser fallback)
// Single import point for all native-shell capabilities. The Tauri shell
// (frontend/src-tauri/) is the only packaged runtime; under `vite dev` the
// browser fallbacks keep every page usable without native hooks.
// ---------------------------------------------------------------------------

import { API_BASE_URL } from "@/lib/api-client";

export const isTauri: boolean =
  typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export type ShellKind = "tauri" | "browser";

export function shellKind(): ShellKind {
  return isTauri ? "tauri" : "browser";
}

/** True when native shell hooks exist (window controls, dialogs, tray). */
export const isDesktop: boolean =
  typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

// -- Window controls --------------------------------------------------------

export async function minimizeWindow(): Promise<void> {
  if (!isTauri) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  await getCurrentWindow().minimize();
}

export async function maximizeWindow(): Promise<void> {
  if (!isTauri) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  const win = getCurrentWindow();
  if (await win.isMaximized()) await win.unmaximize();
  else await win.maximize();
}

export async function closeWindow(): Promise<void> {
  if (!isTauri) return;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  await getCurrentWindow().close();
}

export async function isWindowMaximized(): Promise<boolean> {
  if (!isTauri) return false;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  return getCurrentWindow().isMaximized();
}

export async function isWindowFocused(): Promise<boolean> {
  if (!isTauri) return true;
  const { getCurrentWindow } = await import("@tauri-apps/api/window");
  return getCurrentWindow().isFocused();
}

// -- Filesystem dialogs -----------------------------------------------------

export async function openDirectory(
  defaultPath?: string,
): Promise<string | null> {
  if (!isTauri) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selected = await open({
    directory: true,
    multiple: false,
    defaultPath,
  });
  return typeof selected === "string" ? selected : null;
}

// -- Shell (open paths / URLs) ----------------------------------------------

export async function showItemInFolder(fullPath: string): Promise<void> {
  if (!isTauri) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("show_item_in_folder", { fullPath });
}

export async function openPath(fullPath: string): Promise<void> {
  if (!isTauri) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("open_path", { fullPath });
}

export async function openExternal(url: string): Promise<void> {
  if (!url.startsWith("https:"))
    throw new Error("URL not allowed (https only)");
  if (isTauri) {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(url);
    return;
  }
  window.open(url, "_blank", "noopener");
}

/** Apple-driver Microsoft Store page (winget fallback in DeviceSetup). */
export async function openDriverStorePage(): Promise<{ opened: boolean }> {
  const storeUri = "ms-windows-store://pdp/?productid=9NMPJ99VJBWV";
  const storeWebUrl =
    "https://apps.microsoft.com/detail/apple-devices/9NMPJ99VJBWV";
  if (isTauri) {
    try {
      const { open } = await import("@tauri-apps/plugin-shell");
      await open(storeUri);
      return { opened: true };
    } catch {
      try {
        const { open } = await import("@tauri-apps/plugin-shell");
        await open(storeWebUrl);
        return { opened: true };
      } catch {
        return { opened: false };
      }
    }
  }
  try {
    window.open(storeWebUrl, "_blank", "noopener");
    return { opened: true };
  } catch {
    return { opened: false };
  }
}

// -- Backend status / lifecycle events --------------------------------------
// The Rust shell emits backend:down/starting/ready; in the browser the
// health endpoint is probed directly (same contract).

export async function getBackendStatus(): Promise<{
  running: boolean;
  starting: boolean;
  port: number;
}> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/health`);
    if (res.ok) return { running: true, starting: false, port: 47821 };
  } catch {
    // not running
  }
  return { running: false, starting: false, port: 47821 };
}

function onTauriEvent(event: string, cb: () => void): () => void {
  if (!isTauri) return () => {};
  let unlisten: (() => void) | null = null;
  (async () => {
    const { listen } = await import("@tauri-apps/api/event");
    unlisten = await listen(event, cb);
  })();
  return () => unlisten?.();
}

export function onBackendDown(cb: () => void): () => void {
  return onTauriEvent("backend:down", cb);
}

export function onBackendStarting(cb: () => void): () => void {
  return onTauriEvent("backend:starting", cb);
}

export function onBackendReady(cb: () => void): () => void {
  return onTauriEvent("backend:ready", cb);
}

export function onNewRemovableDrive(
  cb: (data: { driveLetter: string; volumeName: string | null }) => void,
): () => void {
  if (!isTauri) return () => {};
  let unlisten: (() => void) | null = null;
  (async () => {
    const { listen } = await import("@tauri-apps/api/event");
    unlisten = await listen<{
      driveLetter: string;
      volumeName: string | null;
    }>("device:new-removable-drive", (e) => cb(e.payload));
  })();
  return () => unlisten?.();
}

// -- Native notifications ---------------------------------------------------

export async function showNotification(opts: {
  title: string;
  body: string;
  sessionId: number;
}): Promise<boolean> {
  void opts.sessionId;
  if (isTauri) {
    const { sendNotification } =
      await import("@tauri-apps/plugin-notification");
    sendNotification({ title: opts.title, body: opts.body });
    return true;
  }
  try {
    if ("Notification" in window) {
      if (Notification.permission === "granted") {
        new Notification(opts.title, { body: opts.body });
        return true;
      }
      if (Notification.permission !== "denied") {
        const perm = await Notification.requestPermission();
        if (perm === "granted") {
          new Notification(opts.title, { body: opts.body });
          return true;
        }
      }
    }
  } catch {
    // best-effort only
  }
  return false;
}

// NOTE (Tauri gap): a notification click focuses the app via the OS but
// does not route to the session report — the Rust shell has no notification
// activation hook. The report stays one click away on the Dashboard.
// Callers keep the same signature so the routing can be restored later.
export function onNotificationClick(
  _cb: (sessionId: number) => void,
): () => void {
  return () => {};
}

// -- Elevated commands (driver / Tier-2 setup) ------------------------------
// Same allowlist contract as the old shell: winget/wsl/usbipd/sc only,
// enforced again in Rust (defence in depth).

export async function runElevated(opts: {
  executable: string;
  args: string[];
  description?: string;
}): Promise<{ success: boolean; exitCode: number | null; error?: string }> {
  void opts.description;
  if (!isTauri) {
    return {
      success: false,
      exitCode: null,
      error: "Not available in browser",
    };
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke("run_elevated", {
    executable: opts.executable,
    args: opts.args,
  });
}

export async function checkVirtualization(): Promise<{
  available: boolean;
  details: string;
}> {
  if (!isTauri) {
    return { available: false, details: "Not available in browser" };
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke("check_virtualization");
}

// -- App control ------------------------------------------------------------

export async function restartApp(): Promise<void> {
  if (isTauri) {
    const { relaunch } = await import("@tauri-apps/plugin-process");
    await relaunch();
    return;
  }
  window.location.reload();
}

// -- Taskbar progress --------------------------------------------------------

export async function setTrayProgress(
  value: number | null,
  status?: "normal" | "paused" | "error",
): Promise<void> {
  if (!isTauri) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("set_progress", { value, status });
}

// -- App exit / close guard -------------------------------------------------

export async function forceExit(): Promise<void> {
  if (isTauri) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("force_exit");
    return;
  }
  window.close();
}

export function onRequestClose(cb: () => void): () => void {
  return onTauriEvent("app:request-close", cb);
}
