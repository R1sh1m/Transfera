// ---------------------------------------------------------------------------
// Transfera v2 — Device Setup Page
// Two distinct source entry points: "Browse a folder on this PC" and
// "Connected devices" — mirroring Explorer's model. Device sources use
// a virtual folder browser backed by the device API, not the native OS dialog.
// ---------------------------------------------------------------------------

import { useState, useCallback, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
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
  AlertTriangle,
  ArrowRightLeft,
  Copy,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertCircle,
  Smartphone,
  Clock,
  RefreshCw,
  RotateCcw,
  Wifi,
  Usb,
} from 'lucide-react'
import { useConfig, useCreateSession, usePreflightValidate, useValidatePath, useIOSDevices, useDeviceImportState, useClearDeviceImportState, useTier2Status, useTier2SetupPreview, useTier2ExecuteStep, useTier2Cancel, useTier2Reset, useDevicePreference, useSetDevicePreference } from '@/lib/queries'
import { useTransferStore } from '@/store/transfer'
import { cn, extractErrorMessage, isElectron } from '@/lib/utils'
import type { TransferMode, IOSDeviceInfo, SourceRef, Tier2StepPreview } from '@/types/api'
import { IOS_SOURCE_PREFIX, isWPDPath, sourceRefToString } from '@/types/api'
import { DeviceFolderBrowser } from '@/components/DeviceFolderBrowser'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} MB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

function TierBadge({ tier }: { tier?: string | null }) {
  if (!tier || tier === 'none') return null

  const config = (() => {
    switch (tier) {
      case 'tier1':
        return {
          label: 'Apple Support',
          title: 'Connected via: Apple Mobile Device Support (Tier 1)',
          icon: <Usb className="w-2.5 h-2.5" />,
          className: 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400',
        }
      case 'wpd':
        return {
          label: 'Windows',
          title: 'Connected via: Windows Portable Devices (WPD)',
          icon: <HardDrive className="w-2.5 h-2.5" />,
          className: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400',
        }
      case 'tier2':
        return {
          label: 'Open-source bridge',
          title: 'Connected via: Open-source WSL bridge (Tier 2)',
          icon: <Wifi className="w-2.5 h-2.5" />,
          className: 'bg-teal-100 dark:bg-teal-900/30 text-teal-700 dark:text-teal-400',
        }
      default:
        return null
    }
  })()

  if (!config) return null

  return (
    <span
      className={cn(
        'inline-flex items-center gap-0.5 text-[9px] px-1 py-0.5 rounded',
        config.className,
      )}
      title={config.title}
    >
      {config.icon}
      {config.label}
    </span>
  )
}

function formatDate(dateStr: string): string {
  try {
    return new Date(dateStr).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return dateStr
  }
}

// ---------------------------------------------------------------------------
// SourcePicker — Two distinct entry points
// ---------------------------------------------------------------------------
type SourceMode = 'none' | 'folder' | 'device'

interface SourcePickerProps {
  sourceRef: SourceRef | null
  onSourceChange: (ref: SourceRef | null) => void
}

