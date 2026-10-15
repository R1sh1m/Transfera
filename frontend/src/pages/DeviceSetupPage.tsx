// ---------------------------------------------------------------------------
// Transfera v2 — Device Setup Page
// Two distinct source entry points: "Browse a folder on this PC" and
// "Connected devices" — mirroring Explorer's model. Device sources use
// a virtual folder browser backed by the device API, not the native OS dialog.
// ---------------------------------------------------------------------------

import { useState, useCallback, useEffect, useRef, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  FolderOpen,
  FolderCheck,
  ChevronRight,
  Settings,
  HardDrive,
  FileImage,
  Film,
  Music,
  FileText,
  Shield,
  ShieldCheck,
  AlertTriangle,
  ArrowRightLeft,
  Copy,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertCircle,
  Smartphone,
  Tablet,
  Camera,
  Clock,
  RefreshCw,
  RotateCcw,
  X,
  Lock,
  Terminal,
} from "lucide-react";
import { getDeviceMeta } from "@/lib/device-utils";
import { canonicalSerial, keepAwakeNotifiedSerials } from "@/lib/keep-awake";
import { useQueryClient } from "@tanstack/react-query";
import {
  useConfig,
  useCreateSession,
  usePreflightValidate,
  useValidatePath,
  useIOSDevices,
  useDeviceImportState,
  useClearDeviceImportState,
  useTier2Status,
  useTier2SetupPreview,
  useTier2ExecuteStep,
  useTier2Cancel,
  useTier2Reset,
  useDeviceBackendStatus,
  useInstallDriver,
  useRecoverIOSDevice,
  useSetPreferredTier,
  useRetryDevice,
} from "@/lib/queries";
import { useTransferStore } from "@/store/transfer";
import { cn, extractErrorMessage, parseBackendDate } from "@/lib/utils";
import {
  isDesktop,
  openDirectory,
  openExternal,
  onNewRemovableDrive,
  restartApp,
  runElevated,
} from "@/lib/desktop";
import type {
  TransferMode,
  IOSDeviceInfo,
  SourceRef,
  Tier2StepPreview,
  DeviceTierChoice,
  AvailableTierInfo,
} from "@/types/api";
import { IOS_SOURCE_PREFIX, sourceRefToString } from "@/types/api";
import { DeviceFolderBrowser } from "@/components/DeviceFolderBrowser";
import SourcePreviewPanel, {
  clearAllThumbFailed,
} from "@/components/SourcePreviewPanel";
import {
  useBackendReady,
  ENGINE_WAITING_LABEL,
} from "@/hooks/use-backend-ready";
import { PillSelect, type PillSelectOption } from "@/components/PillSelect";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

/** Debounce a fast-changing value (e.g. dest path keystrokes) so
 *  expensive queries (preflight walk) fire once the user pauses. */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}

// ---------------------------------------------------------------------------
// Per-device driver picker (TierSelect)
// ---------------------------------------------------------------------------
function tierLabel(tier?: string | null): string {
  switch (tier) {
    case "tier1":
      return "Apple driver";
    case "wpd":
      return "Windows USB";
    case "tier2":
      return "Linux bridge";
    case "auto":
      return "Auto";
    default:
      return tier ?? "Unknown";
  }
}

const TIER_OPTION_DESCRIPTIONS: Record<string, string> = {
  auto: "Automatic — try the preferred driver first, fall back to the rest",
  tier1: "Apple Mobile Device Support",
  wpd: "Windows Portable Devices (photos only via DCIM)",
  tier2: "Open-source WSL bridge",
};

function TierSelect({
  value,
  serving,
  availableTiers,
  disabled,
  onChange,
  onOpenTier2Setup,
}: {
  value: DeviceTierChoice;
  serving?: string | null;
  availableTiers?: Record<string, AvailableTierInfo> | null;
  disabled?: boolean;
  onChange: (tier: DeviceTierChoice) => void;
  onOpenTier2Setup?: () => void;
}) {
  const optionState = (tier: string) => {
    const info = availableTiers?.[tier];
    if (!info || info.available) {
      return {
        disabled: false,
        title: TIER_OPTION_DESCRIPTIONS[tier] ?? tierLabel(tier),
      };
    }
    return {
      disabled: true,
      title: info.error || `${tierLabel(tier)} is unavailable`,
    };
  };

  const t1 = optionState("tier1");
  const tw = optionState("wpd");
  const t2 = optionState("tier2");

  const tierOptions: PillSelectOption[] = [
    {
      value: "auto",
      label: "Auto (recommended)",
      title: TIER_OPTION_DESCRIPTIONS.auto,
    },
    {
      value: "tier1",
      label: `Apple driver${t1.disabled ? " — unavailable" : ""}`,
      disabled: t1.disabled,
      title: t1.title,
    },
    {
      value: "wpd",
      label: `Windows USB${tw.disabled ? " — unavailable" : ""}`,
      disabled: tw.disabled,
      title: tw.title,
    },
    {
      value: "tier2",
      label: "Linux bridge",
      badge: t2.disabled ? "Set up" : undefined,
      disabled: false,
      title: t2.disabled
        ? "Linux bridge requires setup — click to view download & setup steps"
        : TIER_OPTION_DESCRIPTIONS.tier2,
    },
  ];

  const handleSelect = (val: DeviceTierChoice) => {
    if (val === "tier2" && t2.disabled) {
      onOpenTier2Setup?.();
      return;
    }
    onChange(val);
  };

  return (
    <div
      className="flex items-center gap-2 text-xs text-muted-foreground"
      onClick={(e) => e.stopPropagation()}
    >
      <span className="shrink-0 text-[11px] font-medium text-muted-foreground">
        Driver
      </span>
      <PillSelect
        value={value}
        disabled={disabled}
        size="sm"
        className="flex-1 min-w-0"
        triggerClassName="w-full justify-between"
        menuClassName="w-full min-w-[220px]"
        onChange={(val) => handleSelect(val as DeviceTierChoice)}
        options={tierOptions}
        title={
          serving
            ? `Now serving via ${tierLabel(serving)}. Your choice is tried first; the rest are always tried as fallback.`
            : "Your choice is tried first; the rest are always tried as fallback."
        }
      />
    </div>
  );
}

