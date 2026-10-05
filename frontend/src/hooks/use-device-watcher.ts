import { useEffect, useRef } from "react";
import { useIOSDevices } from "@/lib/queries";
import { useTransferStore } from "@/store/transfer";
import { getDeviceMeta } from "@/lib/device-utils";
import { isDesktop, onNewRemovableDrive } from "@/lib/desktop";
import type { IOSDeviceInfo } from "@/types/api";

/**
 * Global Device Watcher Hook
 * Dynamically tracks device connections and disconnections across the entire app
 * (iPhones, iPads, Android smartphones, Android tablets, cameras, and removable storage).
 *
 * Automatically and seamlessly updates the transfer source when a new device is plugged in
 * or when switching from one device to another (e.g. unplugging an iPhone and plugging in an Android).
 */
export function useDeviceWatcher() {
  const { data: iosDevices } = useIOSDevices(true);
  const prevDevicesRef = useRef<IOSDeviceInfo[] | null>(null);
  const initialMountRef = useRef(true);

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

      if (!setupSourcePath && !isTransferRunning) {
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
      return;
    }

    const prevDevices = prevDevicesRef.current || [];
    prevDevicesRef.current = currentDevices;

    const prevSerials = new Set(prevDevices.map((d) => d.serial.toLowerCase()));
    const currentSerials = new Set(
      currentDevices.map((d) => d.serial.toLowerCase()),
    );

    // Detect newly plugged in devices
    const newlyConnected = currentDevices.filter(
      (d) => !prevSerials.has(d.serial.toLowerCase()) && d.status === "ready",
    );

    // Detect disconnected devices
    const disconnected = prevDevices.filter(
      (d) => !currentSerials.has(d.serial.toLowerCase()),
    );

    // Handle NEW device plugged in -> automatically switch
    if (newlyConnected.length > 0 && !isTransferRunning) {
      const newDev = newlyConnected[0]!;
      const meta = getDeviceMeta(newDev);
      setSetupSourcePath(`ios://${newDev.serial}/DCIM`);
      showNotification(
        "success",
        `Connected ${meta.displayName} — switched transfer source`,
      );
      return;
    }

    // Handle currently active device being unplugged
    if (setupSourcePath && (setupSourcePath.startsWith("ios://") || setupSourcePath.startsWith("wpd://"))) {
      const activeSerial = setupSourcePath
        .replace(/^ios:\/\//, "")
        .replace(/^wpd:\/\//, "")
        .split("/")[0]!
        .toLowerCase();

      const wasActiveDisconnected = disconnected.some(
        (d) => d.serial.toLowerCase() === activeSerial,
      );

      if (wasActiveDisconnected && !isTransferRunning) {
        const remainingReady = currentDevices.filter((d) => d.status === "ready");
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
          // Reset source cleanly
          setSetupSourcePath("");
          showNotification(
            "warning",
            "Device disconnected — source directory reset",
          );
        }
      }
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
        const drivePath = driveLetter.endsWith("\\") ? driveLetter : `${driveLetter}\\`;
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
  }, [setupSourcePath, setupDestPath, setSetupSourcePath, showNotification, transferStatus]);
}