function SourcePicker({ sourceRef, onSourceChange }: SourcePickerProps) {
  const [mode, setMode] = useState<SourceMode>(
    sourceRef?.type === 'device' ? 'device'
      : sourceRef?.type === 'local_folder' ? 'folder'
      : 'none'
  )

  const [deviceListExpanded, setDeviceListExpanded] = useState(
    sourceRef?.type !== 'local_folder'
  )
  const { data: iosDevices, isLoading: iosLoading } = useIOSDevices(
    sourceRef?.type !== 'local_folder'
  )

  // Auto-collapse device list when a local folder source is selected
  useEffect(() => {
    if (sourceRef?.type === 'local_folder') setDeviceListExpanded(false)
  }, [sourceRef?.type])

  const tier2Status = useTier2Status()
  const allDevices = iosDevices?.devices || []
  const readyDevices = allDevices.filter(d => d.status === 'ready')
  const nonReadyDevices = allDevices.filter(d => d.status !== 'ready')
  const tier2Attention = !!tier2Status.data?.error

  const [selectedDevice, setSelectedDevice] = useState<IOSDeviceInfo | null>(null)
  const pathValidation = useValidatePath(
    sourceRef?.type === 'local_folder' ? sourceRef.path : null
  )

  // Handle folder selection via native dialog
  const handleBrowseFolder = async () => {
    if (isElectron && typeof window.electronAPI?.openDirectory === 'function') {
      const currentPath = sourceRef?.type === 'local_folder' ? sourceRef.path : undefined
      const selected = await window.electronAPI.openDirectory(currentPath)
      if (selected) {
        onSourceChange({ type: 'local_folder', path: selected })
        setMode('folder')
      }
    } else {
      const input = prompt('Enter source folder path:', sourceRef?.type === 'local_folder' ? sourceRef.path : '')
      if (input !== null && input.trim()) {
        onSourceChange({ type: 'local_folder', path: input.trim() })
        setMode('folder')
      }
    }
  }

  // Handle device selection — open the virtual folder browser
  const handleSelectDevice = (device: IOSDeviceInfo) => {
    setSelectedDevice(device)
    setMode('device')
  }

  // Handle device folder browser path selection
  const handleDevicePathSelected = (devicePath: string) => {
    if (selectedDevice) {
      // Guard: reject WPD device paths that slipped into the serial field
      if (isWPDPath(selectedDevice.serial)) {
        console.warn(
          'Device serial looks like a WPD path \u2014 refusing to set as iOS source:',
          selectedDevice.serial,
        )
        return
      }
      onSourceChange({
        type: 'device',
        device_id: selectedDevice.serial,
        device_path: devicePath,
        device_name: selectedDevice.name,
      })
      // Close the device browser so the user sees their selected path
      // confirmed in the source-selection view.
      setSelectedDevice(null)
      setMode('none')
    }
  }

  // Handle going back from device browser to device list
  const handleDeviceBrowserBack = () => {
    setSelectedDevice(null)
    setMode('none')
  }

  // Handle clearing the source
  const handleClear = () => {
    onSourceChange(null)
    setMode('none')
    setSelectedDevice(null)
  }

  const isIOS = sourceRef?.type === 'device'
  const isFolder = sourceRef?.type === 'local_folder'

  // If a device browser is active, show it
  if (mode === 'device' && selectedDevice) {
    return (
      <div>
        <label className="text-sm font-medium text-foreground mb-1.5 block">Source</label>
        <DeviceFolderBrowser
          device={selectedDevice}
          onSelectPath={handleDevicePathSelected}
          onBack={handleDeviceBrowserBack}
        />
      </div>
    )
  }

  return (
    <div>
      <label className="text-sm font-medium text-foreground mb-1.5 block">Source</label>

      {/* Two distinct entry points — stacked vertically like Explorer's sidebar */}
      <div className="space-y-2">
        {/* Entry point 1: Browse a folder on this PC */}
        <div className={cn(
          'border rounded-xl transition-colors',
          isFolder
            ? 'border-green-300 dark:border-green-700 bg-green-50/50 dark:bg-green-950/20'
            : 'border-border hover:border-muted-foreground/30',
        )}>
          <div className="flex items-center gap-3 p-3">
            <div className={cn(
              'w-10 h-10 rounded-lg flex items-center justify-center shrink-0',
              isFolder
                ? 'bg-green-100 dark:bg-green-900/30'
                : 'bg-muted',
            )}>
              <FolderOpen className={cn(
                'w-5 h-5',
                isFolder ? 'text-green-600 dark:text-green-400' : 'text-muted-foreground',
              )} />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-foreground">Browse a folder on this PC</p>
              {isFolder && sourceRef.type === 'local_folder' && (
                <p className="text-xs text-muted-foreground truncate mt-0.5">
                  {sourceRef.path}
                </p>
              )}
              {!isFolder && (
                <p className="text-xs text-muted-foreground mt-0.5">
                  Select a folder containing photos or videos
                </p>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              {isFolder && (
                <button
                  type="button"
                  onClick={handleClear}
                  className="p-1 text-muted-foreground hover:text-foreground transition-colors"
                  title="Clear source"
                >
                  <XCircle className="w-4 h-4" />
                </button>
              )}
              <button
                type="button"
                onClick={handleBrowseFolder}
                className="no-drag px-3 py-1.5 bg-primary text-primary-foreground rounded-lg text-xs font-medium hover:bg-primary/90 active:scale-[0.95] transition-all"
              >
                Browse
              </button>
            </div>
          </div>

          {/* Path validation feedback */}
          {isFolder && sourceRef.type === 'local_folder' && (
            <div className="px-3 pb-3">
              {pathValidation.isLoading && (
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Checking path...
                </div>
              )}
              {!pathValidation.isLoading && pathValidation.data && !pathValidation.data.exists && (
                <div className="flex items-center gap-1.5 text-xs text-red-500">
                  <AlertCircle className="w-3 h-3" />
                  Path does not exist or is not a directory
                </div>
              )}
              {!pathValidation.isLoading && pathValidation.data && pathValidation.data.exists && (
                <div className="flex items-center gap-1.5 text-xs text-green-600 dark:text-green-400">
                  <CheckCircle2 className="w-3 h-3" />
                  Folder found
                </div>
              )}
            </div>
          )}
        </div>

        {/* Entry point 2: Connected devices */}
        <div className={cn(
          'border rounded-xl transition-colors',
          isIOS
            ? 'border-blue-300 dark:border-blue-700 bg-blue-50/50 dark:bg-blue-950/20'
            : 'border-border hover:border-muted-foreground/30',
        )}>
          <div className="flex items-center gap-3 p-3">
            <div className={cn(
              'w-10 h-10 rounded-lg flex items-center justify-center shrink-0',
              isIOS
                ? 'bg-blue-100 dark:bg-blue-900/30'
                : 'bg-muted',
            )}>
              <Smartphone className={cn(
                'w-5 h-5',
                isIOS ? 'text-blue-600 dark:text-blue-400' : 'text-muted-foreground',
              )} />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-foreground">Connected devices</p>
              {isIOS && sourceRef.type === 'device' && sourceRef.device_name && (
                <p className="text-xs text-muted-foreground truncate mt-0.5">
                  {sourceRef.device_name} — {sourceRef.device_path}
                </p>
              )}
              {!isIOS && (
                <p className="text-xs text-muted-foreground mt-0.5">
                  iPhone, iPad, or other connected device
                </p>
              )}
            </div>
            <div className="flex items-center gap-1">
              {isIOS && (
                <button
                  type="button"
                  onClick={handleClear}
                  className="p-1 text-muted-foreground hover:text-foreground transition-colors"
                  title="Clear source"
                >
                  <XCircle className="w-4 h-4" />
                </button>
              )}
              <button
                type="button"
                onClick={() => setDeviceListExpanded(!deviceListExpanded)}
                className="p-1 text-muted-foreground hover:text-foreground transition-colors"
                title={deviceListExpanded ? 'Hide device list' : 'Show device list'}
              >
                <ChevronRight className={cn(
                  'w-4 h-4 transition-transform',
                  deviceListExpanded && 'rotate-90',
                )} />
              </button>
            </div>
          </div>

          {/* Device list — collapsible */}
          <AnimatePresence initial={false}>
            {deviceListExpanded && (
              <motion.div
                key="device-list"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: 'easeInOut' }}
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
                  const activeTier = tier2Status.data?.active_tier

                  /* Some tier is actively working, just no devices
                     connected right now — informational, not a warning */
                  if (activeTier && activeTier !== 'none') {
                    return (
                      <div className="flex items-start gap-2 bg-muted/50 border border-border rounded-lg p-2.5">
                        <Smartphone className="w-3.5 h-3.5 text-muted-foreground mt-0.5 shrink-0" />
                        <div>
                          <p className="text-xs font-medium text-foreground">No devices connected</p>
                          <p className="text-[10px] text-muted-foreground mt-0.5">
                            Connect your iPhone via USB and unlock it.
                          </p>
                        </div>
                      </div>
                    )
                  }

                  /* Tier status still loading — wait before showing any warning */
                  if (tier2Status.isLoading) {
                    return (
                      <div className="flex items-center gap-2 text-xs text-muted-foreground py-2">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        <span>Checking device access...</span>
                      </div>
                    )
                  }

                  /* No tier is working at all — determine which blocking
                     message is most actionable */
                  if (!activeTier || activeTier === 'none') {
                    /* Tier 1 is partially available (pymobiledevice3 installed)
                       just needs the Apple driver started */
                    if (iosDevices?.available && iosDevices.driver_status === 'no_driver') {
                      return (
                        <div className="text-xs text-muted-foreground py-1">
                          <DriverInstallerInline />
                        </div>
                      )
                    }

                    /* Absolutely nothing is usable */
                    return (
                      <div className="flex items-start gap-2 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg p-2.5">
                        <AlertTriangle className="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0" />
                        <div>
                          <p className="text-xs font-medium text-amber-700 dark:text-amber-300">
                            Device support unavailable
                          </p>
                          <p className="text-[10px] text-amber-600 dark:text-amber-400 mt-0.5">
                            No device backend is available. Install Apple Mobile Device
                            Support or set up the open-source WSL bridge to connect
                            your iPhone.
                          </p>
                        </div>
                      </div>
                    )
                  }

                  return null
                })()}

                <Tier2SetupPanel />
              </>
            )}

            {readyDevices.map((device) => (
              <button
                key={device.serial}
                type="button"
                onClick={() => handleSelectDevice(device)}
                className={cn(
                  'w-full flex items-center gap-2.5 p-2.5 rounded-lg transition-colors text-left',
                  isIOS && sourceRef.type === 'device' && sourceRef.device_id === device.serial
                    ? 'bg-blue-100 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800'
                    : 'bg-background hover:bg-muted border border-border',
                )}
              >
                <div className="w-8 h-8 rounded-lg bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center shrink-0">
                  <Smartphone className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-foreground truncate">{device.name}</p>
                  <p className="text-[10px] text-muted-foreground">
                    {device.model} · iOS {device.ios_version}
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  <TierBadge tier={device.active_tier} />
                  <span className="text-[9px] px-1 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded">Ready</span>
                  <ChevronRight className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                </div>
              </button>
            ))}

            {nonReadyDevices.map((device) => (
              <div
                key={device.serial}
                className="w-full flex items-center gap-2.5 p-2.5 bg-background border border-border rounded-lg opacity-60"
              >
                <div className="w-8 h-8 rounded-lg bg-muted flex items-center justify-center shrink-0">
                  <Smartphone className="w-4 h-4 text-muted-foreground" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-foreground truncate">{device.name}</p>
                  <p className="text-[10px] text-muted-foreground">
                    {device.status === 'not_trusted'
                      ? 'Unlock and tap "Trust This Computer"'
                      : device.status === 'locked'
                        ? 'Device is locked'
                        : device.status === 'error'
                          ? (device.error_detail || 'Connection error')
                          : device.model}
                  </p>
                </div>
                <span className={cn(
                  'text-[9px] px-1 py-0.5 rounded',
                  device.status === 'not_trusted'
                    ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400'
                    : device.status === 'locked'
                      ? 'bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400'
                      : 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
                )}>
                  {device.status === 'not_trusted'
                    ? 'Not Trusted'
                    : device.status === 'locked'
                      ? 'Locked'
                      : 'Error'}
                </span>
              </div>
            ))}

            {/* Prefer Tier 2 setting — advanced option to use open-source bridge */}
            <PreferTier2Toggle attention={tier2Attention}>
              {allDevices.length > 0 && <Tier2SetupPanel />}
            </PreferTier2Toggle>
              </div>
            </motion.div>
          )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// PreferTier2Toggle — advanced setting to prefer open-source bridge