function TierMismatchNotice({
  preferred,
  serving,
  availableTiers,
  onOpenTier2Setup,
}: {
  preferred: DeviceTierChoice;
  serving?: string | null;
  availableTiers?: Record<string, AvailableTierInfo> | null;
  onOpenTier2Setup?: () => void;
}) {
  if (!preferred || preferred === "auto" || !serving || serving === preferred) {
    return null;
  }
  const err = availableTiers?.[preferred]?.error;
  const isTier2Unavailable =
    preferred === "tier2" &&
    availableTiers?.tier2 &&
    !availableTiers.tier2.available;

  return (
    <div className="flex items-center gap-1.5 text-[10px] text-amber-600 dark:text-amber-400 leading-snug">
      <span>
        Serving via {tierLabel(serving)}
        {err
          ? ` (${tierLabel(preferred)} failed: ${err})`
          : ` (${tierLabel(preferred)} unavailable)`}
      </span>
      {isTier2Unavailable && onOpenTier2Setup && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onOpenTier2Setup();
          }}
          className="text-action underline hover:text-action/80 font-medium ml-1 cursor-pointer"
        >
          Set up now
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// AppleBlindSpotGuide — proactive step-by-step guidance for the Tier-1
// blind spot (WPD sees the iPhone, the Apple driver does not).
// Rendered under the same gate as the old single-hint banner. Every step
// state derives from live signals already available to the frontend:
// driver_status, ready rows per active_tier, non-ready LOCKED/NOT_TRUSTED
// rows, and the recover mutation result. No new backend signals required.
// ---------------------------------------------------------------------------
type GuideStepState = "done" | "current" | "pending";

interface BlindSpotStep {
  id: string;
  label: string;
  hint?: string;
  state: GuideStepState;
}

function AppleBlindSpotGuide({
  driverStatus,
  wpdAppleNames,
  trustNames,
  hasTier1Ready,
  recoverOverall,
  tier1Found,
  elevationPending,
  recoverBusy,
  recoverCooldown,
  recoverPending,
  onRetry,
}: {
  driverStatus?: string;
  wpdAppleNames: string[];
  trustNames: string[];
  hasTier1Ready: boolean;
  recoverOverall?: string;
  tier1Found?: boolean;
  elevationPending: boolean;
  recoverBusy: boolean;
  recoverCooldown: boolean;
  recoverPending: boolean;
  onRetry: () => void;
}) {
  const driverMissing =
    driverStatus === "no_driver" || driverStatus === "no_pymobiledevice3";
  // Step 1: the Apple service answers. The gate already implies ready, and
  // a successful recover confirms it — either signal marks it done.
  const serviceRunning =
    driverStatus === "ready" ||
    recoverOverall === "service_restored" ||
    recoverOverall === "service_already_running";
  // Step 2: a ready Tier-1 row exists (or the last recover re-enumerated one).
  const tier1Visible = hasTier1Ready || tier1Found === true;
  const needsTrust = trustNames.length > 0;
  const phoneLabel =
    wpdAppleNames.length > 0 ? wpdAppleNames[0] : "Your iPhone";
  const trustLabel =
    trustNames.length === 1
      ? trustNames[0]
      : trustNames.length > 1
        ? `${trustNames[0]} (and ${trustNames.length - 1} more)`
        : phoneLabel;

  const steps: BlindSpotStep[] = driverMissing
    ? [
        {
          id: "install",
          label: "Install Apple device support",
          hint: "Needed before the Apple driver can see any iPhone.",
          state: "current",
        },
        {
          id: "visible",
          label: "iPhone visible to Apple driver",
          hint: "Unlock, replug, then retry below.",
          state: "pending",
        },
      ]
    : [
        {
          id: "service",
          label: "Apple service is running",
          state: serviceRunning ? "done" : "current",
        },
        {
          id: "visible",
          label: "iPhone visible to Apple driver",
          hint: tier1Visible
            ? "Found — you can pick it from the list."
            : "Not yet — work through the steps below.",
          state: tier1Visible ? "done" : "current",
        },
        {
          id: "trust",
          label: needsTrust
            ? `Unlock ${trustLabel} and tap Trust`
            : "iPhone unlocked",
          hint: needsTrust
            ? "Use the passcode, then accept Trust This Computer."
            : "Keep the screen on while you replug.",
          state: tier1Visible ? "done" : needsTrust ? "current" : "done",
        },
        {
          id: "replug",
          label: "Unplug, replug, wait a few seconds",
          hint: "Use a data-capable cable, not a charge-only one.",
          state: tier1Visible ? "done" : needsTrust ? "pending" : "current",
        },
      ];

  return (
    <div className="flex items-start gap-2.5 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-xl p-3.5">
      <AlertTriangle className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-xs font-semibold text-amber-700 dark:text-amber-300">
          {driverMissing
            ? "Apple device support is missing"
            : tier1Visible
              ? "iPhone found on the Apple driver"
              : `${phoneLabel} is on USB, but the Apple driver can't see it`}
        </p>
        {driverMissing ? (
          <div className="mt-2">
            <DriverInstallerInline />
          </div>
        ) : (
          <ol className="mt-2 space-y-1.5">
            {steps.map((step, idx) => (
              <li key={step.id} className="flex items-start gap-2">
                <span
                  className={cn(
                    "w-4 h-4 rounded-full flex items-center justify-center shrink-0 text-[9px] font-bold mt-px",
                    step.state === "done" &&
                      "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400",
                    step.state === "current" &&
                      "bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300",
                    step.state === "pending" &&
                      "bg-muted text-muted-foreground",
                  )}
                >
                  {step.state === "done" ? (
                    <CheckCircle2 className="w-3 h-3" />
                  ) : (
                    idx + 1
                  )}
                </span>
                <div className="min-w-0">
                  <p
                    className={cn(
                      "text-[11px] leading-snug",
                      step.state === "done"
                        ? "text-muted-foreground"
                        : "text-amber-700 dark:text-amber-300 font-semibold",
                    )}
                  >
                    {step.label}
                  </p>
                  {step.hint && (
                    <p className="text-[10px] text-amber-600 dark:text-amber-400 leading-snug">
                      {step.hint}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
        {!driverMissing && !tier1Visible && (
          <button
            type="button"
            onClick={onRetry}
            disabled={recoverBusy}
            title={
              recoverCooldown ? "Wait a few seconds before retrying" : undefined
            }
            className="mt-2 inline-flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white rounded-pill text-xs font-normal active:scale-[0.95] transition-all"
          >
            {recoverPending ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <RefreshCw className="w-3 h-3" />
            )}
            {recoverPending
              ? "Retrying…"
              : recoverCooldown
                ? "Wait before retrying…"
                : "Retry Apple driver"}
          </button>
        )}
        {elevationPending && (
          <p className="mt-2 flex items-start gap-1.5 text-[11px] text-amber-700 dark:text-amber-300 leading-snug">
            <Shield className="w-3 h-3 mt-px shrink-0" />
            <span>
              Still stuck? Restart the Apple service from the permission prompt
              below, then retry.
            </span>
          </p>
        )}
        {!driverMissing && (
          <details className="mt-1.5 text-[10px] text-amber-600 dark:text-amber-400">
            <summary className="cursor-pointer hover:text-amber-700 dark:hover:text-amber-300 transition-colors">
              Why am I seeing this?
            </summary>
            <p className="mt-1 leading-snug">
              Photos still work over the Windows USB fallback, but previews and
              transfers are much faster over the Apple driver. This sometimes
              clears on its own; retrying re-checks the driver without
              replugging.
            </p>
          </details>
        )}
      </div>
    </div>
  );
}

function formatDate(dateStr: string): string {
  try {
    return parseBackendDate(dateStr).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return dateStr;
  }
}

// NOTE: canonicalSerial + the keep-awake popup throttle live in
// @/lib/keep-awake (shared with TransferPage) — do NOT redeclare them here
// or the popup fires twice (once per page).

// ---------------------------------------------------------------------------
// SourcePicker — Two distinct entry points
// ---------------------------------------------------------------------------
type SourceMode = "none" | "folder" | "device";

interface SourcePickerProps {
  sourceRef: SourceRef | null;
  onSourceChange: (ref: SourceRef | null) => void;
  onRecovered?: (
    devices: NonNullable<
      import("@/types/api").IOSDeviceRecoverResponse["devices"]
    >,
  ) => void;
  deviceLocked?: boolean;
  isIOSDevice?: boolean;
  showIPhoneTips?: boolean;
  onToggleIPhoneTips?: () => void;
}

function SourcePicker({
  sourceRef,
  onSourceChange,
  onRecovered,
  deviceLocked = false,
  isIOSDevice = false,
  showIPhoneTips = false,
  onToggleIPhoneTips,
}: SourcePickerProps) {
  const [mode, setMode] = useState<SourceMode>(
    sourceRef?.type === "device"
      ? "device"
      : sourceRef?.type === "local_folder"
        ? "folder"
        : "none",
  );

  const [deviceListExpanded, setDeviceListExpanded] = useState(
    sourceRef?.type !== "local_folder",
  );
  const { data: iosDevices, isLoading: iosLoading } = useIOSDevices(
    sourceRef?.type !== "local_folder",
  );

  const selectedDeviceConnected = useMemo(() => {
    if (sourceRef?.type !== "device" || !iosDevices?.devices) return false;
    const targetSerial = canonicalSerial(sourceRef.device_id)?.toLowerCase();
    if (!targetSerial) return false;
    return iosDevices.devices.some(
      (d) =>
        canonicalSerial(d.serial)?.toLowerCase() === targetSerial &&
        d.status !== "disconnected",
    );
  }, [sourceRef, iosDevices]);
  const isDeviceDisconnected =
    sourceRef?.type === "device" && !iosLoading && !selectedDeviceConnected;

  // Auto-collapse device list when a local folder source is selected
  useEffect(() => {
    if (sourceRef?.type === "local_folder") setDeviceListExpanded(false);
  }, [sourceRef?.type]);

  // Sync mode when sourceRef changes externally (e.g. from drive-detection banner)
  useEffect(() => {
    if (sourceRef?.type === "device") setMode("device");
    else if (sourceRef?.type === "local_folder") setMode("folder");
    else setMode("none");
  }, [sourceRef]);

  const [initializingTimedOut, setInitializingTimedOut] = useState(false);
  const backendStatus = useDeviceBackendStatus();
  useEffect(() => {
    if (backendStatus.data?.initializing) {
      setInitializingTimedOut(false);
      const timer = setTimeout(() => setInitializingTimedOut(true), 30000);
      return () => clearTimeout(timer);
    }
  }, [backendStatus.data?.initializing]);

  const iosAvailable = !!backendStatus.data?.ios_available;

  const tier2Status = useTier2Status();
  const allDevices = iosDevices?.devices || [];
  const readyDevices = allDevices.filter((d) => d.status === "ready");
  const nonReadyDevices = allDevices.filter((d) => d.status !== "ready");
  const [tier2ModalOpen, setTier2ModalOpen] = useState(false);
  const [activeSetupSerial, setActiveSetupSerial] = useState<string | null>(
    null,
  );

  const recoverMutation = useRecoverIOSDevice();
  // 10 s cooldown after each recovery settles so users can't hammer the
  // Apple service / USB stack with rapid retries.
  const [recoverCooldown, setRecoverCooldown] = useState(false);
  const recoverSettled = recoverMutation.isSuccess || recoverMutation.isError;
  useEffect(() => {
    if (!recoverSettled) return;
    setRecoverCooldown(true);
    const t = setTimeout(() => setRecoverCooldown(false), 10000);
    return () => clearTimeout(t);
  }, [recoverSettled]);
  const recoverBusy = recoverMutation.isPending || recoverCooldown;
  const setPreferredTier = useSetPreferredTier();
  const retryDevice = useRetryDevice();
  const sourcePickerQueryClient = useQueryClient();

  // Per-device retry: 10 s cooldown scoped to each serial (mirrors the
  // global recoverCooldown pattern, but one device retrying never blocks
  // the others).
  const [retryingMap, setRetryingMap] = useState<Record<string, boolean>>({});
  const [retryCooldownMap, setRetryCooldownMap] = useState<
    Record<string, boolean>
  >({});
  const retryCooldownTimers = useRef<
    Record<string, ReturnType<typeof setTimeout>>
  >({});
  useEffect(
    () => () => {
      Object.values(retryCooldownTimers.current).forEach(clearTimeout);
    },
    [],
  );
  const handleRetryDevice = useCallback(
    async (serial: string) => {
      if (retryingMap[serial] || retryCooldownMap[serial]) return;
      setRetryingMap((prev) => ({ ...prev, [serial]: true }));
      try {
        const res = await retryDevice.mutateAsync({ serial });
        useTransferStore
          .getState()
          .showNotification(
            "success",
            `Device reconnected via ${tierLabel(res.serving_tier)}`,
          );
        // Backend escalation (service restart / re-enumeration) changes
        // what the preview + device list should show — force both to
        // refetch so recovery is visible. Same toast, same button labels.
        if (res.escalated === true) {
          sourcePickerQueryClient.invalidateQueries({
            queryKey: ["device-preview"],
          });
          sourcePickerQueryClient.invalidateQueries({
            queryKey: ["ios-devices"],
          });
        }
      } catch {
        // Error toast is already shown by the mutation's onError.
      } finally {
        setRetryingMap((prev) => {
          const next = { ...prev };
          delete next[serial];
          return next;
        });
        setRetryCooldownMap((prev) => ({ ...prev, [serial]: true }));
        if (retryCooldownTimers.current[serial]) {
          clearTimeout(retryCooldownTimers.current[serial]);
        }
        retryCooldownTimers.current[serial] = setTimeout(() => {
          setRetryCooldownMap((prev) => {
            const next = { ...prev };
            delete next[serial];
            return next;
          });
        }, 10000);
      }
    },
    [retryDevice, retryingMap, retryCooldownMap, sourcePickerQueryClient],
  );

  const handleTierChange = useCallback(
    (serial: string, tier: DeviceTierChoice) => {
      setPreferredTier.mutate({ serial, tier });
    },
    [setPreferredTier],
  );

  // The preview empty-state ("Try another driver") asks the picker to
  // expand the device list so the user can pick a different driver.
  useEffect(() => {
    const expand = () => setDeviceListExpanded(true);
    window.addEventListener("transfera:show-drivers", expand);
    return () => window.removeEventListener("transfera:show-drivers", expand);
  }, []);
  const triggerRecover = useCallback(() => {
    if (recoverMutation.isPending || recoverCooldown) return;
    recoverMutation.mutate();
  }, [recoverMutation, recoverCooldown]);
  const [appleElevationCommand, setAppleElevationCommand] = useState<
    string[] | null
  >(null);
  const handleAppleElevationConfirm = async () => {
    const cmd = appleElevationCommand;
    setAppleElevationCommand(null);
    if (cmd && cmd.length >= 2 && isDesktop) {
      await runElevated({
        executable: cmd[0]!,
        args: cmd.slice(1),
        description: "Start Apple Mobile Device Service",
      });
      useTransferStore
        .getState()
        .showNotification(
          "info",
          "Apple service elevation requested. Refreshing device list...",
        );
      setTimeout(() => recoverMutation.mutate(), 2000);
    }
  };
  const handleAppleElevationCancel = () => setAppleElevationCommand(null);

  // Handle recovery results: show elevation prompt when needed
  useEffect(() => {
    if (!recoverMutation.data) return;
    const r = recoverMutation.data;
    if (r.overall === "elevation_required" && r.service.elevation_command) {
      setAppleElevationCommand(r.service.elevation_command);
    } else if (r.overall === "needs_bind") {
      useTransferStore
        .getState()
        .showNotification(
          "warning",
          "USB device needs binding before it can be attached. Open Device Setup and run bind with admin rights.",
        );
    } else if (r.overall === "needs_elevation") {
      useTransferStore
        .getState()
        .showNotification(
          "warning",
          "USB attach requires administrator permissions.",
        );
    } else if (r.overall === "no_device_found") {
      useTransferStore
        .getState()
        .showNotification(
          "error",
          "No Apple device found. Check your USB connection and try again.",
        );
    } else if (r.overall === "service_already_running") {
      // Service was already running — nothing changed, so this is NOT a
      // success. The guidance toast already fired from the mutation's
      // onSuccess; leave the failure banner as-is (no toast here).
    }
    // Lane 3: forward re-enumerated devices so the page can auto-migrate
    // the preview source onto the live Tier-1 serial + force a refetch.
    if (r.devices && r.devices.length > 0) {
      onRecovered?.(r.devices);
    }
  }, [recoverMutation.data, onRecovered]);

  // Resolve the initializing spinner when any tier finds a device
  const anyTierFoundDevice = iosAvailable || readyDevices.length > 0;

  const [selectedDevice, setSelectedDevice] = useState<IOSDeviceInfo | null>(
    null,
  );
  const pathValidation = useValidatePath(
    sourceRef?.type === "local_folder" ? sourceRef.path : null,
  );

  // Handle folder selection via native dialog
  const handleBrowseFolder = async () => {
    if (isDesktop) {
      const currentPath =
        sourceRef?.type === "local_folder" ? sourceRef.path : undefined;
      const selected = await openDirectory(currentPath);
      if (selected) {
        onSourceChange({ type: "local_folder", path: selected });
        setMode("folder");
      }
    } else {
      useTransferStore
        .getState()
        .showNotification(
          "info",
          "Folder browsing requires the desktop app. Please enter the folder path directly.",
        );
    }
  };

  // Handle device selection — open the virtual folder browser
  const handleSelectDevice = (device: IOSDeviceInfo) => {
    setSelectedDevice(device);
    setMode("device");
  };

  // Handle device folder browser path selection
  const handleDevicePathSelected = (devicePath: string) => {
    if (selectedDevice) {
      onSourceChange({
        type: "device",
        device_id: selectedDevice.serial,
        device_path: devicePath,
        device_name: selectedDevice.name,
      });
      // Close the device browser so the user sees their selected path
      // confirmed in the source-selection view.
      setSelectedDevice(null);
      setMode("none");
    }
  };

  // Handle going back from device browser to device list
  const handleDeviceBrowserBack = () => {
    setSelectedDevice(null);
    setMode("none");
  };

  // Handle clearing the source
  const handleClear = () => {
    onSourceChange(null);
    setMode("none");
    setSelectedDevice(null);
  };

  const isIOS = sourceRef?.type === "device";
  const isFolder = sourceRef?.type === "local_folder";

  // If a device browser is active, show it
  if (mode === "device" && selectedDevice) {
    return (
      <div>
        <label className="text-sm font-semibold text-foreground mb-1.5 block">
          Source
        </label>
        <DeviceFolderBrowser
          device={selectedDevice}
          onSelectPath={handleDevicePathSelected}
          onBack={handleDeviceBrowserBack}
        />
      </div>
    );
  }

  return (
    <div>
      <label className="text-sm font-semibold text-foreground mb-1.5 block">
        Source
      </label>

      {/* Two distinct entry points — stacked vertically like Explorer's sidebar */}
      <div className="space-y-2">
        {/* Entry point 1: Browse a folder on this PC */}
        <div
          className={cn(
            "border rounded-xl transition-colors",
            isFolder
              ? "border-green-300 dark:border-green-700 bg-green-50/50 dark:bg-green-950/20"
              : "border-border hover:border-muted-foreground/30",
          )}
        >
          <div className="flex items-center gap-3 p-3 pb-2">
            <div
              className={cn(
                "w-10 h-10 rounded-lg flex items-center justify-center shrink-0",
                isFolder ? "bg-green-100 dark:bg-green-900/30" : "bg-muted",
              )}
            >
              <FolderOpen
                className={cn(
                  "w-5 h-5",
                  isFolder
                    ? "text-green-600 dark:text-green-400"
                    : "text-muted-foreground",
                )}
              />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-foreground">
                Browse a folder on this PC
              </p>
              <p className="text-xs text-muted-foreground mt-0.5">
                Select or type a folder path containing photos or videos
              </p>
            </div>
          </div>

          <div className="px-3 pb-3 flex gap-2">
            <div className="flex-1 relative">
              <input
                type="text"
                value={
                  isFolder && sourceRef.type === "local_folder"
                    ? sourceRef.path
                    : ""
                }
                onChange={(e) => {
                  const val = e.target.value;
                  if (val.trim()) {
                    onSourceChange({ type: "local_folder", path: val });
                    setMode("folder");
                  } else {
                    handleClear();
                  }
                }}
                placeholder="Enter folder path..."
                className={cn(
                  "w-full px-3 py-2 bg-background border rounded-lg text-xs text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring transition-colors",
                  isFolder &&
                    sourceRef.type === "local_folder" &&
                    sourceRef.path.trim().length > 0
                    ? "border-green-300 dark:border-green-700"
                    : "border-border",
                )}
              />
              {isFolder &&
                sourceRef.type === "local_folder" &&
                sourceRef.path.trim().length > 0 &&
                !pathValidation.isLoading &&
                pathValidation.data?.exists && (
                  <FolderCheck className="absolute right-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-green-500 dark:text-green-400" />
                )}
            </div>
            <button
              type="button"
              onClick={handleBrowseFolder}
              className="no-drag px-3 py-2 bg-primary text-primary-foreground rounded-lg text-xs font-normal hover:bg-primary/90 active:scale-[0.95] transition-all flex items-center gap-1 shrink-0"
            >
              <FolderOpen className="w-3.5 h-3.5" />
              Browse
            </button>
            {isFolder && (
              <button
                type="button"
                onClick={handleClear}
                className="p-1.5 text-muted-foreground hover:text-foreground transition-colors flex items-center justify-center shrink-0"
                title="Clear source"
              >
                <XCircle className="w-4 h-4" />
              </button>
            )}
          </div>

          {/* Path validation feedback */}
          {isFolder && sourceRef.type === "local_folder" && (
            <div className="px-3 pb-3">
              {pathValidation.isLoading && (
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Checking path...
                </div>
              )}
              {!pathValidation.isLoading &&
                pathValidation.data &&
                !pathValidation.data.exists && (
                  <div className="flex items-center gap-1.5 text-xs text-red-500">
                    <AlertCircle className="w-3 h-3" />
                    Path does not exist or is not a directory
                  </div>
                )}
              {!pathValidation.isLoading &&
                pathValidation.data &&
                pathValidation.data.exists && (
                  <div className="flex items-center gap-1.5 text-xs text-green-600 dark:text-green-400">
                    <CheckCircle2 className="w-3 h-3" />
                    Folder found
                  </div>
                )}
            </div>
          )}
        </div>

        {/* Entry point 2: Connected devices */}
        <div
          id="source-devices"
          className={cn(
            "border rounded-xl transition-colors",
            isIOS
              ? isDeviceDisconnected
                ? "border-amber-300 dark:border-amber-700 bg-amber-50/30 dark:bg-amber-950/20"
                : "border-blue-300 dark:border-blue-700 bg-blue-50/50 dark:bg-blue-950/20"
              : "border-border hover:border-muted-foreground/30",
          )}
        >
          <div
            className="flex items-center gap-3 p-3 cursor-pointer select-none"
            onClick={() => setDeviceListExpanded(!deviceListExpanded)}
          >
            <div
              className={cn(
                "w-10 h-10 rounded-lg flex items-center justify-center shrink-0",
                isIOS
                  ? isDeviceDisconnected
                    ? "bg-amber-100 dark:bg-amber-900/30"
                    : "bg-blue-100 dark:bg-blue-900/30"
                  : "bg-muted",
              )}
            >
              <Smartphone
                className={cn(
                  "w-5 h-5",
                  isIOS
                    ? isDeviceDisconnected
                      ? "text-amber-600 dark:text-amber-400"
                      : "text-blue-600 dark:text-blue-400"
                    : "text-muted-foreground",
                )}
              />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-foreground">
                Connected devices
              </p>
              {isIOS && sourceRef.type === "device" && (
                <p
                  className={cn(
                    "text-xs truncate mt-0.5",
                    isDeviceDisconnected
                      ? "text-amber-600 dark:text-amber-400"
                      : "text-muted-foreground",
                  )}
                >
                  {isDeviceDisconnected
                    ? sourceRef.device_name
                      ? `${sourceRef.device_name} (Disconnected)`
                      : "Device disconnected"
                    : sourceRef.device_name
                      ? `${sourceRef.device_name} — ${sourceRef.device_path}`
                      : "Device connected"}
                </p>
              )}
              {!isIOS && (
                <p className="text-xs text-muted-foreground mt-0.5">
                  iPhone, iPad, or other connected device
                </p>
              )}
            </div>
            <div
              className="flex items-center gap-1.5"
              onClick={(e) => e.stopPropagation()}
            >
              {isIOS && isIOSDevice && !deviceLocked && onToggleIPhoneTips && (
                <button
                  type="button"
                  onClick={onToggleIPhoneTips}
                  className={cn(
                    "w-8 h-8 rounded-lg flex items-center justify-center transition-all active:scale-[0.95]",
                    showIPhoneTips
                      ? "bg-blue-600 text-white shadow-xs"
                      : "bg-blue-100/70 dark:bg-blue-900/40 text-blue-600 dark:text-blue-300 border border-blue-200/80 dark:border-blue-700/60 hover:bg-blue-200/70 dark:hover:bg-blue-800/60",
                  )}
                  title="Keep your iPhone awake during preview & transfer (click to toggle tips)"
                  aria-label="Toggle keep iPhone awake guidance"
                  aria-expanded={showIPhoneTips}
                >
                  <Smartphone className="w-4 h-4" />
                </button>
              )}
              {isIOS && (
                <button
                  type="button"
                  onClick={handleClear}
                  className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
                  aria-label="Clear selected source"
                  title="Clear source"
                >
                  <XCircle className="w-4 h-4" />
                </button>
              )}
              <button
                type="button"
                onClick={() => setDeviceListExpanded(!deviceListExpanded)}
                className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
                aria-label={
                  deviceListExpanded
                    ? "Collapse device list"
                    : "Expand device list"
                }
                title={
                  deviceListExpanded
                    ? "Collapse device list"
                    : "Expand device list"
                }
              >
                <ChevronRight
                  className={cn(
                    "w-4 h-4 transition-transform duration-200",
                    deviceListExpanded && "rotate-90",
                  )}
                />
              </button>
            </div>
          </div>

          {/* Device list — collapsible */}
          <AnimatePresence initial={false}>
            {deviceListExpanded && (
              <motion.div
                key="device-list"
                initial={{ height: 0, opacity: 0 }}
                animate={{
                  height: "auto",
                  opacity: 1,
                  transitionEnd: { overflow: "visible" },
                }}
                exit={{ height: 0, opacity: 0, overflow: "hidden" }}
                transition={{ duration: 0.2, ease: "easeInOut" }}
                className="overflow-hidden"
              >
                <div className="px-3 pb-3 space-y-2">
                  {iosLoading && (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground py-2">
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      <span>Scanning for devices...</span>
                    </div>
                  )}

                  {!iosLoading && allDevices.length === 0 && (
                    <>
                      {(() => {
                        /* Device manager is still probing tiers — show a brief
                     loading message instead of a premature empty state.
                     Time out after 30s to avoid an infinite spinner.
                     Resolve as soon as any tier enumerates a device. */
                        if (
                          backendStatus.data?.initializing &&
                          !anyTierFoundDevice
                        ) {
                          if (initializingTimedOut) {
                            return (
                              <div className="space-y-2">
                                <div className="flex items-start gap-2 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg p-2.5">
                                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0" />
                                  <div>
                                    <p className="text-xs font-normal text-amber-700 dark:text-amber-300">
                                      Connection check taking longer than
                                      expected
                                    </p>
                                    <p className="text-[10px] text-amber-600 dark:text-amber-400 mt-0.5">
                                      Your iPhone may still connect via the
                                      Windows driver.
                                    </p>
                                  </div>
                                </div>
                                <button
                                  onClick={triggerRecover}
                                  disabled={recoverBusy}
                                  title={
                                    recoverCooldown
                                      ? "Wait a few seconds before retrying"
                                      : undefined
                                  }
                                  className="flex items-center gap-1.5 text-xs bg-primary text-primary-foreground px-3 py-1.5 rounded-lg hover:bg-primary/90 transition-colors disabled:opacity-50"
                                >
                                  <RefreshCw
                                    className={`w-3 h-3 ${recoverMutation.isPending ? "animate-spin" : ""}`}
                                  />
                                  {recoverMutation.isPending
                                    ? "Recovering..."
                                    : recoverCooldown
                                      ? "Wait before retrying..."
                                      : "Try Auto-Recovery"}
                                </button>
                              </div>
                            );
                          }
                          return (
                            <div className="flex items-center gap-2 text-xs text-muted-foreground py-2">
                              <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              <span>
                                Detecting iPhone connection method&hellip;
                              </span>
                            </div>
                          );
                        }

                        const activeTier = tier2Status.data?.active_tier;

                        /* Some tier is actively working, just no devices
                      connected right now — amber empty-state so the lack
                      of devices reads as "action needed", not neutral. */
                        if (activeTier && activeTier !== "none") {
                          return (
                            <div className="space-y-2.5">
                              <div className="flex items-start justify-between gap-2 bg-amber-500/10 border border-amber-500/30 rounded-lg p-3">
                                <div className="flex items-start gap-2.5">
                                  <Smartphone className="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
                                  <div>
                                    <p className="text-xs font-semibold text-foreground">
                                      No devices detected yet
                                    </p>
                                    <p className="text-[11px] text-muted-foreground mt-0.5">
                                      Connect your iPhone, iPad, Android phone,
                                      or Digital Camera using a USB cable.
                                    </p>
                                  </div>
                                </div>
                                <button
                                  type="button"
                                  onClick={triggerRecover}
                                  disabled={recoverBusy}
                                  title={
                                    recoverCooldown
                                      ? "Wait a few seconds before retrying"
                                      : undefined
                                  }
                                  className="text-[11px] text-primary hover:underline flex items-center gap-1 shrink-0 disabled:opacity-50"
                                >
                                  <RefreshCw
                                    className={cn(
                                      "w-3 h-3",
                                      recoverMutation.isPending &&
                                        "animate-spin",
                                    )}
                                  />
                                  Scan again
                                </button>
                              </div>

                              <div className="p-3 bg-muted/40 dark:bg-muted/20 border border-border rounded-xl text-xs space-y-2">
                                <p className="font-semibold text-foreground text-[11px] uppercase tracking-wider flex items-center justify-between">
                                  <span>Device Connection Checklist</span>
                                  <span className="text-[10px] text-muted-foreground font-normal lowercase">
                                    iOS &middot; Android &middot; Cameras
                                  </span>
                                </p>
                                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 pt-0.5 text-[11px]">
                                  {/* iPhone / iPad */}
                                  <div className="p-2 rounded-lg bg-blue-500/5 border border-blue-500/20 space-y-1">
                                    <p className="font-semibold text-blue-700 dark:text-blue-300">
                                      iPhone / iPad
                                    </p>
                                    <ul className="text-muted-foreground space-y-0.5 list-disc list-inside text-[10.5px]">
                                      <li>Screen unlocked with passcode</li>
                                      <li>
                                        Tap <em>Trust This Computer</em>
                                      </li>
                                      <li>Data-capable USB cable</li>
                                    </ul>
                                  </div>

                                  {/* Android */}
                                  <div className="p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/20 space-y-1">
                                    <p className="font-semibold text-emerald-700 dark:text-emerald-300">
                                      Android Devices
                                    </p>
                                    <ul className="text-muted-foreground space-y-0.5 list-disc list-inside text-[10.5px]">
                                      <li>Screen unlocked</li>
                                      <li>
                                        USB:{" "}
                                        <strong>File Transfer (MTP)</strong>
                                      </li>
                                      <li>Avoid "Charging only" mode</li>
                                    </ul>
                                  </div>

                                  {/* Cameras / Storage */}
                                  <div className="p-2 rounded-lg bg-purple-500/5 border border-purple-500/20 space-y-1">
                                    <p className="font-semibold text-purple-700 dark:text-purple-300">
                                      Cameras &amp; Drives
                                    </p>
                                    <ul className="text-muted-foreground space-y-0.5 list-disc list-inside text-[10.5px]">
                                      <li>Power camera ON</li>
                                      <li>
                                        Set USB Mode to <strong>MTP/PTP</strong>
                                      </li>
                                      <li>Or insert SD card in reader</li>
                                    </ul>
                                  </div>
                                </div>
                              </div>
                            </div>
                          );
                        }

                        /* Tier status still loading — wait before showing any warning */
                        if (tier2Status.isLoading) {
                          return (
                            <div className="flex items-center gap-2 text-xs text-muted-foreground py-2">
                              <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              <span>Checking device access...</span>
                            </div>
                          );
                        }

                        /* No tier is working at all — determine which blocking
                     message is most actionable */
                        if (!activeTier || activeTier === "none") {
                          /* Tier 1 is partially available (pymobiledevice3 installed)
                       just needs the Apple driver started */
                          if (
                            iosDevices?.available &&
                            iosDevices.driver_status === "no_driver"
                          ) {
                            return (
                              <div className="space-y-3 py-1">
                                <div className="text-xs text-muted-foreground">
                                  <DriverInstallerInline />
                                </div>
                              </div>
                            );
                          }

                          /* Absolutely nothing is usable */
                          return (
                            <div className="space-y-3 py-1">
                              <div className="flex items-start gap-2.5 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg p-3">
                                <AlertTriangle className="w-4 h-4 text-primary mt-0.5 shrink-0" />
                                <div className="space-y-2 flex-1">
                                  <div>
                                    <p className="text-xs font-semibold text-foreground">
                                      Apple Support (For iPhones &amp; iPads)
                                    </p>
                                    <p className="text-[11px] text-muted-foreground mt-0.5">
                                      Install Apple Mobile Device Support to
                                      transfer files from iOS devices. Android
                                      phones, digital cameras, and USB drives
                                      work natively with built-in Windows
                                      drivers.
                                    </p>
                                  </div>
                                  <DriverInstallerInline />
                                </div>
                              </div>
                            </div>
                          );
                        }

                        return null;
                      })()}
                    </>
                  )}

                  {readyDevices.map((device) => {
                    const meta = getDeviceMeta(device);
                    const isSelected =
                      sourceRef?.type === "device" &&
                      sourceRef.device_id.toLowerCase() ===
                        device.serial.toLowerCase();
                    const preferred: DeviceTierChoice =
                      (device.user_tier as
                        DeviceTierChoice | null | undefined) ?? "auto";
                    const serving = device.active_tier ?? null;
                    const isIOSRow =
                      meta.category === "iphone" ||
                      meta.category === "ipad" ||
                      meta.category === "ipod" ||
                      meta.platformBadge === "iOS" ||
                      meta.platformBadge === "iPadOS";
                    const isRetrying = !!retryingMap[device.serial];
                    const coolingDown = !!retryCooldownMap[device.serial];
                    const retryBusy = isRetrying || coolingDown;
                    // Check if this device is the currently selected one and is locked
                    const isCurrentDeviceLocked =
                      deviceLocked &&
                      sourceRef?.type === "device" &&
                      sourceRef.device_id.toLowerCase() ===
                        device.serial.toLowerCase();
                    return (
                      <div
                        key={device.serial}
                        className={cn(
                          "w-full rounded-lg transition-all",
                          isSelected
                            ? "bg-action/10 border-2 border-action text-foreground shadow-xs"
                            : "bg-background hover:bg-muted/60 border border-border",
                        )}
                      >
                        <div
                          role="button"
                          tabIndex={0}
                          onClick={() => handleSelectDevice(device)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              handleSelectDevice(device);
                            }
                          }}
                          className="w-full flex items-center gap-2.5 p-2.5 text-left cursor-pointer active:scale-[0.99]"
                        >
                          <div
                            className={cn(
                              "w-8 h-8 rounded-lg flex items-center justify-center shrink-0",
                              meta.accentColor.bg,
                            )}
                          >
                            {meta.iconType === "tablet" ? (
                              <Tablet
                                className={cn("w-4 h-4", meta.accentColor.text)}
                              />
                            ) : meta.iconType === "camera" ? (
                              <Camera
                                className={cn("w-4 h-4", meta.accentColor.text)}
                              />
                            ) : (
                              <Smartphone
                                className={cn("w-4 h-4", meta.accentColor.text)}
                              />
                            )}
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-1.5">
                              <p className="text-xs font-semibold text-foreground truncate">
                                {meta.displayName}
                              </p>
                              <span
                                className={cn(
                                  "text-[9px] px-1.5 py-0.5 rounded-pill border shrink-0",
                                  isCurrentDeviceLocked
                                    ? "bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400 border-orange-300 dark:border-orange-700"
                                    : meta.accentColor.badgeBg,
                                  isCurrentDeviceLocked
                                    ? ""
                                    : meta.accentColor.badgeText,
                                  isCurrentDeviceLocked
                                    ? ""
                                    : meta.accentColor.border,
                                )}
                              >
                                {isCurrentDeviceLocked
                                  ? "Locked"
                                  : meta.platformBadge}
                              </span>
                            </div>
                            <p className="text-[10px] text-muted-foreground truncate">
                              {device.model || meta.categoryLabel}
                              {device.ios_version &&
                              device.ios_version.toLowerCase() !== "unknown" &&
                              !device.ios_version
                                .toLowerCase()
                                .includes("android")
                                ? ` · iOS ${device.ios_version}`
                                : null}
                            </p>
                          </div>
                          <div
                            className="flex items-center gap-1.5"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {!isCurrentDeviceLocked && (
                              <>
                                <span className="text-[9px] px-1.5 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded-pill font-medium">
                                  Ready
                                </span>
                                {(serving === "tier1" ||
                                  serving === "wpd" ||
                                  serving === "tier2") && (
                                  <span className="text-[9px] text-muted-foreground shrink-0">
                                    via {tierLabel(serving)}
                                  </span>
                                )}
                                <button
                                  type="button"
                                  onClick={() =>
                                    handleRetryDevice(device.serial)
                                  }
                                  disabled={retryBusy}
                                  title={
                                    coolingDown
                                      ? "Wait a few seconds before retrying this device"
                                      : "Retry this device"
                                  }
                                  className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-50"
                                >
                                  {isRetrying ? (
                                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                  ) : (
                                    <RefreshCw className="w-3.5 h-3.5" />
                                  )}
                                </button>
                              </>
                            )}
                            {isCurrentDeviceLocked && (
                              <span className="text-[9px] px-1.5 py-0.5 bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400 rounded-pill font-medium flex items-center gap-1">
                                <Lock className="w-3 h-3" />
                                Locked
                              </span>
                            )}
                            <ChevronRight className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                          </div>
                        </div>
                        <div
                          className="px-2.5 pb-2.5 pl-[52px] space-y-1"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <TierSelect
                            value={preferred}
                            serving={serving}
                            availableTiers={
                              iosDevices?.available_tiers ?? undefined
                            }
                            disabled={setPreferredTier.isPending}
                            onChange={(tier) =>
                              handleTierChange(device.serial, tier)
                            }
                            onOpenTier2Setup={() => {
                              setActiveSetupSerial(device.serial);
                              setTier2ModalOpen(true);
                            }}
                          />
                          <TierMismatchNotice
                            preferred={preferred}
                            serving={serving}
                            availableTiers={
                              iosDevices?.available_tiers ?? undefined
                            }
                            onOpenTier2Setup={() => {
                              setActiveSetupSerial(device.serial);
                              setTier2ModalOpen(true);
                            }}
                          />
                          {preferred === "wpd" && isIOSRow && (
                            <p className="text-[10px] text-muted-foreground leading-snug">
                              Windows USB shows photos only (DCIM).
                            </p>
                          )}
                        </div>
                      </div>
                    );
                  })}

                  {nonReadyDevices.map((device) => {
                    const meta = getDeviceMeta(device);
                    return (
                      <div
                        key={device.serial}
                        className="w-full flex items-center gap-2.5 p-2.5 bg-background border border-border rounded-lg opacity-60"
                      >
                        <div className="w-8 h-8 rounded-lg bg-muted flex items-center justify-center shrink-0">
                          {meta.iconType === "tablet" ? (
                            <Tablet className="w-4 h-4 text-muted-foreground" />
                          ) : meta.iconType === "camera" ? (
                            <Camera className="w-4 h-4 text-muted-foreground" />
                          ) : (
                            <Smartphone className="w-4 h-4 text-muted-foreground" />
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5">
                            <p className="text-xs font-semibold text-foreground truncate">
                              {meta.displayName}
                            </p>
                            <span className="text-[9px] px-1.5 py-0.5 rounded-pill bg-muted text-muted-foreground">
                              {meta.platformBadge}
                            </span>
                          </div>
                          <p className="text-[10px] text-muted-foreground">
                            {meta.platformBadge === "Android"
                              ? device.status === "locked"
                                ? 'Unlock phone & select "File Transfer (MTP)"'
                                : 'Set USB Mode to "File Transfer (MTP)"'
                              : device.status === "not_trusted"
                                ? 'Unlock and tap "Trust This Computer"'
                                : device.status === "locked"
                                  ? "Device is locked"
                                  : device.status === "error"
                                    ? device.error_detail || "Connection error"
                                    : device.model}
                          </p>
                        </div>
                        <span
                          className={cn(
                            "text-[9px] px-1.5 py-0.5 rounded-pill font-medium",
                            device.status === "not_trusted"
                              ? "bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400"
                              : device.status === "locked"
                                ? "bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400"
                                : "bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400",
                          )}
                        >
                          {device.status === "not_trusted"
                            ? "Not Trusted"
                            : device.status === "locked"
                              ? "Locked"
                              : "Error"}
                        </span>
                      </div>
                    );
                  })}

                  {/* Apple driver blind spot — usbmux sees nothing but WPD has the phone */}
                  {iosDevices?.driver_status === "ready" &&
                    readyDevices.some(
                      (d) =>
                        d.active_tier === "wpd" &&
                        /apple|iphone|ipad|vid_05ac/i.test(
                          `${d.name} ${d.model}`,
                        ),
                    ) &&
                    !readyDevices.some((d) => d.active_tier === "tier1") && (
                      <AppleBlindSpotGuide
                        driverStatus={iosDevices?.driver_status}
                        wpdAppleNames={readyDevices
                          .filter(
                            (d) =>
                              d.active_tier === "wpd" &&
                              /apple|iphone|ipad|vid_05ac/i.test(
                                `${d.name} ${d.model}`,
                              ),
                          )
                          .map((d) => d.name)}
                        trustNames={nonReadyDevices
                          .filter(
                            (d) =>
                              d.status === "locked" ||
                              d.status === "not_trusted",
                          )
                          .map((d) => d.name)}
                        hasTier1Ready={readyDevices.some(
                          (d) => d.active_tier === "tier1",
                        )}
                        recoverOverall={recoverMutation.data?.overall}
                        tier1Found={recoverMutation.data?.tier1_found}
                        elevationPending={
                          !!appleElevationCommand ||
                          recoverMutation.data?.overall === "elevation_required"
                        }
                        recoverBusy={recoverBusy}
                        recoverCooldown={recoverCooldown}
                        recoverPending={recoverMutation.isPending}
                        onRetry={triggerRecover}
                      />
                    )}

                  {/* Apple service elevation notification */}
                  <AnimatePresence>
                    {appleElevationCommand && (
                      <motion.div
                        initial={{ opacity: 0, y: -8 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -8 }}
                        className="bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-xl p-4 space-y-3"
                      >
                        <div className="flex items-start gap-2.5">
                          <Shield className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
                          <div>
                            <p className="text-sm font-semibold text-blue-700 dark:text-blue-300">
                              Permission Required
                            </p>
                            <p className="text-xs text-blue-600 dark:text-blue-400 mt-1">
                              Administrator privileges are required to start the
                              Apple Mobile Device Service. A Windows User
                              Account Control dialog will appear.
                            </p>
                          </div>
                        </div>
                        <div className="flex gap-2 ml-6">
                          <button
                            type="button"
                            onClick={handleAppleElevationConfirm}
                            className="px-3 py-1.5 bg-action text-white rounded-pill text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all"
                          >
                            Continue
                          </button>
                          <button
                            type="button"
                            onClick={handleAppleElevationCancel}
                            className="px-3 py-1.5 bg-muted text-foreground rounded-lg text-xs font-normal hover:bg-muted/80 transition-colors"
                          >
                            Cancel
                          </button>
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* Linux Bridge Setup Modal triggered from Driver dropdown */}
      <LinuxBridgeSetupModal
        open={tier2ModalOpen}
        onClose={() => setTier2ModalOpen(false)}
        onComplete={() => {
          if (activeSetupSerial) {
            handleTierChange(activeSetupSerial, "tier2");
          }
          setTier2ModalOpen(false);
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// DriverInstallerInline — compact inline driver installer
// ---------------------------------------------------------------------------
function DriverInstallerInline() {
  const installDriver = useInstallDriver();
  const queryClient = useQueryClient();
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleInstall = async () => {
    if (!isDesktop) {
      setError("Automatic installation requires the desktop app.");
      return;
    }
    setInstalling(true);
    setError(null);
    try {
      const result = await installDriver.mutateAsync();
      if (!result.success) {
        // Fall back to elevated install via the Tauri shell
        const elevated = await runElevated({
          executable: "winget",
          args: [
            "install",
            "-e",
            "--id",
            "Apple.AppleMobileDeviceSupport",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--silent",
          ],
        });
        if (elevated.success) {
          queryClient.invalidateQueries({
            queryKey: ["device-backend-status"],
          });
          queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
          setInstalling(false);
          return;
        }
        setError(
          elevated.error ||
            `Installation failed (exit code: ${elevated.exitCode})`,
        );
        setInstalling(false);
        return;
      }
      queryClient.invalidateQueries({ queryKey: ["device-backend-status"] });
      queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
      useTransferStore
        .getState()
        .showNotification(
          "success",
          "Apple Mobile Device Support installed. Please reconnect your iPhone.",
        );
      setInstalling(false);
    } catch (err) {
      setError(extractErrorMessage(err));
      setInstalling(false);
    }
  };

  if (installing) {
    return (
      <div className="flex items-center gap-2 text-xs text-blue-600 dark:text-blue-400 py-1">
        <Loader2 className="w-3 h-3 animate-spin" />
        <span>Installing Apple device support...</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0" />
      <span className="text-xs text-amber-700 dark:text-amber-300">
        Apple device support not installed.
      </span>
      <button
        type="button"
        onClick={handleInstall}
        className="text-[10px] text-primary hover:underline font-normal"
      >
        Install now
      </button>
      {error && <span className="text-[10px] text-red-500">{error}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// WslServiceBrokenCard — shown when the WSL service (LxssManager) is broken
// ---------------------------------------------------------------------------
const WSL_REPAIR_STEPS = [
  { label: "Update WSL", cmd: "wsl --update" },
  { label: "Shut down all distros", cmd: "wsl --shutdown" },
  { label: "Restart Windows", cmd: "(restart your PC)" },
  {
    label: "If still broken — reinstall WSL",
    cmd: "wsl --unregister Ubuntu && wsl --install -d Ubuntu",
  },
] as const;

function WslServiceBrokenCard() {
  const [copied, setCopied] = useState<string | null>(null);

  const copyCmd = (cmd: string) => {
    navigator.clipboard.writeText(cmd).catch(() => {});
    setCopied(cmd);
    setTimeout(() => setCopied(null), 2000);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-xl border border-red-200 dark:border-red-800/60 bg-red-50 dark:bg-red-950/20 p-4 space-y-4"
    >
      {/* Header */}
      <div className="flex items-start gap-3">
        <AlertTriangle className="w-5 h-5 text-red-500 mt-0.5 shrink-0" />
        <div>
          <p className="text-sm font-semibold text-red-700 dark:text-red-300">
            Windows Subsystem for Linux isn't working
          </p>
          <p className="text-xs text-red-600/80 dark:text-red-400/80 mt-1 leading-relaxed">
            The WSL service (LxssManager) returned a catastrophic failure. This
            is a Windows-level issue unrelated to Transfera — it usually means
            WSL needs to be updated or reinstalled.
          </p>
        </div>
      </div>

      {/* Repair steps */}
      <div className="space-y-2 pl-8">
        <p className="text-xs font-semibold text-foreground/70">
          Run these commands in PowerShell (as Administrator):
        </p>
        {WSL_REPAIR_STEPS.map(({ label, cmd }) => (
          <div key={cmd} className="flex items-center gap-2">
            <div className="flex-1 flex items-center gap-2 bg-background/60 dark:bg-background/30 border border-border/60 rounded-lg px-3 py-1.5">
              <span className="text-[11px] text-foreground/50 shrink-0 w-40 truncate">
                {label}
              </span>
              <code className="text-[11px] font-mono text-foreground flex-1 min-w-0 truncate">
                {cmd}
              </code>
            </div>
            <button
              type="button"
              id={`wsl-repair-copy-${label.replace(/\s+/g, "-").toLowerCase()}`}
              onClick={() => copyCmd(cmd)}
              title="Copy to clipboard"
              className="p-1.5 rounded-md hover:bg-red-100 dark:hover:bg-red-900/30 active:scale-[0.95] transition-all text-red-500 dark:text-red-400 shrink-0"
            >
              {copied === cmd ? (
                <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />
              ) : (
                <Copy className="w-3.5 h-3.5" />
              )}
            </button>
          </div>
        ))}
      </div>

      {/* Escape hatches */}
      <div className="pl-8 pt-1 space-y-1.5">
        <p className="text-xs text-foreground/50 mb-2">
          Or continue without iPhone-direct (WSL not required):
        </p>
        <div className="flex flex-wrap gap-2">
          <a
            href="#folder-source"
            id="wsl-broken-use-folder"
            onClick={(e) => {
              e.preventDefault();
              document.getElementById("source-folder-btn")?.click();
            }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-background border border-border text-xs font-normal hover:bg-muted active:scale-[0.95] transition-all"
          >
            <FolderOpen className="w-3 h-3" />
            Browse a folder
          </a>
          <a
            href="#wpd-source"
            id="wsl-broken-use-wpd"
            onClick={(e) => {
              e.preventDefault();
              document.getElementById("source-devices-btn")?.click();
            }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-background border border-border text-xs font-normal hover:bg-muted active:scale-[0.95] transition-all"
          >
            <HardDrive className="w-3 h-3" />
            Use USB / WPD device
          </a>
        </div>
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// LinuxBridgeSetupModal — step-by-step modal for WSL2 + usbipd-win setup
// Opened from Driver dropdown menu ("Linux bridge [Set up]")
// ---------------------------------------------------------------------------
function LinuxBridgeSetupModal({
  open,
  onClose,
  onComplete,
}: {
  open: boolean;
  onClose: () => void;
  onComplete?: () => void;
}) {
  const tier2Status = useTier2Status();
  const preview = useTier2SetupPreview();
  const executeStep = useTier2ExecuteStep();
  const cancelSetup = useTier2Cancel();
  const resetSetup = useTier2Reset();
  const queryClient = useQueryClient();

  const [hasStarted, setHasStarted] = useState(false);
  const [completedSteps, setCompletedSteps] = useState<string[]>([]);
  const [currentStep, setCurrentStep] = useState<string | null>(null);
  const [notificationShown, setNotificationShown] = useState<
    Record<string, boolean>
  >({});
  const [error, setError] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [restartNotification, setRestartNotification] = useState<{
    step: Tier2StepPreview;
  } | null>(null);
  const [elevationNotification, setElevationNotification] = useState<{
    step: Tier2StepPreview;
  } | null>(null);

  const steps = preview.data?.steps || [];
  const status = tier2Status.data;
  const isBridgeReady = status?.bridge_running && status?.bridge_reachable;

  // Determine which step is next
  const nextStep = steps.find(
    (s) => !completedSteps.includes(s.step_id) && s.step_id !== currentStep,
  );

  const handleStartStep = async (step: Tier2StepPreview) => {
    setCurrentStep(step.step_id);
    setError(null);
    setErrorCode(null);
    try {
      const result = await executeStep.mutateAsync({
        step_id: step.step_id,
        confirmed: true,
      });
      if (result.completed) {
        setCompletedSteps((prev) => [...prev, step.step_id]);
        setCurrentStep(null);
        queryClient.invalidateQueries({ queryKey: ["tier2-status"] });
        queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
      } else if (result.restart_required) {
        setRestartNotification({ step });
      } else if (result.error) {
        setError(result.error);
        setErrorCode(result.error_code ?? null);
        setCurrentStep(null);
      }
    } catch (err) {
      setError(extractErrorMessage(err));
      setCurrentStep(null);
    }
  };

  // Auto-advance once user has pressed Start Setup
  useEffect(() => {
    if (hasStarted && !currentStep && nextStep && preview.data) {
      if (!notificationShown[nextStep.step_id]) {
        setNotificationShown((prev) => ({ ...prev, [nextStep.step_id]: true }));
        if (nextStep.requires_restart) {
          setRestartNotification({ step: nextStep });
          return;
        }
        if (nextStep.requires_elevation) {
          setElevationNotification({ step: nextStep });
          return;
        }
        handleStartStep(nextStep);
      }
    }
  }, [hasStarted, currentStep, nextStep, completedSteps, notificationShown]);

  const allCompleted =
    steps.length > 0 && completedSteps.length >= steps.length;

  useEffect(() => {
    if (allCompleted) {
      queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
      queryClient.invalidateQueries({ queryKey: ["tier2-status"] });
      queryClient.invalidateQueries({ queryKey: ["device-backend-status"] });
    }
  }, [allCompleted, queryClient]);

  const handleReset = async () => {
    try {
      await resetSetup.mutateAsync();
    } catch {
      // Ignored
    }
    setCompletedSteps([]);
    setCurrentStep(null);
    setError(null);
    setErrorCode(null);
    setNotificationShown({});
    setRestartNotification(null);
    setElevationNotification(null);
    setHasStarted(false);
  };

  const handleRetry = () => {
    setError(null);
    setErrorCode(null);
    if (nextStep) {
      handleStartStep(nextStep);
    }
  };

  const handleRestartConfirm = async () => {
    setRestartNotification(null);
    if (isDesktop) {
      await restartApp();
    }
  };

  const handleRestartLater = () => {
    setRestartNotification(null);
  };

  const handleElevationConfirm = async () => {
    const step = elevationNotification?.step;
    setElevationNotification(null);
    if (step) {
      handleStartStep(step);
    }
  };

  const handleElevationCancel = () => {
    setElevationNotification(null);
    setCurrentStep(null);
    setHasStarted(false);
  };

  const handleCancel = () => {
    cancelSetup.mutate();
    setCompletedSteps([]);
    setCurrentStep(null);
    setError(null);
    setNotificationShown({});
    setHasStarted(false);
    onClose();
  };

  const handleFinishAndSwitch = () => {
    onComplete?.();
    onClose();
  };

  if (!open) return null;

  return (
    <AnimatePresence>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.96, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.96, y: 10 }}
          transition={{ duration: 0.18, ease: "easeOut" }}
          className="relative w-full max-w-lg bg-card border border-border shadow-2xl rounded-2xl overflow-hidden flex flex-col max-h-[85vh]"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-card/60 backdrop-blur-md">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-action/10 text-action flex items-center justify-center">
                <Terminal className="w-4 h-4" />
              </div>
              <div>
                <h2 className="text-sm font-semibold tracking-tight text-foreground">
                  Linux Bridge Setup
                </h2>
                <p className="text-xs text-muted-foreground">
                  Open-source device access via WSL2
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="p-1.5 rounded-full text-muted-foreground hover:text-foreground hover:bg-muted/80 transition-colors"
              aria-label="Close"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Body */}
          <div className="p-6 space-y-4 overflow-y-auto flex-1">
            {status?.wsl_service_broken ? (
              <WslServiceBrokenCard />
            ) : isBridgeReady || allCompleted ? (
              <div className="space-y-4">
                <div className="p-4 rounded-xl border border-emerald-500/20 bg-emerald-500/5 flex items-start gap-3">
                  <CheckCircle2 className="w-5 h-5 text-emerald-600 dark:text-emerald-400 shrink-0 mt-0.5" />
                  <div className="space-y-1">
                    <p className="text-sm font-semibold text-foreground">
                      Linux Bridge is Ready
                    </p>
                    <p className="text-xs text-muted-foreground">
                      The open-source bridge service is running on port 18920.
                      You can now use it for iOS device transfers without Apple
                      drivers.
                    </p>
                  </div>
                </div>
                <div className="flex justify-end gap-2 pt-2">
                  <button
                    type="button"
                    onClick={handleFinishAndSwitch}
                    className="px-4 py-2 bg-action text-white rounded-pill text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all"
                  >
                    Use Linux Bridge
                  </button>
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                <p className="text-xs text-muted-foreground leading-relaxed">
                  Transfera uses an open-source bridge running inside your WSL2
                  Ubuntu environment to talk to your iOS device. Follow the
                  steps below to download and configure it.
                </p>

                <AnimatePresence>
                  {restartNotification && (
                    <motion.div
                      initial={{ opacity: 0, y: -8 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -8 }}
                      className="bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-xl p-3.5 space-y-2.5"
                    >
                      <div className="flex items-start gap-2.5">
                        <RefreshCw className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
                        <div>
                          <p className="text-xs font-semibold text-amber-700 dark:text-amber-300">
                            Restart Required
                          </p>
                          <p className="text-[11px] text-amber-600 dark:text-amber-400 mt-0.5">
                            {restartNotification.step.restart_description ||
                              `Restarting enables the ${restartNotification.step.title} feature.`}
                          </p>
                        </div>
                      </div>
                      <div className="flex gap-2 ml-6.5">
                        <button
                          type="button"
                          onClick={handleRestartConfirm}
                          className="px-3 py-1 bg-amber-600 text-white rounded-pill text-xs font-normal hover:bg-amber-700 active:scale-[0.95] transition-all"
                        >
                          Restart now
                        </button>
                        <button
                          type="button"
                          onClick={handleRestartLater}
                          className="px-3 py-1 bg-muted text-foreground rounded-pill text-xs font-normal hover:bg-muted/80 transition-colors"
                        >
                          I'll restart later
                        </button>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>

                <AnimatePresence>
                  {elevationNotification && (
                    <motion.div
                      initial={{ opacity: 0, y: -8 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -8 }}
                      className="bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-xl p-3.5 space-y-2.5"
                    >
                      <div className="flex items-start gap-2.5">
                        <Shield className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
                        <div>
                          <p className="text-xs font-semibold text-blue-700 dark:text-blue-300">
                            Administrator Permission Required
                          </p>
                          <p className="text-[11px] text-blue-600 dark:text-blue-400 mt-0.5">
                            {elevationNotification.step.elevation_description ||
                              `This step requires administrator permissions: ${elevationNotification.step.title}. A Windows UAC prompt will appear.`}
                          </p>
                        </div>
                      </div>
                      <div className="flex gap-2 ml-6.5">
                        <button
                          type="button"
                          onClick={handleElevationConfirm}
                          className="px-3 py-1 bg-action text-white rounded-pill text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all"
                        >
                          Continue
                        </button>
                        <button
                          type="button"
                          onClick={handleElevationCancel}
                          className="px-3 py-1 bg-muted text-foreground rounded-pill text-xs font-normal hover:bg-muted/80 transition-colors"
                        >
                          Cancel
                        </button>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>

                {currentStep && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="bg-muted/50 border border-border rounded-xl p-3 flex items-center gap-2.5"
                  >
                    <Loader2 className="w-4 h-4 text-action animate-spin shrink-0" />
                    <span className="text-xs font-semibold text-foreground">
                      {steps.find((s) => s.step_id === currentStep)?.title ||
                        "Executing step..."}
                    </span>
                  </motion.div>
                )}

                {error && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 rounded-xl p-3.5 space-y-2.5"
                  >
                    <div className="flex items-start gap-2.5">
                      <XCircle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
                      <div>
                        <p className="text-xs font-semibold text-red-700 dark:text-red-300">
                          Setup Error
                        </p>
                        <p className="text-[11px] text-red-600 dark:text-red-400 mt-0.5 break-words whitespace-pre-wrap select-text">
                          {errorCode === "NO_WSL_DISTRO"
                            ? "No WSL Linux environment found. Install Ubuntu from the Microsoft Store, then retry."
                            : error}
                        </p>
                      </div>
                    </div>
                    <div className="flex gap-2 ml-6.5">
                      {errorCode === "NO_WSL_DISTRO" ? (
                        <button
                          type="button"
                          onClick={() =>
                            openExternal(
                              "https://apps.microsoft.com/detail/9PDXGNCFSCZV",
                            )
                          }
                          className="px-3 py-1 bg-action text-white rounded-pill text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all"
                        >
                          Open Microsoft Store
                        </button>
                      ) : (
                        <>
                          <button
                            type="button"
                            onClick={handleRetry}
                            className="flex items-center gap-1.5 px-3 py-1 bg-amber-600 text-white rounded-pill text-xs font-normal hover:bg-amber-700 active:scale-[0.95] transition-all"
                          >
                            <RefreshCw className="w-3 h-3" />
                            Retry
                          </button>
                          <button
                            type="button"
                            onClick={handleReset}
                            disabled={resetSetup.isPending}
                            className="flex items-center gap-1.5 px-3 py-1 bg-red-600 text-white rounded-pill text-xs font-normal hover:bg-red-700 active:scale-[0.95] transition-all disabled:opacity-50"
                          >
                            <RotateCcw className="w-3 h-3" />
                            Reset
                          </button>
                        </>
                      )}
                    </div>
                  </motion.div>
                )}

                {preview.isLoading ? (
                  <div className="flex items-center gap-2 text-xs text-muted-foreground py-3">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Detecting WSL environment...</span>
                  </div>
                ) : (
                  <div className="space-y-1.5">
                    {steps.map((step, idx) => {
                      const isCompleted = completedSteps.includes(step.step_id);
                      const isActive = currentStep === step.step_id;
                      const isPending = !isCompleted && !isActive;

                      return (
                        <div
                          key={step.step_id}
                          className={cn(
                            "flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs transition-colors border",
                            isCompleted &&
                              "bg-emerald-500/5 border-emerald-500/20",
                            isActive && "bg-action/5 border-action/30",
                            isPending &&
                              "bg-muted/30 border-transparent opacity-70",
                          )}
                        >
                          <div
                            className={cn(
                              "w-5 h-5 rounded-full flex items-center justify-center shrink-0 text-[10px] font-semibold",
                              isCompleted &&
                                "bg-emerald-500/20 text-emerald-600 dark:text-emerald-400",
                              isActive && "bg-action/20 text-action",
                              isPending && "bg-muted text-muted-foreground",
                            )}
                          >
                            {isCompleted ? "✓" : idx + 1}
                          </div>
                          <div className="flex-1 min-w-0">
                            <p className="font-semibold text-foreground text-[11px]">
                              {step.title}
                            </p>
                            <p className="text-[10px] text-muted-foreground truncate">
                              {step.description}
                            </p>
                          </div>
                          {step.requires_restart && (
                            <span className="text-[9px] px-1.5 py-0.5 bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 rounded-pill shrink-0">
                              Restart
                            </span>
                          )}
                          {step.requires_elevation && (
                            <span className="text-[9px] px-1.5 py-0.5 bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 rounded-pill shrink-0">
                              Admin
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}

                <div className="flex items-center justify-between pt-3 border-t border-border">
                  <button
                    type="button"
                    onClick={handleCancel}
                    className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                  >
                    Cancel
                  </button>
                  {!hasStarted && steps.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setHasStarted(true)}
                      className="px-4 py-1.5 bg-action text-white rounded-pill text-xs font-normal hover:bg-action/90 active:scale-[0.95] transition-all shadow-xs"
                    >
                      Start Setup
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// Preflight Metrics Widget
// ---------------------------------------------------------------------------
function PreflightMetrics({
  sourceRef,
  destPath,
}: {
  sourceRef: SourceRef | null;
  destPath: string;
}) {
  const transferActive = useTransferStore((s) =>
    ["running", "paused"].includes(s.transfer.status),
  );
  const sourcePath = sourceRef?.type === "local_folder" ? sourceRef.path : null;
  const { data, isLoading, isError, error, refetch } = usePreflightValidate(
    sourcePath,
    destPath || null,
    sourceRef?.type === "device" ? sourceRef : null,
    { enabled: !transferActive },
  );

  const hasSource = sourceRef !== null;
  const hasDest = destPath.trim().length > 0;
  const showMetrics = hasSource && hasDest;

  return (
    <AnimatePresence mode="wait">
      {showMetrics && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          exit={{ opacity: 0, height: 0 }}
          transition={{ duration: 0.25, ease: "easeInOut" }}
          className="overflow-hidden"
        >
          <div className="bg-surface-parchment border border-hairline rounded-xl p-5 space-y-4">
            {isLoading && (
              <div className="flex items-center gap-3 text-sm text-muted-foreground">
                <Loader2 className="w-4 h-4 animate-spin text-primary" />
                <span>Analyzing source and destination drive...</span>
              </div>
            )}

            {isError && !isLoading && (
              <div className="flex items-center gap-3 text-sm text-muted-foreground">
                <AlertTriangle className="w-4 h-4 text-amber-500" />
                <span className="flex-1">
                  {(error as any)?.response?.data?.detail ||
                    (error as Error)?.message ||
                    "Unable to calculate disk metrics. Check that both paths exist."}
                </span>
                <button
                  type="button"
                  onClick={() => refetch()}
                  className="px-2.5 py-1 text-xs text-primary hover:text-primary/80 border border-border rounded-full transition-colors shrink-0"
                >
                  Retry
                </button>
              </div>
            )}

            {data && !isLoading && (
              <>
                <div className="grid grid-cols-3 gap-4">
                  <MetricCard
                    label="Files Discovered"
                    value={data.file_count.toLocaleString()}
                    icon={<FileImage className="w-4 h-4" />}
                  />
                  <MetricCard
                    label="Total Backup Size"
                    value={formatBytes(data.source_size_bytes)}
                    icon={<HardDrive className="w-4 h-4" />}
                  />
                  <MetricCard
                    label="Destination Free"
                    value={formatBytes(data.dest_free_bytes)}
                    icon={<HardDrive className="w-4 h-4" />}
                    isWarning={!data.is_sufficient}
                  />
                </div>

                {data.is_sufficient ? (
                  <div className="flex items-center gap-2 text-sm">
                    <CheckCircle2 className="w-4 h-4 text-green-500" />
                    <span className="text-green-700 dark:text-green-400">
                      Destination has sufficient space for this backup
                    </span>
                  </div>
                ) : (
                  <div className="flex items-start gap-2.5 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg p-3.5">
                    <XCircle className="w-4.5 h-4.5 text-red-500 mt-0.5 shrink-0" />
                    <div>
                      <p className="text-sm font-semibold text-red-700 dark:text-red-300">
                        Insufficient destination space
                      </p>
                      <p className="text-xs text-red-600 dark:text-red-400 mt-1">
                        The destination drive needs at least{" "}
                        {formatBytes(data.source_size_bytes)} but only has{" "}
                        {formatBytes(data.dest_free_bytes)} free. Free up{" "}
                        {formatBytes(
                          data.source_size_bytes - data.dest_free_bytes,
                        )}{" "}
                        or choose a different destination.
                      </p>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function MetricCard({
  label,
  value,
  icon,
  isWarning,
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  isWarning?: boolean;
}) {
  return (
    <div className="text-center">
      <div className="flex items-center justify-center gap-1.5 mb-1.5 text-muted-foreground">
        {icon}
        <span className="text-xs font-semibold uppercase tracking-wide">
          {label}
        </span>
      </div>
      <p
        className={cn(
          "text-lg font-semibold",
          isWarning ? "text-red-600 dark:text-red-400" : "text-foreground",
        )}
      >
        {value}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ModeSegmentedControl
// ---------------------------------------------------------------------------
function ModeSegmentedControl({
  value,
  onChange,
}: {
  value: TransferMode;
  onChange: (mode: TransferMode) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <label className="text-sm font-semibold text-foreground">
          Transfer Mode
        </label>
      </div>

      <div className="flex gap-2 p-1 bg-muted rounded-full">
        <button
          type="button"
          onClick={() => onChange("copy")}
          className={cn(
            "flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-full text-sm transition-all active:scale-[0.97]",
            value === "copy"
              ? "bg-primary text-primary-foreground shadow-xs"
              : "text-muted-foreground hover:text-foreground hover:bg-background/50",
          )}
        >
          <Copy className="w-4 h-4" />
          <span className="font-semibold">Backup</span>
        </button>

        <button
          type="button"
          onClick={() => onChange("move")}
          className={cn(
            "flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-full text-sm transition-all active:scale-[0.97]",
            value === "move"
              ? "bg-primary text-primary-foreground shadow-xs"
              : "text-muted-foreground hover:text-foreground hover:bg-background/50",
          )}
        >
          <ArrowRightLeft className="w-4 h-4" />
          <span className="font-semibold">Space Saver</span>
        </button>
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={value}
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 4 }}
          transition={{ duration: 0.15 }}
        >
          {value === "copy" ? (
            <div className="flex items-start gap-2.5 px-4 py-3 bg-blue-50 dark:bg-blue-950/30 border border-blue-100 dark:border-blue-900 rounded-xl">
              <Shield className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-semibold text-blue-700 dark:text-blue-300">
                  Backup (Copy) — Files remain fully safe on your device
                </p>
                <p className="text-xs text-blue-600/70 dark:text-blue-400/70 mt-0.5">
                  Source files are read-only copied to the destination. Nothing
                  is deleted from the original location.
                </p>
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-2.5 px-4 py-3 bg-amber-50 dark:bg-amber-950/30 border border-amber-100 dark:border-amber-900 rounded-xl">
              <AlertTriangle className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-semibold text-amber-700 dark:text-amber-300">
                  Space Saver (Move) — Files removed only after two-stage
                  verification
                </p>
                <p className="text-xs text-amber-600/70 dark:text-amber-400/70 mt-0.5">
                  Files are transferred directly. Source files are deleted only
                  after passing byte-level hash verification across both
                  transfer stages.
                </p>
              </div>
            </div>
          )}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ExtensionBadges
// ---------------------------------------------------------------------------
function ExtensionBadges({
  label,
  icon,
  extensions,
}: {
  label: string;
  icon: React.ReactNode;
  extensions: string[];
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleCount = 8;
  const hasMore = extensions.length > visibleCount;
  const visible = expanded ? extensions : extensions.slice(0, visibleCount);

  return (
    <div className="flex items-start gap-2">
      <div className="w-7 h-7 rounded-lg bg-muted flex items-center justify-center shrink-0 mt-0.5">
        {icon}
      </div>
      <div className="min-w-0">
        <p className="text-xs font-semibold text-foreground">{label}</p>
        <div className="flex flex-wrap gap-1 mt-1">
          {visible.map((ext) => (
            <span
              key={ext}
              className="px-1.5 py-0.5 bg-muted rounded-md text-[10px] text-muted-foreground font-mono"
            >
              {ext}
            </span>
          ))}
          {hasMore && (
            <button
              type="button"
              onClick={() => setExpanded((e) => !e)}
              className="px-1.5 py-0.5 bg-primary/10 hover:bg-primary/20 text-primary rounded-md text-[10px] font-normal transition-colors cursor-pointer"
            >
              {expanded ? "show less" : `+${extensions.length - visibleCount}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeviceSetupPage
// ---------------------------------------------------------------------------
export default function DeviceSetupPage() {
  const sourcePath = useTransferStore((s) => s.ui.setupSourcePath);
  const destPath = useTransferStore((s) => s.ui.setupDestPath);
  const sessionName = useTransferStore((s) => s.ui.setupSessionName);
  const storeTransferMode = useTransferStore((s) => s.ui.setupTransferMode);
  const moveConfirmed = useTransferStore((s) => s.ui.setupMoveConfirmed);
  const onlyNewMode = useTransferStore((s) => s.ui.setupOnlyNewMode);
  const folderLayout = useTransferStore((s) => s.ui.setupFolderLayout);

  // Validate persisted paths on mount (they may be stale from a prior session)
  const { data: sourcePathValid } = useValidatePath(
    sourcePath &&
      !sourcePath.startsWith("ios://") &&
      !sourcePath.startsWith("wpd://")
      ? sourcePath
      : null,
  );
  const { data: destPathValid } = useValidatePath(destPath || null);
  const sourcePathStale =
    sourcePath &&
    !sourcePath.startsWith("ios://") &&
    !sourcePath.startsWith("wpd://") &&
    sourcePathValid &&
    !sourcePathValid.exists;
  const destPathStale = destPath && destPathValid && !destPathValid.exists;

  const setSourcePath = useTransferStore((s) => s.setSetupSourcePath);
  const setDestPath = useTransferStore((s) => s.setSetupDestPath);
  const setSessionName = useTransferStore((s) => s.setSetupSessionName);
  const setStoreTransferMode = useTransferStore((s) => s.setSetupTransferMode);
  const setMoveConfirmed = useTransferStore((s) => s.setSetupMoveConfirmed);
  const setSetupOnlyNewMode = useTransferStore((s) => s.setSetupOnlyNewMode);
  const setSetupFolderLayout = useTransferStore((s) => s.setSetupFolderLayout);
  const resetSetup = useTransferStore((s) => s.resetSetup);

  const defaultTransferMode = useTransferStore((s) => s.ui.defaultTransferMode);
  const setDefaultTransferMode = useTransferStore(
    (s) => s.setDefaultTransferMode,
  );
  const [transferMode, setTransferMode] = useState<TransferMode>(
    storeTransferMode || defaultTransferMode,
  );
  // Files per transfer batch (server clamps 10..500, default 100).
  // Smaller batches = more responsive progress + cheaper retries on flaky links.
  const [batchSize, setBatchSize] = useState(100);

  const { data: config, isLoading: configLoading } = useConfig();
  const createSession = useCreateSession({ silent: true });
  // Centralized engine-readiness flag — the Start action gates on this while
  // navigation and cached previews stay interactive.
  const backendReady = useBackendReady();
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage);
  const initTransfer = useTransferStore((s) => s.initTransfer);
  const showNotification = useTransferStore((s) => s.showNotification);

  const [selectedFiles, setSelectedFiles] = useState<string[]>([]);
  const selectedFilesRef = useRef(selectedFiles);
  selectedFilesRef.current = selectedFiles;

  const [startError, setStartError] = useState<string | null>(null);
  const [confirmForget, setConfirmForget] = useState(false);
  const [pendingDrive, setPendingDrive] = useState<{
    driveLetter: string;
    volumeName: string | null;
  } | null>(null);
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Helper: parse store string path into a typed SourceRef
  const parseSourcePath = useCallback(
    (path: string | null): SourceRef | null => {
      if (!path || !path.trim()) return null;
      if (path.startsWith(IOS_SOURCE_PREFIX)) {
        const withoutPrefix = path.slice(IOS_SOURCE_PREFIX.length);
        const slashIdx = withoutPrefix.indexOf("/");
        const deviceId =
          slashIdx === -1 ? withoutPrefix : withoutPrefix.slice(0, slashIdx);
        return {
          type: "device",
          device_id: deviceId,
          device_path:
            slashIdx === -1 ? "/" : "/" + withoutPrefix.slice(slashIdx + 1),
        };
      }
      return { type: "local_folder", path };
    },
    [],
  );

  // Source reference — the typed source (either local folder or device)
  const [sourceRef, setSourceRef] = useState<SourceRef | null>(() =>
    parseSourcePath(sourcePath),
  );

  // Sync external changes from store (e.g. edited on Dashboard page) into sourceRef
  useEffect(() => {
    const currentStr = sourceRef ? sourceRefToString(sourceRef) : "";
    if (sourcePath !== currentStr) {
      setSourceRef(parseSourcePath(sourcePath));
    }
  }, [sourcePath, parseSourcePath]);

  // Bumped on every recovery so the device preview panel remounts and
  // re-fetches from the (possibly re-enumerated) live device.
  const queryClient = useQueryClient();
  const [previewNonce, setPreviewNonce] = useState(0);
  const [deviceLocked, setDeviceLocked] = useState(false);
  const [scanId, setScanId] = useState<string | null>(null);
  const [showIPhoneTips, setShowIPhoneTips] = useState(false);

  // Lane 3: after a successful recovery, migrate the preview source onto
  // the live Tier-1 READY serial whose canonical id matches the current
  // source (the tier flap changes the raw serial but not the identity).
  const handleRecovered = useCallback(
    (
      devices: NonNullable<
        import("@/types/api").IOSDeviceRecoverResponse["devices"]
      >,
    ) => {
      setPreviewNonce((n) => n + 1);
      if (sourceRef?.type !== "device") return;
      const curCanon = canonicalSerial(sourceRef.device_id);
      const live = devices.find(
        (d) =>
          d.status === "ready" &&
          d.active_tier === "tier1" &&
          canonicalSerial(d.serial) === curCanon,
      );
      if (live && live.serial !== sourceRef.device_id) {
        setSourceRef({
          type: "device",
          device_id: live.serial,
          device_path: sourceRef.device_path,
          device_name: live.name ?? sourceRef.device_name,
        });
        showNotification(
          "success",
          "iPhone reconnected via Apple driver — preview refreshed",
        );
      }
    },
    [sourceRef, showNotification],
  );

  // Device import state for incremental imports. Canonicalize the serial
  // (WPD PnP id vs UDID) so the query key is stable across tier flaps;
  // the backend canonicalizes again before the DB lookup.
  const deviceSerial =
    sourceRef?.type === "device" ? canonicalSerial(sourceRef.device_id) : null;
  const { data: deviceImportState } = useDeviceImportState(deviceSerial);
  const clearDeviceState = useClearDeviceImportState();
  const retryDevicePreview = useRetryDevice();

  // Empty-preview affordance: reveal the per-device driver picker, or retry
  // reaching the device straight from the preview panel.
  const handlePreviewTryAnotherDriver = useCallback(() => {
    window.dispatchEvent(new CustomEvent("transfera:show-drivers"));
    requestAnimationFrame(() => {
      document
        .getElementById("source-devices")
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }, []);
  const handlePreviewRetryDriver = useCallback(() => {
    if (sourceRef?.type !== "device") return;
    const serial = sourceRef.device_id;
    retryDevicePreview.mutate(
      { serial },
      {
        onSuccess: (res) => {
          showNotification(
            "success",
            `Device reconnected via ${tierLabel(res.serving_tier)}`,
          );
          // Escalated recovery restarts the service / re-enumerates, so
          // previously-failed thumbs may now succeed: drop the negative
          // cache and refetch the preview. Same Retry button + spinner.
          if (res.escalated === true) {
            clearAllThumbFailed();
            queryClient.invalidateQueries({ queryKey: ["device-preview"] });
          }
        },
      },
    );
  }, [sourceRef, retryDevicePreview, showNotification, queryClient]);
  const hasDeviceState = !!deviceImportState?.last_successful_cutoff;

  const { data: pageIosDevices, isLoading: pageIosLoading } = useIOSDevices(
    sourceRef?.type !== "local_folder",
  );

  const pageConnectedDevice = useMemo(() => {
    if (sourceRef?.type !== "device" || !pageIosDevices?.devices) return null;
    const targetSerial = canonicalSerial(sourceRef.device_id)?.toLowerCase();
    if (!targetSerial) return null;
    return (
      pageIosDevices.devices.find(
        (d) =>
          canonicalSerial(d.serial)?.toLowerCase() === targetSerial &&
          d.status !== "disconnected",
      ) ?? null
    );
  }, [sourceRef, pageIosDevices]);

  const isDeviceConnected =
    sourceRef?.type === "device"
      ? !pageIosLoading &&
        !!pageConnectedDevice &&
        pageConnectedDevice.status === "ready"
      : true;

  const isIOSDevice = useMemo(() => {
    if (sourceRef?.type !== "device" || !pageConnectedDevice) return false;
    const meta = getDeviceMeta(pageConnectedDevice);
    return (
      meta.platformBadge === "iOS" ||
      meta.platformBadge === "iPadOS" ||
      meta.category === "iphone" ||
      meta.category === "ipad" ||
      meta.category === "ipod"
    );
  }, [sourceRef, pageConnectedDevice]);

  // Keep-awake reminder as a notification when an iOS device is connected
  const keepAwakeNotifiedRef = useRef<string | null>(null);
  useEffect(() => {
    if (
      isDeviceConnected &&
      isIOSDevice &&
      sourceRef?.type === "device" &&
      sourceRef.device_id
    ) {
      const canonicalId =
        canonicalSerial(sourceRef.device_id) ?? sourceRef.device_id;
      const seenKey = canonicalId.toLowerCase();
      if (keepAwakeNotifiedRef.current === canonicalId) return;
      keepAwakeNotifiedRef.current = canonicalId;
      if (keepAwakeNotifiedSerials.has(seenKey)) return;
      keepAwakeNotifiedSerials.add(seenKey);
      showNotification(
        "warning",
        "Keep your iPhone unlocked — preview and transfer pause if it locks. Your phone's auto-lock can't be turned off from this PC, so tap the screen now and then to keep it awake.",
      );
    } else if (!isDeviceConnected || sourceRef?.type !== "device") {
      keepAwakeNotifiedRef.current = null;
    }
  }, [isDeviceConnected, isIOSDevice, sourceRef, showNotification]);

  // Handle device lock state from preview panel
  const handleDeviceLocked = useCallback(() => {
    setDeviceLocked(true);
    // Invalidate device queries so the row flips to Locked
    queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
    queryClient.invalidateQueries({ queryKey: ["device-preview"] });
  }, []);

  const handleDeviceUnlocked = useCallback(() => {
    setDeviceLocked(false);
    // Invalidate device queries so the row flips back to Ready
    queryClient.invalidateQueries({ queryKey: ["ios-devices"] });
    queryClient.invalidateQueries({ queryKey: ["device-preview"] });
  }, []);

  // Debounced dest path for the two preflight subscribers below (widget
  // + page gate). Typing fires zero walks until a 500 ms pause, and both
  // hooks share one debounced value → one query key → one request.
  const debouncedDestPath = useDebouncedValue(destPath, 500);

  // Preflight validation
  const transferActive = useTransferStore((s) =>
    ["running", "paused"].includes(s.transfer.status),
  );
  const sourcePathForPreflight =
    sourceRef?.type === "local_folder" ? sourceRef.path : null;
  const { data: preflight } = usePreflightValidate(
    sourcePathForPreflight,
    debouncedDestPath || null,
    sourceRef?.type === "device" ? sourceRef : null,
    { enabled: !transferActive },
  );

  // Derive the effective source path string for display and legacy compat
  const effectiveSourcePath = sourceRef ? sourceRefToString(sourceRef) : "";

  // Source == Destination check
  const isSamePath =
    effectiveSourcePath.trim().length > 0 &&
    destPath.trim().length > 0 &&
    effectiveSourcePath.trim().toLowerCase() === destPath.trim().toLowerCase();

  const hasPaths =
    effectiveSourcePath.trim().length > 0 && destPath.trim().length > 0;
  const spaceSufficient = !preflight || preflight.is_sufficient;
  const needsMoveConfirm = transferMode === "move" && !moveConfirmed;
  const canStart =
    hasPaths &&
    spaceSufficient &&
    !isSamePath &&
    !needsMoveConfirm &&
    !createSession.isPending &&
    backendReady &&
    (sourceRef?.type !== "device" || isDeviceConnected);

  // Sync sourceRef changes from this page back to store
  useEffect(() => {
    if (effectiveSourcePath !== sourcePath) {
      setSourcePath(effectiveSourcePath);
    }
  }, [effectiveSourcePath, sourcePath, setSourcePath]);

  const handleTransferModeChange = useCallback(
    (mode: TransferMode) => {
      setTransferMode(mode);
      setStoreTransferMode(mode);
      setDefaultTransferMode(mode);
      if (mode === "copy") setMoveConfirmed(false);
    },
    [setStoreTransferMode, setDefaultTransferMode, setMoveConfirmed],
  );

  const handleSelectionConfirm = useCallback(
    (paths: string[], quiet?: boolean) => {
      setSelectedFiles(paths);
      if (paths.length === 0 && !quiet) {
        showNotification(
          "info",
          "Selection cleared — all files will be transferred.",
        );
      }
    },
    [showNotification],
  );

  const handleStart = useCallback(
    async (confirmedPaths?: string[]) => {
      setStartError(null);
      const files = confirmedPaths ?? selectedFilesRef.current;
      if (import.meta.env.DEV && files.length > 0) {
        console.log(
          `[Transfer] Starting session with ${files.length} selected file(s). First 3:`,
          files.slice(0, 3),
        );
      }
      const defaultName =
        sourceRef?.type === "device"
          ? `iPhone Backup – ${new Date().toLocaleDateString("en-US", { month: "short", day: "numeric" })}`
          : `Backup – ${new Date().toLocaleDateString("en-US", { month: "short", day: "numeric" })}`;
      const name = sessionName.trim() || defaultName;
      try {
        const session = await createSession.mutateAsync({
          session_name: name,
          source_root: effectiveSourcePath,
          source_ref: sourceRef ?? undefined,
          dest_root: destPath,
          transfer_mode: transferMode,
          only_new_since_last_import: isIOSDevice && onlyNewMode,
          folder_layout: folderLayout,
          selected_files: files.length > 0 ? files : null,
          batch_size: batchSize,
          scan_id: scanId ?? undefined,
        });
        initTransfer(session);
        resetSetup();
        setSelectedFiles([]);
        setCurrentPage("transfer");
      } catch (err) {
        setStartError(extractErrorMessage(err));
      }
    },
    [
      sessionName,
      effectiveSourcePath,
      sourceRef,
      destPath,
      transferMode,
      isIOSDevice,
      onlyNewMode,
      folderLayout,
      batchSize,
      scanId,
      createSession,
      initTransfer,
      resetSetup,
      setCurrentPage,
    ],
  );

  const handleTransferStart = useCallback(
    (confirmedPaths?: string[]) => {
      if (import.meta.env.DEV) {
        console.log("[Transfer] canStart parts:", {
          hasPaths,
          spaceSufficient,
          isSamePath,
          needsMoveConfirm,
          isPending: createSession.isPending,
          confirmedCount: confirmedPaths?.length ?? null,
          selectedCount: selectedFilesRef.current.length,
        });
      }
      // Guard against double-submit from the preview-panel Start buttons
      // (SourcePreviewPanel has no pending prop, so this early return is
      // the only double-submit protection for that surface).
      if (createSession.isPending) return;
      if (canStart) {
        handleStart(confirmedPaths);
        return;
      }
      if (!hasPaths) {
        showNotification(
          "info",
          "Select both source and destination directories to continue",
        );
      } else if (isSamePath) {
        showNotification("info", "Source and destination cannot be the same");
      } else if (needsMoveConfirm) {
        showNotification(
          "info",
          "Confirm that you understand the move operation",
        );
      } else if (!spaceSufficient) {
        showNotification(
          "info",
          "Free up disk space on the destination to continue",
        );
      } else {
        showNotification(
          "info",
          "Selection saved. Set a destination folder to start the transfer.",
        );
      }
    },
    [
      canStart,
      createSession.isPending,
      handleStart,
      showNotification,
      hasPaths,
      isSamePath,
      needsMoveConfirm,
      spaceSufficient,
    ],
  );

  // Reset only-new-mode when source changes away from iOS device
  useEffect(() => {
    if (!isIOSDevice && onlyNewMode) {
      setSetupOnlyNewMode(false);
    }
  }, [isIOSDevice, onlyNewMode, setSetupOnlyNewMode]);

  // Clear selected files when source changes (but NOT on initial mount —
  // the mount run would wipe a just-confirmed selection restored alongside
  // the source). handleStart already prefers the preview panel's
  // confirmedPaths param over state, so a stale clear can't drop files.
  const prevSourceRef = useRef(sourceRef);
  useEffect(() => {
    if (prevSourceRef.current !== sourceRef) {
      prevSourceRef.current = sourceRef;
      setSelectedFiles([]);
    }
  }, [sourceRef]);

  // Esc key to go back
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setCurrentPage("dashboard");
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [setCurrentPage]);

  // Listen for newly connected removable drives (USB flash, SD card, etc.)
  // No-op in the browser — the Tauri shell emits device:new-removable-drive.
  useEffect(() => {
    if (!isDesktop) return;

    const unsub = onNewRemovableDrive((data) => {
      if (sourceRef) return;
      const currentDest = useTransferStore.getState().ui.setupDestPath;
      if (
        data.driveLetter &&
        currentDest &&
        currentDest
          .trim()
          .toLowerCase()
          .startsWith(data.driveLetter.toLowerCase())
      )
        return;

      setPendingDrive(data);

      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current);
      dismissTimerRef.current = setTimeout(() => setPendingDrive(null), 10000);
    });

    return () => {
      unsub();
      if (dismissTimerRef.current) {
        clearTimeout(dismissTimerRef.current);
        dismissTimerRef.current = null;
      }
    };
  }, [sourceRef]);

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground tracking-tight">
          Device Setup
        </h1>
        <p className="text-sm text-muted-foreground mt-1">
          Configure source and destination for your backup
        </p>
      </div>

      {/* Removable drive detected banner */}
      <AnimatePresence>
        {pendingDrive && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="flex items-center gap-3 px-4 py-3 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 rounded-lg">
              <HardDrive className="w-5 h-5 text-blue-500 shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-semibold text-blue-700 dark:text-blue-300">
                  Detected a new drive at {pendingDrive.driveLetter}
                  {pendingDrive.volumeName
                    ? ` (${pendingDrive.volumeName})`
                    : ""}
                </p>
                <p className="text-xs text-blue-600/70 dark:text-blue-400/70">
                  Use as source for this backup?
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSourceRef({
                    type: "local_folder",
                    path: pendingDrive.driveLetter + "\\",
                  });
                  setPendingDrive(null);
                  if (dismissTimerRef.current) {
                    clearTimeout(dismissTimerRef.current);
                    dismissTimerRef.current = null;
                  }
                }}
                className="px-3 py-1.5 bg-action hover:bg-action/90 text-white text-xs font-normal rounded-pill transition-colors active:scale-[0.95] shrink-0"
              >
                Use as source
              </button>
              <button
                type="button"
                onClick={() => {
                  setPendingDrive(null);
                  if (dismissTimerRef.current) {
                    clearTimeout(dismissTimerRef.current);
                    dismissTimerRef.current = null;
                  }
                }}
                className="p-1 text-blue-400 hover:text-blue-600 dark:hover:text-blue-300 transition-colors shrink-0"
                aria-label="Dismiss"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Source and Destination Selection */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-card border border-border rounded-xl p-6 space-y-5"
      >
        <div className="flex items-center gap-2 mb-1">
          <Settings className="w-5 h-5 text-primary" />
          <h2 className="text-base font-semibold text-foreground">
            Directories
          </h2>
        </div>

        {/* Source: Two distinct entry points */}
        <SourcePicker
          sourceRef={sourceRef}
          onSourceChange={setSourceRef}
          onRecovered={handleRecovered}
          deviceLocked={deviceLocked}
          isIOSDevice={isIOSDevice}
          showIPhoneTips={showIPhoneTips}
          onToggleIPhoneTips={() => setShowIPhoneTips((prev) => !prev)}
        />
        {sourcePathStale && (
          <div className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg text-xs text-amber-700 dark:text-amber-300">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            <span>
              Previously used source path is no longer accessible:{" "}
              <code className="font-mono text-[10px]">{sourcePath}</code>
            </span>
          </div>
        )}

        {/* Source preview panel — inline media picker */}
        {sourceRef?.type === "local_folder" && sourceRef.path && (
          <div className="border border-border rounded-xl p-3 bg-card">
            <SourcePreviewPanel
              sourcePath={sourceRef.path}
              deviceSource={null}
              onSelectionConfirm={handleSelectionConfirm}
              onTransferStart={handleTransferStart}
            />
          </div>
        )}

        {sourceRef?.type === "device" && deviceLocked && (
          <div className="flex items-start gap-2.5 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-xl p-3.5">
            <Lock className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
            <div className="min-w-0">
              <p className="text-xs font-semibold text-amber-700 dark:text-amber-300">
                iPhone locked — transfers pause, they don&apos;t fail
              </p>
              <p className="text-[11px] text-amber-600 dark:text-amber-400 mt-0.5 leading-snug">
                Unlock your iPhone and tap Trust once, then retry the preview.
                Pauses, not failed — Resume reuses the session and skips
                completed files (current file restarts; preview re-scans).
              </p>
            </div>
          </div>
        )}

        {sourceRef?.type === "device" &&
          isIOSDevice &&
          isDeviceConnected &&
          !deviceLocked && (
            <AnimatePresence>
              {showIPhoneTips && (
                <motion.div
                  key="tips-expanded"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <div className="flex items-start gap-2.5 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-xl p-3.5">
                    <Smartphone className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-semibold text-blue-700 dark:text-blue-300">
                        Keep your iPhone awake during preview &amp; transfer
                      </p>
                      <ul className="text-[11px] text-blue-600 dark:text-blue-400 mt-1 space-y-0.5 leading-snug list-disc list-inside">
                        <li>Tap the screen now and then to keep it awake</li>
                        <li>
                          iPhone Settings &gt; Display &gt; Auto-Lock &gt; Never
                          (while transferring)
                        </li>
                        <li>Use a good cable + rear USB port</li>
                        <li>Tap Trust once when asked</li>
                      </ul>
                      <p className="text-[10px] text-blue-600/70 dark:text-blue-400/70 mt-1.5 leading-snug">
                        Brief hiccups retry on their own. If a preview takes
                        longer than 15 seconds we keep this page so you can
                        retry without losing your place.
                      </p>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          )}

        {sourceRef?.type === "device" &&
          sourceRef.device_id &&
          sourceRef.device_path && (
            <div
              key={`device-preview:${sourceRef.device_id}:${previewNonce}`}
              className="border border-border rounded-xl p-3 bg-card"
            >
              <SourcePreviewPanel
                sourcePath={null}
                deviceSource={{
                  device_id: sourceRef.device_id,
                  device_path: sourceRef.device_path,
                }}
                isDeviceConnected={isDeviceConnected}
                onSelectionConfirm={handleSelectionConfirm}
                onTransferStart={handleTransferStart}
                onTryAnotherDriver={handlePreviewTryAnotherDriver}
                onRetryDriver={handlePreviewRetryDriver}
                isRetryingDriver={retryDevicePreview.isPending}
                onDeviceLocked={handleDeviceLocked}
                onDeviceUnlocked={handleDeviceUnlocked}
                deviceLocked={deviceLocked}
                onScanId={setScanId}
              />
            </div>
          )}

        {/* Destination: existing folder picker */}
        <div>
          <label className="text-sm font-semibold text-foreground mb-1.5 block">
            Destination Directory
          </label>
          <div className="flex gap-2">
            <div className="flex-1 relative">
              <input
                type="text"
                value={destPath}
                onChange={(e) => setDestPath(e.target.value)}
                placeholder="Where to store the backup archive..."
                className={cn(
                  "w-full px-3 py-2.5 bg-background border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring transition-colors",
                  destPath.trim().length > 0 && destPathValid?.exists
                    ? "border-green-300 dark:border-green-700"
                    : destPathStale
                      ? "border-amber-300 dark:border-amber-700"
                      : "border-border",
                )}
              />
              {destPath.trim().length > 0 && destPathValid?.exists && (
                <FolderCheck className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-green-500 dark:text-green-400" />
              )}
            </div>
            <button
              onClick={async () => {
                if (isDesktop) {
                  const selected = await openDirectory(destPath || undefined);
                  if (selected) setDestPath(selected);
                } else {
                  showNotification(
                    "info",
                    "Folder browsing requires the desktop app. Please enter the destination path directly.",
                  );
                }
              }}
              className="no-drag px-4 py-2.5 bg-primary text-primary-foreground rounded-pill text-sm font-normal hover:bg-primary/90 active:scale-[0.95] transition-all flex items-center gap-1.5"
            >
              <FolderOpen className="w-4 h-4" />
              Browse
            </button>
          </div>
          {destPathStale && (
            <div className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg text-xs text-amber-700 dark:text-amber-300">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
              <span>
                Previously used destination path is no longer accessible:{" "}
                <code className="font-mono text-[10px]">{destPath}</code>
              </span>
            </div>
          )}
        </div>

        {/* Folder layout */}
        <div>
          <label className="text-sm font-semibold text-foreground mb-2 block">
            Folder Layout
          </label>
          <div className="inline-flex gap-0.5 bg-muted rounded-lg p-0.5">
            {(["year/month/day", "year/month", "flat"] as const).map(
              (layout) => (
                <button
                  key={layout}
                  type="button"
                  onClick={() => setSetupFolderLayout(layout)}
                  className={cn(
                    "px-3 py-1.5 text-xs font-normal rounded-md transition-all",
                    folderLayout === layout
                      ? "bg-background text-foreground shadow-xs"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {layout === "year/month/day" && "Year / Month / Day"}
                  {layout === "year/month" && "Year / Month"}
                  {layout === "flat" && "Flat"}
                </button>
              ),
            )}
          </div>
          <div className="mt-1.5 text-xs text-muted-foreground">
            Preview:{" "}
            {folderLayout === "flat"
              ? "IMG_0001.jpg"
              : folderLayout === "year/month"
                ? `${new Date().getFullYear()}/${String(new Date().getMonth() + 1).padStart(2, "0")}/IMG_0001.jpg`
                : `${new Date().getFullYear()}/${String(new Date().getMonth() + 1).padStart(2, "0")}/${String(new Date().getDate()).padStart(2, "0")}/IMG_0001.jpg`}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            Documents:{" "}
            {folderLayout === "flat"
              ? "Documents/PDFs/Report.pdf"
              : folderLayout === "year/month"
                ? `Documents/PDFs/${new Date().getFullYear()}/${String(new Date().getMonth() + 1).padStart(2, "0")}/Report.pdf`
                : `Documents/PDFs/${new Date().getFullYear()}/${String(new Date().getMonth() + 1).padStart(2, "0")}/${String(new Date().getDate()).padStart(2, "0")}/Report.pdf`}
          </div>
        </div>

        {/* Batch size */}
        <div>
          <label className="text-sm font-semibold text-foreground mb-2 block">
            Batch Size
          </label>
          <div className="inline-flex gap-0.5 bg-muted rounded-lg p-0.5">
            {[25, 50, 100, 200].map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => setBatchSize(n)}
                className={cn(
                  "px-3 py-1.5 text-xs font-normal rounded-md transition-all",
                  batchSize === n
                    ? "bg-background text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {n}
              </button>
            ))}
          </div>
          <div className="mt-1.5 text-xs text-muted-foreground">
            Files per batch. Smaller batches give more responsive progress and
            cheaper retries on flaky links.
          </div>
        </div>

        {isSamePath && (
          <div className="flex items-start gap-2.5 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg p-3.5">
            <AlertCircle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
            <div>
              <p className="text-sm font-semibold text-red-700 dark:text-red-300">
                Source and destination cannot be the same
              </p>
              <p className="text-xs text-red-600 dark:text-red-400 mt-1">
                Choose a different destination directory for your backup.
              </p>
            </div>
          </div>
        )}

        <div>
          <label className="text-sm font-semibold text-foreground mb-1.5 block">
            Backup Name{" "}
            <span className="text-muted-foreground font-normal">
              (optional)
            </span>
          </label>
          <input
            type="text"
            value={sessionName}
            onChange={(e) => setSessionName(e.target.value)}
            placeholder={
              sourceRef?.type === "device"
                ? `iPhone Backup – ${new Date().toLocaleDateString("en-US", { month: "short", day: "numeric" })}`
                : `Backup – ${new Date().toLocaleDateString("en-US", { month: "short", day: "numeric" })}`
            }
            className="w-full px-3 py-2.5 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring transition-colors"
          />
        </div>
      </motion.div>

      {/* Only New Since Last Import — iOS device incremental import */}
      <AnimatePresence>
        {isIOSDevice && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -12 }}
            transition={{ delay: 0.03 }}
            className="bg-card border border-border rounded-xl p-6 space-y-4"
          >
            <div className="flex items-center gap-2 mb-1">
              <Clock className="w-5 h-5 text-primary" />
              <h2 className="text-base font-semibold text-foreground">
                Incremental Import
              </h2>
            </div>

            {hasDeviceState ? (
              <>
                <label className="flex items-start gap-3 cursor-pointer group">
                  <input
                    type="checkbox"
                    checked={onlyNewMode}
                    onChange={(e) => setSetupOnlyNewMode(e.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-border text-primary focus:ring-ring accent-primary"
                  />
                  <span className="text-sm text-foreground">
                    Only import new items since last import from this device
                  </span>
                </label>

                {onlyNewMode && deviceImportState?.last_successful_cutoff && (
                  <div className="ml-7 flex items-start gap-2 bg-blue-50 dark:bg-blue-950/30 border border-blue-100 dark:border-blue-900 rounded-lg p-3">
                    <Clock className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
                    <div>
                      <p className="text-sm text-blue-700 dark:text-blue-300">
                        Only showing items added after{" "}
                        {formatDate(deviceImportState.last_successful_cutoff)}
                      </p>
                      <p className="text-xs text-blue-600/70 dark:text-blue-400/70 mt-1">
                        Files already imported will be skipped. New and
                        previously-failed files will go through the full import
                        pipeline.
                      </p>
                    </div>
                  </div>
                )}

                <div className="flex items-center justify-between pt-2 border-t border-border">
                  <div className="text-xs text-muted-foreground">
                    Last import: session #
                    {deviceImportState.last_import_session_id ?? "—"}
                  </div>
                  {confirmForget ? (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-amber-600 dark:text-amber-400">
                        Force full scan?
                      </span>
                      <button
                        type="button"
                        onClick={() => {
                          if (deviceSerial) {
                            clearDeviceState.mutate(deviceSerial);
                            setSetupOnlyNewMode(false);
                          }
                          setConfirmForget(false);
                        }}
                        disabled={clearDeviceState.isPending}
                        className="text-xs text-destructive hover:underline font-semibold"
                      >
                        Yes, forget
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmForget(false)}
                        className="text-xs text-muted-foreground hover:text-foreground"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setConfirmForget(true)}
                      disabled={clearDeviceState.isPending}
                      className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                    >
                      <RefreshCw
                        className={cn(
                          "w-3 h-3",
                          clearDeviceState.isPending && "animate-spin",
                        )}
                      />
                      Forget last import
                    </button>
                  )}
                </div>
              </>
            ) : (
              <div className="flex items-start gap-2.5 bg-muted/50 border border-border rounded-lg p-3">
                <Smartphone className="w-4 h-4 text-muted-foreground mt-0.5 shrink-0" />
                <div>
                  <p className="text-sm font-semibold text-foreground">
                    First import from this device
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    No previous import history. After your first import
                    completes, you can enable incremental mode to skip
                    already-imported items.
                  </p>
                </div>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Real-time Preflight Metrics */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.05 }}
      >
        <PreflightMetrics sourceRef={sourceRef} destPath={debouncedDestPath} />
      </motion.div>

      {/* Transfer Mode Selection */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="bg-card border border-border rounded-xl p-6"
      >
        <ModeSegmentedControl
          value={transferMode}
          onChange={handleTransferModeChange}
        />

        <AnimatePresence>
          {transferMode === "move" && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <label className="flex items-start gap-3 cursor-pointer group">
                <input
                  type="checkbox"
                  checked={moveConfirmed}
                  onChange={(e) => setMoveConfirmed(e.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-border text-primary focus:ring-ring accent-primary"
                />
                <span className="text-sm text-foreground">
                  I understand that source files will be{" "}
                  <strong>permanently deleted</strong> after byte-level hash
                  verification.
                </span>
              </label>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>

      {/* Supported Formats */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.15 }}
        className="bg-card border border-border rounded-xl p-6"
      >
        <h2 className="text-base font-semibold text-foreground mb-4 flex items-center gap-2">
          <HardDrive className="w-5 h-5 text-primary" />
          Supported Formats
        </h2>

        {configLoading ? (
          <div className="space-y-3">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="h-8 bg-muted rounded-lg animate-pulse" />
            ))}
          </div>
        ) : config ? (
          <div className="space-y-4">
            <ExtensionBadges
              label="Images"
              icon={<FileImage className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.image_extensions}
            />
            <ExtensionBadges
              label="Video"
              icon={<Film className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.video_extensions}
            />
            <ExtensionBadges
              label="Audio"
              icon={<Music className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.audio_extensions}
            />
            <ExtensionBadges
              label="Documents"
              icon={<FileText className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.document_extensions}
            />
          </div>
        ) : null}

        {config && (
          <details className="mt-4 pt-3 border-t border-border">
            <summary className="text-[11px] text-muted-foreground cursor-pointer hover:text-foreground transition-colors">
              Developer &amp; engine info
            </summary>
            <div className="flex items-center gap-4 text-xs text-muted-foreground mt-2 font-mono">
              <span>Batch size: {config.batch_size}</span>
              <span>Max retries: {config.max_retry}</span>
              <span>Port: {config.port}</span>
            </div>
          </details>
        )}
      </motion.div>

      {/* How Transfera Protects Your Media */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.18 }}
        className="bg-card border border-border rounded-xl p-5"
      >
        <details className="group">
          <summary className="flex items-center justify-between cursor-pointer list-none">
            <div className="flex items-center gap-2">
              <ShieldCheck className="w-4.5 h-4.5 text-action" />
              <span className="text-sm font-semibold text-foreground">
                How Transfera Protects Your Media
              </span>
            </div>
            <ChevronRight className="w-4 h-4 text-muted-foreground transition-transform group-open:rotate-90" />
          </summary>

          <div className="mt-4 pt-3 border-t border-border grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
            <div className="p-3 bg-muted/30 border border-border/60 rounded-lg space-y-1">
              <p className="font-semibold text-foreground flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-action" />
                Two-Stage Verification
              </p>
              <p className="text-muted-foreground text-[11px] leading-relaxed">
                Files are streamed to a temporary cache with cryptographic
                BLAKE3 hashes. The app verifies every byte before final saving.
              </p>
            </div>

            <div className="p-3 bg-muted/30 border border-border/60 rounded-lg space-y-1">
              <p className="font-semibold text-foreground flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-action" />
                Zero Overwrites
              </p>
              <p className="text-muted-foreground text-[11px] leading-relaxed">
                Transfera never replaces existing files. If identical names
                exist, numerical suffixes (e.g. _001) preserve all your photos
                safely.
              </p>
            </div>

            <div className="p-3 bg-muted/30 border border-border/60 rounded-lg space-y-1">
              <p className="font-semibold text-foreground flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-action" />
                Smart Date Organization
              </p>
              <p className="text-muted-foreground text-[11px] leading-relaxed">
                Photos and videos are neatly sorted into Year/Month/Day folders
                using camera EXIF timestamps, so you never have to file them
                manually.
              </p>
            </div>

            <div className="p-3 bg-muted/30 border border-border/60 rounded-lg space-y-1">
              <p className="font-semibold text-foreground flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-action" />
                Crash &amp; Disconnect Recovery
              </p>
              <p className="text-muted-foreground text-[11px] leading-relaxed">
                If your PC restarts, sleeps, or the cable disconnects, Transfera
                automatically resumes from the exact file it left off on.
              </p>
            </div>
          </div>
        </details>
      </motion.div>

      {/* Start Button */}
      <div>
        <motion.button
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
          whileHover={canStart ? { scale: 1.01 } : undefined}
          whileTap={canStart ? { scale: 0.95 } : undefined}
          disabled={!canStart || createSession.isPending}
          onClick={() => handleTransferStart()}
          title={
            !backendReady
              ? ENGINE_WAITING_LABEL
              : canStart
                ? undefined
                : !hasPaths
                  ? "Select both source and destination directories to continue"
                  : isSamePath
                    ? "Source and destination cannot be the same"
                    : needsMoveConfirm
                      ? "Confirm that you understand the move operation"
                      : "Free up disk space on the destination to continue"
          }
          className={cn(
            "no-drag w-full flex items-center justify-center gap-2 px-6 py-3.5 rounded-pill text-sm font-semibold active:scale-[0.99] transition-colors",
            canStart && !createSession.isPending
              ? "bg-primary text-primary-foreground hover:bg-primary/90"
              : "bg-muted text-muted-foreground cursor-not-allowed",
          )}
        >
          {createSession.isPending ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Creating session...
            </>
          ) : selectedFiles.length > 0 ? (
            <>
              Transfer {selectedFiles.length} selected file
              {selectedFiles.length !== 1 ? "s" : ""}
              <ChevronRight className="w-4 h-4" />
            </>
          ) : (
            <>
              Transfer all files
              <ChevronRight className="w-4 h-4" />
            </>
          )}
        </motion.button>
        {startError && (
          <div className="flex items-start gap-2.5 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg p-3.5 mt-3 overflow-hidden">
            <AlertCircle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
            <p className="text-xs text-red-600 dark:text-red-400 break-words overflow-wrap-anywhere whitespace-pre-wrap select-text">
              {startError}
            </p>
          </div>
        )}
        {!canStart && !startError && (
          <p className="text-xs text-muted-foreground text-center mt-2">
            {!hasPaths
              ? "Select both source and destination directories to continue"
              : isSamePath
                ? "Source and destination cannot be the same"
                : needsMoveConfirm
                  ? "Confirm that you understand the move operation"
                  : "Free up disk space on the destination to continue"}
          </p>
        )}
      </div>
    </div>
  );
}
