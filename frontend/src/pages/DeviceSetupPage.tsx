// ---------------------------------------------------------------------------
// Transfera v2 — Device Setup Page
// Configuration selectors with real-time preflight disk metrics
// and explicit Backup / Space Saver mode segmented control.
// ---------------------------------------------------------------------------

import { useState, useCallback } from 'react'
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
} from 'lucide-react'
import { useConfig, useCreateSession, usePreflightValidate } from '@/lib/queries'
import { useTransferStore } from '@/store/transfer'
import { cn } from '@/lib/utils'
import type { TransferMode } from '@/types/api'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

// ---------------------------------------------------------------------------
// FolderPicker
// ---------------------------------------------------------------------------
interface FolderPickerProps {
  label: string
  value: string
  onChange: (path: string) => void
  placeholder?: string
}

function FolderPicker({ label, value, onChange, placeholder }: FolderPickerProps) {
  const handleBrowse = async () => {
    if (window.electronAPI) {
      const selected = await window.electronAPI.openDirectory(value || undefined)
      if (selected) {
        onChange(selected)
      }
    } else {
      const input = prompt(`Enter ${label} path:`, value)
      if (input !== null) onChange(input)
    }
  }

  const isValid = value.trim().length > 0

  return (
    <div>
      <label className="text-sm font-medium text-foreground mb-1.5 block">{label}</label>
      <div className="flex gap-2">
        <div className="flex-1 relative">
          <input
            type="text"
            value={value}
            readOnly
            placeholder={placeholder ?? `Select ${label.toLowerCase()}...`}
            className={cn(
              'w-full px-3 py-2.5 bg-background border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring transition-colors',
              isValid ? 'border-green-300 dark:border-green-700' : 'border-border',
            )}
          />
          {isValid && (
            <FolderCheck className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-green-500 dark:text-green-400" />
          )}
        </div>
        <button
          onClick={handleBrowse}
          className="no-drag px-4 py-2.5 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 active:scale-[0.95] transition-all flex items-center gap-1.5"
        >
          <FolderOpen className="w-4 h-4" />
          Browse
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Preflight Metrics Widget
// ---------------------------------------------------------------------------
function PreflightMetrics({
  sourcePath,
  destPath,
}: {
  sourcePath: string
  destPath: string
}) {
  const { data, isLoading, isError } = usePreflightValidate(
    sourcePath || null,
    destPath || null,
  )

  const hasSource = sourcePath.trim().length > 0
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
                <span>Analyzing source directory and destination drive...</span>
              </div>
            )}

            {isError && !isLoading && (
              <div className="flex items-center gap-3 text-sm text-muted-foreground">
                <AlertTriangle className="w-4 h-4 text-amber-500" />
                <span>Unable to calculate disk metrics. Check that both paths exist.</span>
              </div>
            )}

            {data && !isLoading && (
              <>
                {/* Metrics grid */}
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

                {/* Space sufficiency indicator */}
                {data.is_sufficient ? (
                  <div className="flex items-center gap-2 text-sm">
                    <CheckCircle2 className="w-4 h-4 text-green-500" />
                    <span className="text-green-700 dark:text-green-400">
                      Destination has sufficient space for this backup
                    </span>
                  </div>
                ) : (
                  <div className="flex items-start gap-2.5 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg p-3.5">
                    <XCircle className="w-4.5 h-4.5 text-red-500 mt-0.5 flex-shrink-0" />
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
// Matches DESIGN.md configurator-option-chip: pill shape, caption typography,
// 12px × 16px padding, rounded.pill.
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
        {/* Backup (Copy) */}
        <button
          type="button"
          onClick={() => onChange('copy')}
          className={cn(
            'flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-full text-sm transition-all active:scale-[0.97]',
            value === 'copy'
              ? 'bg-primary text-primary-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground hover:bg-background/50',
          )}
        >
          <Copy className="w-4 h-4" />
          <span className="font-medium">Backup</span>
        </button>

        {/* Space Saver (Move) */}
        <button
          type="button"
          onClick={() => onChange('move')}
          className={cn(
            'flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-full text-sm transition-all active:scale-[0.97]',
            value === 'move'
              ? 'bg-primary text-primary-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground hover:bg-background/50',
          )}
        >
          <ArrowRightLeft className="w-4 h-4" />
          <span className="font-medium">Space Saver</span>
        </button>
      </div>

      {/* Mode description pill */}
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
              <Shield className="w-4 h-4 text-blue-500 mt-0.5 flex-shrink-0" />
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
              <AlertTriangle className="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" />
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
  return (
    <div className="flex items-start gap-2">
      <div className="w-7 h-7 rounded-lg bg-muted flex items-center justify-center flex-shrink-0 mt-0.5">
        {icon}
      </div>
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{label}</p>
        <div className="flex flex-wrap gap-1 mt-1">
          {extensions.slice(0, 8).map((ext) => (
            <span
              key={ext}
              className="px-1.5 py-0.5 bg-muted rounded-md text-[10px] text-muted-foreground font-mono"
            >
              {ext}
            </span>
          ))}
          {extensions.length > 8 && (
            <span className="px-1.5 py-0.5 bg-muted rounded-md text-[10px] text-muted-foreground">
              +{extensions.length - 8}
            </span>
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
  const [sourcePath, setSourcePath] = useState('')
  const [destPath, setDestPath] = useState('')
  const [sessionName, setSessionName] = useState('')
  const [transferMode, setTransferMode] = useState<TransferMode>('copy')

  const { data: config, isLoading: configLoading } = useConfig()
  const createSession = useCreateSession()
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const initTransfer = useTransferStore((s) => s.initTransfer)

  // Preflight validation
  const { data: preflight } = usePreflightValidate(
    sourcePath || null,
    destPath || null,
  )

  const hasPaths = sourcePath.trim().length > 0 && destPath.trim().length > 0
  const spaceSufficient = !preflight || preflight.is_sufficient
  const canStart = hasPaths && spaceSufficient && !createSession.isPending

  const handleStart = useCallback(async () => {
    const name = sessionName.trim() || `backup-${Date.now()}`
    const session = await createSession.mutateAsync({
      session_name: name,
      source_root: sourcePath,
      dest_root: destPath,
      transfer_mode: transferMode,
    })
    initTransfer(session)
    setCurrentPage('transfer')
  }, [sessionName, sourcePath, destPath, transferMode, createSession, initTransfer, setCurrentPage])

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground tracking-tight">Device Setup</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Configure source and destination for your backup
        </p>
      </div>

      {/* Directory Selection */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-card border border-border rounded-xl p-6 space-y-5"
      >
        <div className="flex items-center gap-2 mb-1">
          <Settings className="w-5 h-5 text-primary" />
          <h2 className="text-base font-semibold text-foreground">Directories</h2>
        </div>

        <FolderPicker
          label="Source Directory"
          value={sourcePath}
          onChange={setSourcePath}
          placeholder="Folder containing media to back up..."
        />

        <FolderPicker
          label="Destination Directory"
          value={destPath}
          onChange={setDestPath}
          placeholder="Where to store the backup archive..."
        />

        <div>
          <label className="text-sm font-medium text-foreground mb-1.5 block">
            Session Name <span className="text-muted-foreground font-normal">(optional)</span>
          </label>
          <input
            type="text"
            value={sessionName}
            onChange={(e) => setSessionName(e.target.value)}
            placeholder="My Backup"
            className="w-full px-3 py-2.5 bg-background border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring transition-colors"
          />
        </div>
      </motion.div>

      {/* Real-time Preflight Metrics */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.05 }}
      >
        <PreflightMetrics sourcePath={sourcePath} destPath={destPath} />
      </motion.div>

      {/* Transfer Mode Selection — Segmented Control */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="bg-card border border-border rounded-xl p-6"
      >
        <ModeSegmentedControl value={transferMode} onChange={setTransferMode} />
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
      <motion.button
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        whileHover={canStart ? { scale: 1.01 } : undefined}
        whileTap={canStart ? { scale: 0.95 } : undefined}
        disabled={!canStart}
        onClick={handleStart}
        className={cn(
          'no-drag w-full flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl text-sm font-semibold transition-colors',
          canStart
            ? 'bg-primary text-primary-foreground hover:bg-primary/90'
            : 'bg-muted text-muted-foreground cursor-not-allowed',
        )}
      >
        {createSession.isPending ? (
          <div className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
        ) : (
          <>
            Start Backup
            <ChevronRight className="w-4 h-4" />
          </>
        )}
      </motion.button>
    </div>
  )
}