// ---------------------------------------------------------------------------
function PreferTier2Toggle({ children, attention }: { children?: React.ReactNode; attention?: boolean }) {
  const { data: pref } = useDevicePreference()
  const setPref = useSetDevicePreference()
  const [expanded, setExpanded] = useState(false)

  const preferTier2 = pref?.prefer_tier2 ?? false

  const handleToggle = () => {
    setPref.mutate({ prefer_tier2: !preferTier2 })
  }

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          'w-full flex items-center justify-between px-3 py-2 text-[10px] transition-colors',
          attention
            ? 'text-amber-600 dark:text-amber-400 bg-amber-50/50 dark:bg-amber-950/20 hover:bg-amber-50 dark:hover:bg-amber-950/30'
            : 'text-muted-foreground hover:bg-muted/50',
        )}
      >
        <span className="flex items-center gap-2">
          {attention && <AlertTriangle className="w-3 h-3" />}
          <span className="font-medium uppercase tracking-wide">Advanced</span>
        </span>
        <ChevronRight className={cn(
          'w-3 h-3 transition-transform',
          expanded && 'rotate-90',
        )} />
      </button>
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: 'auto' }}
            exit={{ height: 0 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3 space-y-2">
              <label className="flex items-start gap-3 cursor-pointer group">
                <input
                  type="checkbox"
                  checked={preferTier2}
                  onChange={handleToggle}
                  disabled={setPref.isPending}
                  className="mt-0.5 h-4 w-4 rounded border-border text-primary focus:ring-ring accent-primary"
                />
                <div>
                  <span className="text-xs text-foreground">
                    Prefer open-source bridge over Apple driver
                  </span>
                  <p className="text-[10px] text-muted-foreground mt-0.5">
                    Skip Apple Mobile Device Support and use the open-source
                    WSL bridge instead. Enable this if you don't want Apple
                    software installed on your PC.
                  </p>
                </div>
              </label>
              {children && <div className="border-t border-border pt-2">{children}</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


// ---------------------------------------------------------------------------
// DriverInstallerInline — compact inline driver installer
// ---------------------------------------------------------------------------
function DriverInstallerInline() {
  const [installing, setInstalling] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleInstall = async () => {
    if (!isElectron) {
      setError('Automatic installation requires the desktop app.')
      return
    }
    setInstalling(true)
    setError(null)
    try {
      const response = await fetch('/api/ios-driver/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (!response.ok) {
        const data = await response.json()
        const detail = typeof data?.detail === 'string' ? data.detail : 'Installation failed'
        throw new Error(detail)
      }
      setInstalling(false)
    } catch (err) {
      setError(extractErrorMessage(err))
      setInstalling(false)
    }
  }

  if (installing) {
    return (
      <div className="flex items-center gap-2 text-xs text-blue-600 dark:text-blue-400 py-1">
        <Loader2 className="w-3 h-3 animate-spin" />
        <span>Installing Apple device support...</span>
      </div>
    )
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
        className="text-[10px] text-primary hover:underline font-medium"
      >
        Install now
      </button>
      {error && (
        <span className="text-[10px] text-red-500">{error}</span>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Tier2SetupPanel — step-by-step wizard for WSL2 + usbipd-win setup
// ---------------------------------------------------------------------------
function Tier2SetupPanel() {
  const tier2Status = useTier2Status()
  const preview = useTier2SetupPreview()
  const executeStep = useTier2ExecuteStep()
  const cancelSetup = useTier2Cancel()
  const resetSetup = useTier2Reset()
  const [completedSteps, setCompletedSteps] = useState<string[]>([])
  const [currentStep, setCurrentStep] = useState<string | null>(null)
  const [notificationShown, setNotificationShown] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const [errorCount, setErrorCount] = useState(0)
  const [restartNotification, setRestartNotification] = useState<{ step: Tier2StepPreview } | null>(null)
  const [elevationNotification, setElevationNotification] = useState<{ step: Tier2StepPreview } | null>(null)

  const steps = preview.data?.steps || []
  const status = tier2Status.data

  // Determine which step is next
  const nextStep = steps.find(s => !completedSteps.includes(s.step_id) && s.step_id !== currentStep)

  // Auto-start next step when current completes
  useEffect(() => {
    if (!currentStep && nextStep && preview.data) {
      // Show notification before each step
      if (!notificationShown[nextStep.step_id]) {
        setNotificationShown(prev => ({ ...prev, [nextStep.step_id]: true }))
        if (nextStep.requires_restart) {
          setRestartNotification({ step: nextStep })
          return
        }
        if (nextStep.requires_elevation) {
          setElevationNotification({ step: nextStep })
          return
        }
        // Auto-advance for non-blocking steps
        handleStartStep(nextStep)
      }
    }
  }, [currentStep, nextStep, completedSteps, notificationShown])

  const handleStartStep = async (step: Tier2StepPreview) => {
    setCurrentStep(step.step_id)
    setError(null)
    try {
      const result = await executeStep.mutateAsync({ step_id: step.step_id, confirmed: true })
      if (result.completed) {
        setCompletedSteps(prev => [...prev, step.step_id])
        setCurrentStep(null)
      } else if (result.restart_required) {
        // Need restart — show notification
        setRestartNotification({ step })
      } else if (result.error) {
        setError(result.error)
        setErrorCount(prev => prev + 1)
        setCurrentStep(null)
      }
    } catch (err) {
      setError(extractErrorMessage(err))
      setErrorCount(prev => prev + 1)
      setCurrentStep(null)
    }
  }

  const handleReset = async () => {
    try {
      await resetSetup.mutateAsync()
    } catch {
      // Error notification already handled in mutation
    }
    setCompletedSteps([])
    setCurrentStep(null)
    setError(null)
    setErrorCount(0)
    setNotificationShown({})
    setRestartNotification(null)
    setElevationNotification(null)
  }

  const handleRestartConfirm = async () => {
    setRestartNotification(null)
    // Electron restart
    if (isElectron && typeof window.electronAPI?.restartApp === 'function') {
      await window.electronAPI.restartApp()
    }
  }

  const handleRestartLater = () => {
    setRestartNotification(null)
  }

  const handleElevationConfirm = async () => {
    const step = elevationNotification?.step
    setElevationNotification(null)
    if (step) {
      handleStartStep(step)
    }
  }

  const handleElevationCancel = () => {
    setElevationNotification(null)
    setCurrentStep(null)
  }

  const handleCancel = () => {
    cancelSetup.mutate()
    setCompletedSteps([])
    setCurrentStep(null)
    setError(null)
    setErrorCount(0)
    setNotificationShown({})
  }

  // Don't show panel if bridge is already running and devices are accessible
  if (status?.bridge_running && status?.active_tier === 'tier2') {
    return null
  }

  // Don't show if no Apple devices detected at all (nothing to fall back to)
  if (!tier2Status.isLoading && !status) {
    return null
  }

  return (
    <div className="space-y-3">
      {/* Restart notification — blocks everything until user acts */}
      <AnimatePresence>
        {restartNotification && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-xl p-4 space-y-3"
          >
            <div className="flex items-start gap-2.5">
              <RefreshCw className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-semibold text-amber-700 dark:text-amber-300">
                  Restart Required
                </p>
                <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
                  {restartNotification.step.restart_description ||
                    `Restarting enables the ${restartNotification.step.title} feature. Your backup progress is saved and will resume automatically.`}
                </p>
              </div>
            </div>
            <div className="flex gap-2 ml-6">
              <button
                type="button"
                onClick={handleRestartConfirm}
                className="px-3 py-1.5 bg-amber-600 text-white rounded-lg text-xs font-medium hover:bg-amber-700 active:scale-[0.95] transition-all"
              >
                Restart now
              </button>
              <button
                type="button"
                onClick={handleRestartLater}
                className="px-3 py-1.5 bg-muted text-foreground rounded-lg text-xs font-medium hover:bg-muted/80 transition-colors"
              >
                I'll restart later
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Elevation notification */}
      <AnimatePresence>
        {elevationNotification && (
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
                  {elevationNotification.step.elevation_description ||
                    `This step requires administrator permissions: ${elevationNotification.step.title}. A Windows User Account Control dialog will appear.`}
                </p>
              </div>
            </div>
            <div className="flex gap-2 ml-6">
              <button
                type="button"
                onClick={handleElevationConfirm}
                className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 active:scale-[0.95] transition-all"
              >
                Continue
              </button>
              <button
                type="button"
                onClick={handleElevationCancel}
                className="px-3 py-1.5 bg-muted text-foreground rounded-lg text-xs font-medium hover:bg-muted/80 transition-colors"
              >
                Cancel
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Step progress */}
      {currentStep && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-muted/50 border border-border rounded-xl p-4"
        >
          <div className="flex items-center gap-2">
            <Loader2 className="w-4 h-4 text-primary animate-spin" />
            <span className="text-sm font-medium text-foreground">
              {steps.find(s => s.step_id === currentStep)?.title || 'Processing...'}
            </span>
          </div>
        </motion.div>
      )}

      {/* Error */}
      {error && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 rounded-xl p-4 space-y-3"
        >
          <div className="flex items-start gap-2.5">
            <XCircle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
            <div>
              <p className="text-sm font-semibold text-red-700 dark:text-red-300">Setup Error</p>
              <p className="text-xs text-red-600 dark:text-red-400 mt-1">{error}</p>
            </div>
          </div>
          {errorCount >= 1 && (
            <div className="flex gap-2 ml-6">
              <button
                type="button"
                onClick={handleReset}
                disabled={resetSetup.isPending}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600 text-white rounded-lg text-xs font-medium hover:bg-red-700 active:scale-[0.95] transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                {resetSetup.isPending ? 'Resetting...' : 'Reset Device Setup'}
              </button>
            </div>
          )}
        </motion.div>
      )}

      {/* Steps list — only show when preview is loading or has steps */}
      {preview.isLoading && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground py-2">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          <span>Checking device access setup...</span>
        </div>
      )}

      {!preview.isLoading && steps.length > 0 && !currentStep && completedSteps.length < steps.length && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              Device Access Setup
            </p>
            {errorCount >= 1 ? (
              <button
                type="button"
                onClick={handleReset}
                disabled={resetSetup.isPending}
                className="flex items-center gap-1 text-[10px] text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300 transition-colors disabled:opacity-50"
              >
                <RotateCcw className="w-3 h-3" />
                {resetSetup.isPending ? 'Resetting...' : 'Reset setup'}
              </button>
            ) : completedSteps.length > 0 ? (
              <button
                type="button"
                onClick={handleCancel}
                className="text-[10px] text-muted-foreground hover:text-foreground transition-colors"
              >
                Cancel setup
              </button>
            ) : null}
          </div>

          <div className="space-y-1">
            {steps.map((step, idx) => {
              const isCompleted = completedSteps.includes(step.step_id)
              const isActive = currentStep === step.step_id
              const isPending = !isCompleted && !isActive

              return (
                <div
                  key={step.step_id}
                  className={cn(
                    'flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs transition-colors',
                    isCompleted && 'bg-green-50 dark:bg-green-950/20',
                    isActive && 'bg-blue-50 dark:bg-blue-950/20',
                    isPending && 'opacity-60',
                  )}
                >
                  <div className={cn(
                    'w-5 h-5 rounded-full flex items-center justify-center shrink-0 text-[10px] font-bold',
                    isCompleted && 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400',
                    isActive && 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-400',
                    isPending && 'bg-muted text-muted-foreground',
                  )}>
                    {isCompleted ? '✓' : idx + 1}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-foreground">{step.title}</p>
                    <p className="text-[10px] text-muted-foreground truncate">{step.description}</p>
                  </div>
                  {step.requires_restart && (
                    <span className="text-[9px] px-1 py-0.5 bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 rounded shrink-0">
                      Restart
                    </span>
                  )}
                  {step.requires_elevation && (
                    <span className="text-[9px] px-1 py-0.5 bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 rounded shrink-0">
                      Admin
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Completed message */}
      {!preview.isLoading && completedSteps.length === steps.length && steps.length > 0 && !currentStep && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-green-50 dark:bg-green-950/20 border border-green-200 dark:border-green-800 rounded-xl p-3"
        >
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-green-600 dark:text-green-400" />
            <p className="text-xs font-medium text-green-700 dark:text-green-300">
              Device access setup complete
            </p>
          </div>
        </motion.div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Preflight Metrics Widget
// ---------------------------------------------------------------------------
function PreflightMetrics({
  sourceRef,
  destPath,
}: {
  sourceRef: SourceRef | null
  destPath: string
}) {
  const sourcePath = sourceRef?.type === 'local_folder' ? sourceRef.path : null
  const { data, isLoading, isError, error } = usePreflightValidate(
    sourcePath,
    destPath || null,
    sourceRef?.type === 'device' ? sourceRef : null,
  )

  const hasSource = sourceRef !== null
  const hasDest = destPath.trim().length > 0
  const showMetrics = hasSource && hasDest

  return (
    <AnimatePresence mode="wait">
      {showMetrics && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: 'auto' }}
          exit={{ opacity: 0, height: 0 }}
          transition={{ duration: 0.25, ease: 'easeInOut' }}
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
                <span>{(error as any)?.response?.data?.detail || (error as Error)?.message || 'Unable to calculate disk metrics. Check that both paths exist.'}</span>
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
                        The destination drive needs at least {formatBytes(data.source_size_bytes)}{' '}
                        but only has {formatBytes(data.dest_free_bytes)} free.
                        Free up {formatBytes(data.source_size_bytes - data.dest_free_bytes)} or
                        choose a different destination.
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
  )
}

function MetricCard({
  label,
  value,
  icon,
  isWarning,
}: {
  label: string
  value: string
  icon: React.ReactNode
  isWarning?: boolean
}) {
  return (
    <div className="text-center">
      <div className="flex items-center justify-center gap-1.5 mb-1.5 text-muted-foreground">
        {icon}
        <span className="text-xs font-medium uppercase tracking-wide">{label}</span>
      </div>
      <p
        className={cn(
          'text-lg font-semibold',
          isWarning ? 'text-red-600 dark:text-red-400' : 'text-foreground',
        )}
      >
        {value}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ModeSegmentedControl
// ---------------------------------------------------------------------------
function ModeSegmentedControl({
  value,
  onChange,
}: {
  value: TransferMode
  onChange: (mode: TransferMode) => void
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <label className="text-sm font-medium text-foreground">Transfer Mode</label>
      </div>

      <div className="flex gap-2 p-1 bg-muted rounded-full">
        <button
          type="button"
          onClick={() => onChange('copy')}
          className={cn(
            'flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-full text-sm transition-all active:scale-[0.97]',
            value === 'copy'
              ? 'bg-primary text-primary-foreground shadow-xs'
              : 'text-muted-foreground hover:text-foreground hover:bg-background/50',
          )}
        >
          <Copy className="w-4 h-4" />
          <span className="font-medium">Backup</span>
        </button>

        <button
          type="button"
          onClick={() => onChange('move')}
          className={cn(
            'flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-full text-sm transition-all active:scale-[0.97]',
            value === 'move'
              ? 'bg-primary text-primary-foreground shadow-xs'
              : 'text-muted-foreground hover:text-foreground hover:bg-background/50',
          )}
        >
          <ArrowRightLeft className="w-4 h-4" />
          <span className="font-medium">Space Saver</span>
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
          {value === 'copy' ? (
            <div className="flex items-start gap-2.5 px-4 py-3 bg-blue-50 dark:bg-blue-950/30 border border-blue-100 dark:border-blue-900 rounded-xl">
              <Shield className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-medium text-blue-700 dark:text-blue-300">
                  Backup (Copy) — Files remain fully safe on your device
                </p>
                <p className="text-xs text-blue-600/70 dark:text-blue-400/70 mt-0.5">
                  Source files are read-only copied to the destination. Nothing is deleted from
                  the original location.
                </p>
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-2.5 px-4 py-3 bg-amber-50 dark:bg-amber-950/30 border border-amber-100 dark:border-amber-900 rounded-xl">
              <AlertTriangle className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-medium text-amber-700 dark:text-amber-300">
                  Space Saver (Move) — Files removed only after two-stage verification
                </p>
                <p className="text-xs text-amber-600/70 dark:text-amber-400/70 mt-0.5">
                  Files are transferred directly. Source files are deleted only after passing
                  byte-level hash verification across both transfer stages.
                </p>
              </div>
            </div>
          )}
        </motion.div>
      </AnimatePresence>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ExtensionBadges
// ---------------------------------------------------------------------------
function ExtensionBadges({
  label,
  icon,
  extensions,
}: {
  label: string
  icon: React.ReactNode
  extensions: string[]
}) {
  const [expanded, setExpanded] = useState(false)
  const visibleCount = 8
  const hasMore = extensions.length > visibleCount
  const visible = expanded ? extensions : extensions.slice(0, visibleCount)

  return (
    <div className="flex items-start gap-2">
      <div className="w-7 h-7 rounded-lg bg-muted flex items-center justify-center shrink-0 mt-0.5">
        {icon}
      </div>
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{label}</p>
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
              className="px-1.5 py-0.5 bg-primary/10 hover:bg-primary/20 text-primary rounded-md text-[10px] font-medium transition-colors cursor-pointer"
            >
              {expanded ? 'show less' : `+${extensions.length - visibleCount}`}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// DeviceSetupPage
// ---------------------------------------------------------------------------
export default function DeviceSetupPage() {
  const sourcePath = useTransferStore((s) => s.ui.setupSourcePath)
  const destPath = useTransferStore((s) => s.ui.setupDestPath)
  const sessionName = useTransferStore((s) => s.ui.setupSessionName)
  const storeTransferMode = useTransferStore((s) => s.ui.setupTransferMode)
  const moveConfirmed = useTransferStore((s) => s.ui.setupMoveConfirmed)
  const onlyNewMode = useTransferStore((s) => s.ui.setupOnlyNewMode)

  const setSourcePath = useTransferStore((s) => s.setSetupSourcePath)
  const setDestPath = useTransferStore((s) => s.setSetupDestPath)
  const setSessionName = useTransferStore((s) => s.setSetupSessionName)
  const setStoreTransferMode = useTransferStore((s) => s.setSetupTransferMode)
  const setMoveConfirmed = useTransferStore((s) => s.setSetupMoveConfirmed)
  const setSetupOnlyNewMode = useTransferStore((s) => s.setSetupOnlyNewMode)
  const resetSetup = useTransferStore((s) => s.resetSetup)

  const defaultTransferMode = useTransferStore((s) => s.ui.defaultTransferMode)
  const setDefaultTransferMode = useTransferStore((s) => s.setDefaultTransferMode)
  const [transferMode, setTransferMode] = useState<TransferMode>(storeTransferMode || defaultTransferMode)

  const { data: config, isLoading: configLoading } = useConfig()
  const createSession = useCreateSession()
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const initTransfer = useTransferStore((s) => s.initTransfer)

  const [startError, setStartError] = useState<string | null>(null)

  // Source reference — the typed source (either local folder or device)
  const [sourceRef, setSourceRef] = useState<SourceRef | null>(() => {
    // Initialize from legacy source path in store
    if (sourcePath) {
      if (sourcePath.startsWith(IOS_SOURCE_PREFIX)) {
        const withoutPrefix = sourcePath.slice(IOS_SOURCE_PREFIX.length)
        const slashIdx = withoutPrefix.indexOf('/')
        const deviceId = slashIdx === -1 ? withoutPrefix : withoutPrefix.slice(0, slashIdx)
        // Guard: reject WPD device IDs from stored legacy paths
        if (isWPDPath(deviceId)) return null
        return {
          type: 'device',
          device_id: deviceId,
          device_path: slashIdx === -1 ? '/' : '/' + withoutPrefix.slice(slashIdx + 1),
        }
      }
      return { type: 'local_folder', path: sourcePath }
    }
    return null
  })

  // Device import state for incremental imports
  const deviceSerial = sourceRef?.type === 'device' ? sourceRef.device_id : null
  const { data: deviceImportState } = useDeviceImportState(deviceSerial)
  const clearDeviceState = useClearDeviceImportState()
  const hasDeviceState = !!deviceImportState?.last_successful_cutoff
  const isIOSDevice = sourceRef?.type === 'device'

  // Preflight validation
  const sourcePathForPreflight = sourceRef?.type === 'local_folder' ? sourceRef.path : null
  const { data: preflight } = usePreflightValidate(
    sourcePathForPreflight,
    destPath || null,
    sourceRef?.type === 'device' ? sourceRef : null,
  )

  // Derive the effective source path string for display and legacy compat
  const effectiveSourcePath = sourceRef ? sourceRefToString(sourceRef) : ''

  // Source == Destination check
  const isSamePath = effectiveSourcePath.trim().length > 0
    && destPath.trim().length > 0
    && effectiveSourcePath.trim().toLowerCase() === destPath.trim().toLowerCase()

  const hasPaths = effectiveSourcePath.trim().length > 0 && destPath.trim().length > 0
  const spaceSufficient = !preflight || preflight.is_sufficient
  const needsMoveConfirm = transferMode === 'move' && !moveConfirmed
  const canStart = hasPaths && spaceSufficient && !isSamePath && !needsMoveConfirm && !createSession.isPending

  // Sync sourceRef back to store for legacy compat
  useEffect(() => {
    setSourcePath(effectiveSourcePath)
  }, [effectiveSourcePath, setSourcePath])

  const handleTransferModeChange = useCallback((mode: TransferMode) => {
    setTransferMode(mode)
    setStoreTransferMode(mode)
    setDefaultTransferMode(mode)
    if (mode === 'copy') setMoveConfirmed(false)
  }, [setStoreTransferMode, setDefaultTransferMode, setMoveConfirmed])

  const handleStart = useCallback(async () => {
    setStartError(null)
    const name = sessionName.trim() || `backup-${Date.now()}`
    try {
      const session = await createSession.mutateAsync({
        session_name: name,
        source_root: effectiveSourcePath,
        source_ref: sourceRef ?? undefined,
        dest_root: destPath,
        transfer_mode: transferMode,
        only_new_since_last_import: isIOSDevice && onlyNewMode,
      })
      initTransfer(session)
      resetSetup()
      setCurrentPage('transfer')
    } catch (err) {
      setStartError(extractErrorMessage(err))
    }
  }, [sessionName, effectiveSourcePath, sourceRef, destPath, transferMode, isIOSDevice, onlyNewMode, createSession, initTransfer, resetSetup, setCurrentPage])

  // Reset only-new-mode when source changes away from iOS device
  useEffect(() => {
    if (!isIOSDevice && onlyNewMode) {
      setSetupOnlyNewMode(false)
    }
  }, [isIOSDevice, onlyNewMode, setSetupOnlyNewMode])

  // Esc key to go back
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setCurrentPage('dashboard')
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [setCurrentPage])

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground tracking-tight">Device Setup</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Configure source and destination for your backup
        </p>
      </div>

      {/* Source and Destination Selection */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-card border border-border rounded-xl p-6 space-y-5"
      >
        <div className="flex items-center gap-2 mb-1">
          <Settings className="w-5 h-5 text-primary" />
          <h2 className="text-base font-semibold text-foreground">Directories</h2>
        </div>

        {/* Source: Two distinct entry points */}
        <SourcePicker
          sourceRef={sourceRef}
          onSourceChange={setSourceRef}
        />

        {/* Destination: existing folder picker */}
        <div>
          <label className="text-sm font-medium text-foreground mb-1.5 block">Destination Directory</label>
          <div className="flex gap-2">
            <div className="flex-1 relative">
              <input
                type="text"
                value={destPath}
                onChange={(e) => setDestPath(e.target.value)}
                placeholder="Where to store the backup archive..."
                className={cn(
                  'w-full px-3 py-2.5 bg-background border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring transition-colors',
                  destPath.trim().length > 0
                    ? 'border-green-300 dark:border-green-700'
                    : 'border-border',
                )}
              />
              {destPath.trim().length > 0 && (
                <FolderCheck className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-green-500 dark:text-green-400" />
              )}
            </div>
            <button
              onClick={async () => {
                if (isElectron && typeof window.electronAPI?.openDirectory === 'function') {
                  const selected = await window.electronAPI.openDirectory(destPath || undefined)
                  if (selected) setDestPath(selected)
                } else {
                  const input = prompt('Enter destination path:', destPath)
                  if (input !== null) setDestPath(input)
                }
              }}
              className="no-drag px-4 py-2.5 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 active:scale-[0.95] transition-all flex items-center gap-1.5"
            >
              <FolderOpen className="w-4 h-4" />
              Browse
            </button>
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
          <label className="text-sm font-medium text-foreground mb-1.5 block">
            Session Name <span className="text-muted-foreground font-normal">(optional)</span>
          </label>
          <input
            type="text"
            value={sessionName}
            onChange={(e) => setSessionName(e.target.value)}
            placeholder="My Backup"
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
              <h2 className="text-base font-semibold text-foreground">Incremental Import</h2>
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
                        Only showing items added after {formatDate(deviceImportState.last_successful_cutoff)}
                      </p>
                      <p className="text-xs text-blue-600/70 dark:text-blue-400/70 mt-1">
                        Files already imported will be skipped. New and previously-failed files
                        will go through the full import pipeline.
                      </p>
                    </div>
                  </div>
                )}

                <div className="flex items-center justify-between pt-2 border-t border-border">
                  <div className="text-xs text-muted-foreground">
                    Last import: session #{deviceImportState.last_import_session_id ?? '—'}
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      if (deviceSerial && confirm('This will force a full re-scan on next import. Continue?')) {
                        clearDeviceState.mutate(deviceSerial)
                        setSetupOnlyNewMode(false)
                      }
                    }}
                    disabled={clearDeviceState.isPending}
                    className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <RefreshCw className={cn('w-3 h-3', clearDeviceState.isPending && 'animate-spin')} />
                    Forget last import
                  </button>
                </div>
              </>
            ) : (
              <div className="flex items-start gap-2.5 bg-muted/50 border border-border rounded-lg p-3">
                <Smartphone className="w-4 h-4 text-muted-foreground mt-0.5 shrink-0" />
                <div>
                  <p className="text-sm font-medium text-foreground">
                    First import from this device
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    No previous import history. After your first import completes,
                    you can enable incremental mode to skip already-imported items.
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
        <PreflightMetrics sourceRef={sourceRef} destPath={destPath} />
      </motion.div>

      {/* Transfer Mode Selection */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="bg-card border border-border rounded-xl p-6"
      >
        <ModeSegmentedControl value={transferMode} onChange={handleTransferModeChange} />

        <AnimatePresence>
          {transferMode === 'move' && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
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
                  I understand that source files will be{' '}
                  <strong>permanently deleted</strong> after byte-level hash verification.
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
          <div className="mt-4 pt-3 border-t border-border flex items-center gap-4 text-xs text-muted-foreground">
            <span>Batch size: {config.batch_size}</span>
            <span>Max retries: {config.max_retry}</span>
            <span>Port: {config.port}</span>
          </div>
        )}
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
          onClick={handleStart}
          title={
            canStart
              ? undefined
              : !hasPaths
                ? 'Select both source and destination directories to continue'
                : isSamePath
                  ? 'Source and destination cannot be the same'
                  : needsMoveConfirm
                    ? 'Confirm that you understand the move operation'
                    : 'Free up disk space on the destination to continue'
          }
          className={cn(
            'no-drag w-full flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl text-sm font-semibold transition-colors',
            canStart && !createSession.isPending
              ? 'bg-primary text-primary-foreground hover:bg-primary/90'
              : 'bg-muted text-muted-foreground cursor-not-allowed',
          )}
        >
          {createSession.isPending ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Creating session...
            </>
          ) : (
            <>
              Start Backup
              <ChevronRight className="w-4 h-4" />
            </>
          )}
        </motion.button>
        {startError && (
          <div className="flex items-start gap-2.5 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg p-3.5 mt-3">
            <AlertCircle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
            <p className="text-xs text-red-600 dark:text-red-400">{startError}</p>
          </div>
        )}
        {!canStart && !startError && (
          <p className="text-xs text-muted-foreground text-center mt-2">
            {!hasPaths
              ? 'Select both source and destination directories to continue'
              : isSamePath
                ? 'Source and destination cannot be the same'
                : needsMoveConfirm
                  ? 'Confirm that you understand the move operation'
                  : 'Free up disk space on the destination to continue'}
          </p>
        )}
      </div>
    </div>
  )
}
