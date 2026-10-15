import { useEffect, useRef } from "react";
import { useIOSDevices } from "@/lib/queries";
import { useTransferStore } from "@/store/transfer";
import { getDeviceMeta, canonicalSerial } from "@/lib/device-utils";
import { isDesktop, onNewRemovableDrive } from "@/lib/desktop";
import type { IOSDeviceInfo } from "@/types/api";

/**
 * Global Device Watcher Hook
 * Dynamically tracks device connections and disconnections across the entire app
 * (iPhones, iPads, Android smartphones, Android tablets, cameras, and removable storage).
 *
 * Automatically and seamlessly updates the transfer source when a new device is plugged in
 * or when switching from one device to another (e.g. unplugging an iPhone and plugging in an Android).
 *
 * NOTE (device URI convention): this watcher always writes `ios://<serial>/DCIM`,
 * even when the serving tier is WPD. The backend canonical form is `ios://`
 * (source_ref_to_legacy_string, device_preview abs_path, and wpd_backend all
 * assume `ios://SERIAL/PATH`, and legacy_string_to_source_ref only parses
 * `ios://` — a `wpd://` source would be misread as a local folder).
 * Prefixes are interchangeable for *lookup* (formatDevicePath/canonicalSerial
 * match regardless of prefix), but `ios://` is the single write convention.
 */

// Canonical identity for one serial (never null for non-empty input).
const canonKey = (s: string) => canonicalSerial(s) ?? s.toLowerCase();

