// ---------------------------------------------------------------------------
// Transfera v2 — shared keep-awake throttle
// Single source of truth for the "Keep your iPhone unlocked" popup throttle.
// The module-level Set survives remounts/page revisits within the same JS
// runtime and resets on app close/reload (fresh runtime = empty Set).
// Both DeviceSetupPage and TransferPage import from here — do NOT create
// per-page duplicate Sets or the popup fires twice (once per page).
// ---------------------------------------------------------------------------

/** Once-per-app-launch throttle: device serials that already saw the popup. */
export const keepAwakeNotifiedSerials = new Set<string>();

// Stable device identity across Tier 1 / WPD serial formats. Mirrors
// backend canonical_device_serial: a WPD PnP id often embeds the 40-hex
// UDID — extracting it keeps import-state lookups and tier matching stable
// when the serving tier flaps. (Backend canonicalizes too; this keeps the
// query key stable on the client.)
export function canonicalSerial(
  serial: string | null | undefined,
): string | null {
  if (!serial) return null;
  const m = serial.match(/[0-9a-fA-F]{40}/);
  if (m?.[0]) return m[0].toLowerCase();
  return serial;
}

// Canonical device serial from an ios://<serial>/… source root (mirrors
// backend canonical_device_serial: embedded 40-hex UDID wins, else
// lowercased raw id). Used for the once-per-app-launch popup throttle.
export function canonicalSerialFromSourceRoot(
  sourceRoot: string | null | undefined,
): string | null {
  if (!sourceRoot?.startsWith("ios://")) return null;
  const raw = sourceRoot.slice("ios://".length);
  const slashIdx = raw.indexOf("/");
  const devId = (slashIdx === -1 ? raw : raw.slice(0, slashIdx)).trim();
  if (!devId) return null;
  const m = devId.match(/[0-9a-fA-F]{40}/);
  return (m?.[0] ?? devId).toLowerCase();
}
