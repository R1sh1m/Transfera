// ---------------------------------------------------------------------------
// Transfera v2 — Dashboard Page
// Live system metrics, directory analysis, session management.
// ---------------------------------------------------------------------------

import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  HardDrive,
  Play,
  Clock,
  CheckCircle2,
  AlertTriangle,
  RefreshCw,
  ArrowRight,
  Folder,
  Archive,
  Loader2,
  Copy,
  ArrowRightLeft,
  Activity,
  Database,
  FileText,
  ChevronDown,
  ChevronRight,
  Trash2,
  X,
} from 'lucide-react'
import { useSessionList, useRecovery, useFolderMetadata, useHealth, useDiskSpace, useClearSessions } from '@/lib/queries'
import { useTransferStore } from '@/store/transfer'
import { cn, isElectron } from '@/lib/utils'
import type { SessionInfo, SessionStatus } from '@/types/api'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / 1024 ** i).toFixed(i > 0 ? 1 : 0)} ${units[i]}`
}

function timeAgo(dateStr: string): string {
  const now = Date.now()
  const then = new Date(dateStr).getTime()
  const diffMs = now - then
  const seconds = Math.floor(diffMs / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(dateStr).toLocaleDateString()
}

// ---------------------------------------------------------------------------
// Status Badge
// ---------------------------------------------------------------------------
const fallbackBadge = { color: 'text-muted-foreground', bg: 'bg-muted', icon: <Clock className="w-3.5 h-3.5" /> }

const statusConfig: Record<SessionStatus, { color: string; bg: string; icon: React.ReactNode }> = {
  created:   { color: 'text-muted-foreground', bg: 'bg-muted',               icon: <Clock className="w-3.5 h-3.5" /> },
  running:   { color: 'text-blue-600 dark:text-blue-400',   bg: 'bg-blue-50 dark:bg-blue-950',   icon: <Play className="w-3.5 h-3.5" /> },
  paused:    { color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-950', icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  completed: { color: 'text-green-600 dark:text-green-400', bg: 'bg-green-50 dark:bg-green-950', icon: <CheckCircle2 className="w-3.5 h-3.5" /> },
  completed_with_errors: { color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-950', icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  failed:    { color: 'text-red-600 dark:text-red-400',     bg: 'bg-red-50 dark:bg-red-950',     icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  cancelled: { color: 'text-muted-foreground', bg: 'bg-muted',               icon: <Clock className="w-3.5 h-3.5" /> },
}

function StatusBadge({ status }: { status: SessionStatus }) {
  const c = statusConfig[status] ?? fallbackBadge
  return (
    <span className={cn('inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium', c.bg, c.color)}>
      {c.icon}
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Directory Metrics Card
// ---------------------------------------------------------------------------
interface DirMetricsCardProps {
  label: string
  sublabel: string
  icon: React.ReactNode
  iconBg: string
  path: string | null
  sessionName?: string
  transferMode?: 'copy' | 'move'
}

function DirMetricsCard({ label, sublabel, icon, iconBg, path, sessionName, transferMode }: DirMetricsCardProps) {
  const { data: metrics, isLoading } = useFolderMetadata(path)

  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center gap-3 mb-3">
        <div className={cn('w-9 h-9 rounded-lg flex items-center justify-center', iconBg)}>
          {icon}
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-foreground">{label}</p>
          <p className="text-[11px] text-muted-foreground truncate" title={path ?? undefined}>
            {path || 'No path selected'}
          </p>
        </div>
        {sessionName && (
          <span className="text-[10px] font-medium text-muted-foreground bg-muted px-1.5 py-0.5 rounded truncate max-w-[100px]" title={sessionName}>
            {sessionName}
          </span>
        )}
      </div>

      {isLoading && path ? (
        <div className="flex items-center gap-2 py-2">
          <Loader2 className="w-3.5 h-3.5 text-muted-foreground animate-spin" />
          <span className="text-xs text-muted-foreground">Analyzing...</span>
        </div>
      ) : metrics ? (
        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <div className="text-center">
              <p className="text-sm font-bold text-foreground">{metrics.size_gb} GB</p>
              <p className="text-[10px] text-muted-foreground">Total Size</p>
            </div>
            <div className="text-center">
              <p className="text-sm font-bold text-foreground">{metrics.file_count.toLocaleString()}</p>
              <p className="text-[10px] text-muted-foreground">Files</p>
            </div>
          </div>
          {transferMode && (
            <div className="flex items-center justify-center gap-1.5 pt-1 border-t border-border">
              {transferMode === 'copy' ? (
                <Copy className="w-3 h-3 text-blue-500 dark:text-blue-400" />
              ) : (
                <ArrowRightLeft className="w-3 h-3 text-amber-500 dark:text-amber-400" />
              )}
              <span className={cn(
                'text-[10px] font-medium',
                transferMode === 'copy' ? 'text-blue-600 dark:text-blue-400' : 'text-amber-600 dark:text-amber-400',
              )}>
                {transferMode === 'copy' ? 'Backup (Copy)' : 'Space Saver (Move)'}
              </span>
            </div>
          )}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground py-2">{sublabel}</p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Resume Alert
// ---------------------------------------------------------------------------
function ResumeAlert() {
  const { data: sessionList } = useSessionList(1, 100)
  const recovery = useRecovery()
  const [expanded, setExpanded] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const pausedSessions = sessionList?.sessions.filter(
    (s) => s.status === 'paused' || s.status === 'created',
  ) ?? []

  // Reset dismiss when the underlying list changes (new sessions appear on
  // a fresh page load after a prior dismiss).
  useEffect(() => {
    setDismissed(false)
  }, [pausedSessions.length])

  if (pausedSessions.length === 0 || dismissed) return null

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      className="relative bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-lg p-4 mb-6"
    >
      <button
        onClick={() => setDismissed(true)}
        className="no-drag absolute top-3 right-3 text-amber-400 hover:text-amber-600 dark:hover:text-amber-200 transition-colors"
        title="Dismiss"
      >
        <X className="w-4 h-4" />
      </button>

      <div className="flex items-start gap-3">
        <div className="shrink-0 w-8 h-8 rounded-full bg-amber-100 dark:bg-amber-900 flex items-center justify-center">
          <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-200">
            Interrupted Workloads Detected
          </h3>
          <p className="text-xs text-amber-700 dark:text-amber-300 mt-1">
            {pausedSessions.length} session{pausedSessions.length > 1 ? 's' : ''} can be resumed.
          </p>

          {/* Expandable session list */}
          <button
            onClick={() => setExpanded(!expanded)}
            className="no-drag flex items-center gap-1 mt-2 text-xs text-amber-600 dark:text-amber-400 hover:text-amber-800 dark:hover:text-amber-200 transition-colors"
          >
            {expanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
            {expanded ? 'Hide details' : 'Show details'}
          </button>

          {expanded && (
            <div className="mt-2 space-y-1.5">
              {pausedSessions.map((s) => (
                <div
                  key={s.id}
                  className="flex items-center justify-between text-xs px-2.5 py-1.5 bg-amber-100/50 dark:bg-amber-900/30 rounded"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-medium text-amber-800 dark:text-amber-200 truncate">
                      {s.session_name}
                    </span>
                    <span className={cn(
                      'text-[10px] font-mono px-1.5 py-0.5 rounded',
                      s.transfer_mode === 'copy'
                        ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300'
                        : 'bg-amber-100 dark:bg-amber-800 text-amber-700 dark:text-amber-300',
                    )}>
                      {s.transfer_mode === 'copy' ? 'COPY' : 'MOVE'}
                    </span>
                  </div>
                  <span className="text-amber-600 dark:text-amber-400 ml-2 shrink-0">
                    {s.total_items} items
                  </span>
                </div>
              ))}
            </div>
          )}

          <div className="flex gap-2 mt-2">
            <button
              onClick={() => {
                recovery.mutate(undefined, {
                  onSuccess: () => setDismissed(true),
                })
              }}
              disabled={recovery.isPending}
              className="no-drag inline-flex items-center gap-1 px-3 py-1.5 bg-amber-500 text-white rounded text-xs font-medium hover:bg-amber-600 transition-colors disabled:opacity-50"
            >
              <RefreshCw className={cn('w-3 h-3', recovery.isPending && 'animate-spin')} />
              Recover All
            </button>
          </div>
        </div>
      </div>
    </motion.div>
  )
}

// ---------------------------------------------------------------------------
// Confirm Dialog
// ---------------------------------------------------------------------------
function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  onConfirm,
  onCancel,
  loading,
}: {
  open: boolean
  title: string
  description: string
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  loading: boolean
}) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center"
        >
          <div className="fixed inset-0 bg-black/50" onClick={onCancel} />
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            className="relative bg-card border border-border rounded-lg p-6 max-w-md w-full mx-4 shadow-lg"
          >
            <h3 className="text-lg font-semibold text-foreground mb-2">{title}</h3>
            <p className="text-sm text-muted-foreground mb-6 whitespace-pre-line">{description}</p>
            <div className="flex justify-end gap-3">
              <button
                onClick={onCancel}
                disabled={loading}
                className="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors rounded-md hover:bg-muted disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={onConfirm}
                disabled={loading}
                className="px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-md transition-colors disabled:opacity-50 inline-flex items-center gap-2"
              >
                {loading && <Loader2 className="w-4 h-4 animate-spin" />}
                {confirmLabel}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ---------------------------------------------------------------------------
// Clear Sessions Button
// ---------------------------------------------------------------------------
function ClearSessionsButton({ sessionCount }: { sessionCount: number }) {
  const [showDialog, setShowDialog] = useState(false)
  const clearSessions = useClearSessions()

  const handleConfirm = () => {
    clearSessions.mutate(undefined, {
      onSettled: () => setShowDialog(false),
    })
  }

  if (sessionCount === 0) return null

  return (
    <>
      <button
        onClick={() => setShowDialog(true)}
        className="no-drag inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-950 border border-input rounded-md transition-colors"
        title="Clear session history"
      >
        <Trash2 className="w-3.5 h-3.5" />
        Clear Sessions
      </button>
      <ConfirmDialog
        open={showDialog}
        title="Clear Session History"
        description={`This will permanently remove all ${sessionCount} session(s), their batches, library items, and generated thumbnails from the app.\n\nThis only clears app records — your actual files at the transfer destination are not affected.\n\nThis action cannot be undone.`}
        confirmLabel="Clear All Sessions"
        onConfirm={handleConfirm}
        onCancel={() => setShowDialog(false)}
        loading={clearSessions.isPending}
      />
    </>
  )
}

// ---------------------------------------------------------------------------
// Backend Status Card
// ---------------------------------------------------------------------------
function BackendStatusCard() {
  const { data: health, isLoading: healthLoading, isError: healthError } = useHealth()
  const wsConnected = useTransferStore((s) => s.wsConnected)
  const sessionId = useTransferStore((s) => s.transfer.sessionId)

  const restOnline = !healthLoading && !healthError && health?.status === 'ok'
  const restColor = healthLoading
    ? 'bg-muted'
    : restOnline
      ? 'bg-green-500'
      : 'bg-red-500'
  const wsColor = wsConnected
    ? 'bg-green-500'
    : sessionId !== null
      ? 'bg-red-500'
      : 'bg-muted-foreground/40'
  const wsTooltip = wsConnected
    ? 'WebSocket connected'
    : sessionId !== null
      ? 'WebSocket disconnected'
      : 'No active transfer'

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <Activity className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Backend Status</p>
        <div className="flex items-center gap-3 mt-1">
          <div className="flex items-center gap-1.5">
            <span className={cn('w-2 h-2 rounded-full', restColor)} />
            <span className="text-[11px] text-muted-foreground">REST</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className={cn('w-2 h-2 rounded-full', wsColor)} title={wsTooltip} />
            <span className="text-[11px] text-muted-foreground">WS</span>
          </div>
        </div>
      </div>
      {health?.version && (
        <span className="text-[10px] font-medium text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
          v{health.version}
        </span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Aggregate Stats Card
// ---------------------------------------------------------------------------
function AggregateStatsCard({ sessions }: { sessions: SessionInfo[] }) {
  const totalSessions = sessions.length
  const totalFiles = sessions.reduce((sum, s) => sum + s.completed_items, 0)
  const totalVolume = sessions.reduce((sum, s) => sum + (s.total_bytes_volume ?? 0), 0)
  const activeCount = sessions.filter(
    (s) => s.status === 'running' || s.status === 'paused',
  ).length

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <Database className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Aggregate Stats</p>
        <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 mt-1">
          <span className="text-[11px] text-muted-foreground">{totalSessions} sessions</span>
          <span className="text-[11px] text-muted-foreground">{totalFiles.toLocaleString()} files</span>
          <span className="text-[11px] text-muted-foreground">{formatBytes(totalVolume)} vol.</span>
          <span className="text-[11px] text-muted-foreground">
            {activeCount > 0 ? (
              <span className="text-blue-600 dark:text-blue-400">{activeCount} active</span>
            ) : (
              'No active'
            )}
          </span>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Storage Health Card
// ---------------------------------------------------------------------------
function StorageHealthCard({ destPath }: { destPath: string | null }) {
  const { data: diskSpace, isLoading } = useDiskSpace(destPath)

  const freePct = diskSpace
    ? Math.round((diskSpace.free_bytes / diskSpace.total_bytes) * 100)
    : null

  const healthColor = !diskSpace
    ? 'text-muted-foreground'
    : freePct !== null && freePct < 10
      ? 'text-red-600 dark:text-red-400'
      : freePct !== null && freePct < 25
        ? 'text-amber-600 dark:text-amber-400'
        : 'text-green-600 dark:text-green-400'

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <HardDrive className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Storage Health</p>
        {isLoading && destPath ? (
          <div className="flex items-center gap-1.5 mt-1">
            <Loader2 className="w-3 h-3 text-muted-foreground animate-spin" />
            <span className="text-[11px] text-muted-foreground">Checking...</span>
          </div>
        ) : diskSpace ? (
          <div className="mt-1">
            <p className={cn('text-sm font-semibold', healthColor)}>
              {formatBytes(diskSpace.free_bytes)} free
            </p>
            <p className="text-[10px] text-muted-foreground">
              of {formatBytes(diskSpace.total_bytes)} total ({freePct}% free)
            </p>
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground mt-1">
            {destPath ? 'Unable to read disk' : 'No destination set'}
          </p>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Last Backup Card
// ---------------------------------------------------------------------------
function LastBackupCard({ sessions }: { sessions: SessionInfo[] }) {
  const lastCompleted = sessions.find((s) => s.status === 'completed')

  return (
    <div className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
        <CheckCircle2 className="w-4.5 h-4.5 text-primary" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Last Backup</p>
        {lastCompleted ? (
          <div className="mt-1">
            <p className="text-sm font-semibold text-foreground truncate" title={lastCompleted.session_name}>
              {lastCompleted.session_name}
            </p>
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-muted-foreground">
                {lastCompleted.completed_at ? timeAgo(lastCompleted.completed_at) : 'unknown'}
              </span>
              {lastCompleted.failed_items > 0 && (
                <span className="text-[10px] font-medium text-amber-600 dark:text-amber-400">
                  {lastCompleted.failed_items} failed
                </span>
              )}
            </div>
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground mt-1">No backups yet</p>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Session Table Row
// ---------------------------------------------------------------------------
function SessionRow({ session }: { session: SessionInfo }) {
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const initTransfer = useTransferStore((s) => s.initTransfer)

  const handleResume = () => {
    initTransfer(session)
    setCurrentPage('transfer')
  }

  const handleViewReport = () => {
    if (!session.session_report_path) return
    if (isElectron && window.electronAPI?.openPath) {
      window.electronAPI.openPath(session.session_report_path)
    } else {
      window.open(`/api/sessions/${session.id}/report?fmt=html`, '_blank')
    }
  }

  return (
    <tr className="border-b border-border hover:bg-muted/30 transition-colors">
      <td className="py-2.5 pr-3">
        <StatusBadge status={session.status} />
      </td>
      <td className="py-2.5 pr-3">
        <p className="text-sm font-medium text-foreground truncate max-w-[180px]" title={session.session_name}>
          {session.session_name}
        </p>
      </td>
      <td className="py-2.5 pr-3">
        <p className="text-xs text-muted-foreground truncate max-w-[220px]" title={session.source_root}>
          {session.source_root}
        </p>
      </td>
      <td className="py-2.5 pr-3">
        <p className="text-xs text-muted-foreground truncate max-w-[220px]" title={session.dest_root}>
          {session.dest_root}
        </p>
      </td>
      <td className="py-2.5 pr-3">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {session.completed_items.toLocaleString()} / {session.total_items.toLocaleString()}
          </span>
          {session.total_bytes_volume != null && session.total_bytes_volume > 0 && (
            <span className="text-[10px] font-medium text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
              {formatBytes(session.total_bytes_volume)}
            </span>
          )}
        </div>
      </td>
      <td className="py-2.5 pr-3">
        <span className="text-xs text-muted-foreground">
          {new Date(session.created_at).toLocaleDateString()}
        </span>
      </td>
      <td className="py-2.5">
        <div className="flex items-center gap-1.5">
          {session.status === 'paused' && (
            <button
              onClick={handleResume}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-amber-500 text-white rounded text-xs font-medium hover:bg-amber-600 transition-colors"
            >
              <Play className="w-3 h-3" />
              Resume
            </button>
          )}
          {['completed', 'completed_with_errors', 'failed'].includes(session.status) && session.session_report_path && (
            <button
              onClick={handleViewReport}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-medium hover:bg-secondary/80 transition-colors"
              title="Open HTML report"
            >
              <FileText className="w-3 h-3" />
              Report
            </button>
          )}
          {['failed', 'cancelled', 'completed_with_errors'].includes(session.status) && !session.session_report_path && (
            <span className="text-[10px] text-muted-foreground italic" title="Report not generated — session did not complete">
              No report
            </span>
          )}
          {['completed', 'completed_with_errors', 'failed'].includes(session.status) && (
            <button
              onClick={handleResume}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-medium hover:bg-secondary/80 transition-colors"
            >
              <ArrowRight className="w-3 h-3" />
              View
            </button>
          )}
        </div>
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------
export default function DashboardPage() {
  const { data: sessionList, isLoading } = useSessionList(1, 20)
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const sourceRoot = useTransferStore((s) => s.transfer.sourceRoot)
  const destRoot = useTransferStore((s) => s.transfer.destRoot)

  const latestSession = sessionList?.sessions[0]
  const activeSource = sourceRoot || latestSession?.source_root || null
  const activeDest = destRoot || latestSession?.dest_root || null
  const activeSessionName = latestSession?.session_name
  const activeTransferMode = latestSession?.transfer_mode

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Live system metrics and session management</p>
      </div>

      {/* Resume Alert */}
      <ResumeAlert />

      {/* Directory Metrics */}
      <div className="grid grid-cols-2 gap-3">
        <DirMetricsCard
          label="Source Directory"
          sublabel="Select a source path to analyze"
          icon={<Folder className="w-4.5 h-4.5 text-blue-600 dark:text-blue-400" />}
          iconBg="bg-blue-50 dark:bg-blue-950"
          path={activeSource}
          sessionName={activeSessionName}
          transferMode={activeTransferMode}
        />
        <DirMetricsCard
          label="Backup Destination"
          sublabel="Select a destination path to analyze"
          icon={<Archive className="w-4.5 h-4.5 text-green-600 dark:text-green-400" />}
          iconBg="bg-green-50 dark:bg-green-950"
          path={activeDest}
          sessionName={activeSessionName}
          transferMode={activeTransferMode}
        />
      </div>

      {/* System Stats */}
      <div className="grid grid-cols-4 gap-3">
        <BackendStatusCard />
        <AggregateStatsCard sessions={sessionList?.sessions ?? []} />
        <StorageHealthCard destPath={activeDest} />
        <LastBackupCard sessions={sessionList?.sessions ?? []} />
      </div>

      {/* Quick Start */}
      <motion.button
        whileHover={{ scale: 1.01 }}
        whileTap={{ scale: 0.99 }}
        onClick={() => setCurrentPage('setup')}
        className="no-drag w-full bg-primary text-primary-foreground rounded-lg p-4 flex items-center justify-between hover:bg-primary/90 transition-colors"
      >
        <div className="flex items-center gap-3">
          <HardDrive className="w-5 h-5" />
          <div className="text-left">
            <p className="text-sm font-semibold">Start New Backup</p>
            <p className="text-xs opacity-80">Select source and destination directories</p>
          </div>
        </div>
        <ArrowRight className="w-5 h-5" />
      </motion.button>

      {/* Session History Table */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-foreground">Recent Sessions</h2>
          <div className="flex items-center gap-2">
            {sessionList && sessionList.total > 20 && (
              <button className="text-xs text-primary hover:underline">View All</button>
            )}
            {sessionList && <ClearSessionsButton sessionCount={sessionList.total} />}
          </div>
        </div>

        {isLoading ? (
          <div className="bg-card border border-border rounded-lg p-6 space-y-3">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="flex items-center gap-4 animate-pulse">
                <div className="h-5 w-16 bg-muted rounded-full" />
                <div className="h-4 bg-muted rounded w-32" />
                <div className="h-4 bg-muted rounded w-48" />
                <div className="h-4 bg-muted rounded w-48" />
                <div className="h-4 bg-muted rounded w-20" />
                <div className="h-4 bg-muted rounded w-16" />
              </div>
            ))}
          </div>
        ) : sessionList?.sessions.length === 0 ? (
          <div className="bg-card border border-border rounded-lg p-8 text-center">
            <HardDrive className="w-10 h-10 text-muted-foreground mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">No sessions yet. Start your first backup!</p>
          </div>
        ) : (
          <div className="bg-card border border-border rounded-lg overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border bg-muted/30">
                  <th className="text-left text-xs font-medium text-muted-foreground py-2 px-4 pr-3 w-[100px]">Status</th>
                  <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3 w-[180px]">Session</th>
                  <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Source</th>
                  <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Destination</th>
                  <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3 w-[160px]">Progress</th>
                  <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3 w-[90px]">Date</th>
                  <th className="text-left text-xs font-medium text-muted-foreground py-2 w-[120px]">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sessionList?.sessions.map((s) => (
                  <SessionRow key={s.id} session={s} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