// Serial portion of an ios:// or wpd:// device URI (prefix-agnostic).
const stripDevicePrefix = (p: string) =>
  p.replace(/^(ios|wpd|afc):\/\//i, "").split(/[/\\]/)[0] ?? "";
export function useDeviceWatcher() {
  const { data: iosDevices } = useIOSDevices(true);
  const prevDevicesRef = useRef<IOSDeviceInfo[] | null>(null);
  const initialMountRef = useRef(true);
  // Debounce: newly-appeared device awaiting a 2nd consecutive ready poll
  // before we auto-switch to it (absorbs single-poll enumeration flaps).
  const pendingNewRef = useRef<{ canon: string } | null>(null);
  // Debounce: consecutive polls the active source has been absent.
  const absentPollsRef = useRef(0);

  const setupSourcePath = useTransferStore((s) => s.ui.setupSourcePath);
  const setupDestPath = useTransferStore((s) => s.ui.setupDestPath);
  const setSetupSourcePath = useTransferStore((s) => s.setSetupSourcePath);
  const showNotification = useTransferStore((s) => s.showNotification);
  const transferStatus = useTransferStore((s) => s.transfer.status);

  // 1. Monitor USB / MTP devices (iPhone, iPad, Android, Cameras)
  useEffect(() => {
    if (!iosDevices?.devices) return;
    const currentDevices = iosDevices.devices;
    const isTransferRunning =
      transferStatus === "running" || transferStatus === "paused";

    // On initial mount, if a device is connected and no source has been configured yet,
    // auto-adopt the ready device as the source.
    if (initialMountRef.current) {
      initialMountRef.current = false;
      prevDevicesRef.current = currentDevices;

      if (!isTransferRunning) {
        if (!setupSourcePath) {
          const firstReady = currentDevices.find((d) => d.status === "ready");
          if (firstReady) {
            const meta = getDeviceMeta(firstReady);
            setSetupSourcePath(`ios://${firstReady.serial}/DCIM`);
            showNotification(
              "info",
              `Connected ${meta.displayName} — set as transfer source`,
            );
          }
        } else if (
          setupSourcePath.startsWith("ios://") ||
          setupSourcePath.startsWith("wpd://")
        ) {
          // Verify if the restored device is currently connected
          // (prefix-agnostic: a tier flip from ios:// to wpd:// or a UDID
          // vs WPD PnP re-enumeration still canonically matches).
          const canonActive = canonKey(stripDevicePrefix(setupSourcePath));
          const isPresent = currentDevices.some(
            (d) => canonKey(d.serial) === canonActive && d.status === "ready",
          );
          if (!isPresent) {
            const firstReady = currentDevices.find((d) => d.status === "ready");
            if (firstReady) {
              const meta = getDeviceMeta(firstReady);
              setSetupSourcePath(`ios://${firstReady.serial}/DCIM`);
              showNotification(
                "info",
                `Connected ${meta.displayName} — set as transfer source`,
              );
            }
          }
        }
      }
      return;
    }

    const prevDevices = prevDevicesRef.current || [];
    prevDevicesRef.current = currentDevices;

    // Never auto-switch mid-transfer: a poll race must not rip the source
    // out from under a running/paused backup. Drop any armed debounce so
    // nothing fires surprise-switches after the transfer ends.
    if (isTransferRunning) {
      pendingNewRef.current = null;
      absentPollsRef.current = 0;
      return;
    }

    const prevSerials = new Set(prevDevices.map((d) => canonKey(d.serial)));
    const currentSerials = new Set(
      currentDevices.map((d) => canonKey(d.serial)),
    );

    // Detect newly plugged in devices
    const newlyConnected = currentDevices.filter(
      (d) => !prevSerials.has(canonKey(d.serial)) && d.status === "ready",
    );

    // Active device identity (prefix-agnostic: ios:// vs wpd:// never
    // distinguishes devices — canonicalSerial matches regardless).
    const activeCanon =
      setupSourcePath &&
      (setupSourcePath.startsWith("ios://") ||
        setupSourcePath.startsWith("wpd://"))
        ? canonKey(stripDevicePrefix(setupSourcePath))
        : null;

    // 1. Resolve a pending candidate armed on the previous poll: still
    // present + ready means stable for >= 2 consecutive polls → switch.
    const pending = pendingNewRef.current;
    if (pending) {
      const stable = currentDevices.find(
        (d) => canonKey(d.serial) === pending.canon && d.status === "ready",
      );
      pendingNewRef.current = null;
      if (stable) {
        // Same physical device re-enumerated under another tier serial
        // (UDID vs WPD PnP ID)? Keep the existing path untouched — a tier
        // prefix change alone is never a reason to rewrite the source.
        if (activeCanon !== pending.canon) {
          const meta = getDeviceMeta(stable);
          setSetupSourcePath(`ios://${stable.serial}/DCIM`);
          showNotification(
            "success",
            `Connected ${meta.displayName} — switched transfer source`,
          );
        }
        return;
      }
      // Vanished before stabilizing — drop it and fall through to the
      // disconnect check below with fresh state.
    }

    // 2. Arm the debounce for freshly appeared devices: the actual switch
    // happens on the next poll once the device proves stable.
    if (newlyConnected.length > 0) {
      const newDev = newlyConnected[0]!;
      // Already our active device under a new tier serial? Ignore — the
      // source still resolves to the same phone.
      if (activeCanon === canonKey(newDev.serial)) return;
      pendingNewRef.current = { canon: canonKey(newDev.serial) };
      return;
    }

    // 3. Handle the active device being unplugged. Require 2 consecutive
    // absent polls so one transient miss (already damped by the query
    // hysteresis in useIOSDevices) can't strand the source.
    if (setupSourcePath && activeCanon) {
      if (currentSerials.has(activeCanon)) {
        absentPollsRef.current = 0;
      } else {
        absentPollsRef.current += 1;
        if (absentPollsRef.current >= 2) {
          absentPollsRef.current = 0;
          const remainingReady = currentDevices.filter(
            (d) => d.status === "ready",
          );
          if (remainingReady.length > 0) {
            // Switch to remaining ready device
            const nextDev = remainingReady[0]!;
            const meta = getDeviceMeta(nextDev);
            setSetupSourcePath(`ios://${nextDev.serial}/DCIM`);
            showNotification(
              "info",
              `Device disconnected — switched source to ${meta.displayName}`,
            );
          } else {
            showNotification(
              "warning",
              "Device disconnected — connect device to preview and transfer",
            );
          }
        }
      }
    } else {
      absentPollsRef.current = 0;
    }
  }, [
    iosDevices?.devices,
    setupSourcePath,
    setSetupSourcePath,
    showNotification,
    transferStatus,
  ]);

  // 2. Monitor removable storage (SD cards, USB drives) via Tauri drive watcher
  useEffect(() => {
    if (!isDesktop) return;

    const unsub = onNewRemovableDrive((data) => {
      const isTransferRunning =
        transferStatus === "running" || transferStatus === "paused";
      if (isTransferRunning) return;

      const driveLetter = data.driveLetter?.trim();
      if (!driveLetter) return;

      // Don't auto-switch if the removable drive was already our destination
      if (
        setupDestPath &&
        setupDestPath.toLowerCase().startsWith(driveLetter.toLowerCase())
      ) {
        return;
      }

      // If no source is set or source is currently empty, auto-set to the new removable drive
      if (!setupSourcePath) {
        const drivePath = driveLetter.endsWith("\\")
          ? driveLetter
          : `${driveLetter}\\`;
        setSetupSourcePath(drivePath);
        showNotification(
          "success",
          `Connected ${data.volumeName || "Removable Drive"} (${driveLetter}) — set as transfer source`,
        );
      }
    });

    return () => {
      unsub();
    };
  }, [
    setupSourcePath,
    setupDestPath,
    setSetupSourcePath,
    showNotification,
    transferStatus,
  ]);
}
