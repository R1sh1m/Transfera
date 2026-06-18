// ---------------------------------------------------------------------------
// Transfera v2 — Dashboard Page
// Live system metrics, directory analysis, session management.
// ---------------------------------------------------------------------------

import { motion } from 'framer-motion'
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
  Shield,
  Zap,
  FileText,
} from 'lucide-react'
import { useSessionList, useRecovery, useFolderMetadata } from '@/lib/queries'
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

// ---------------------------------------------------------------------------
// Status Badge
// ---------------------------------------------------------------------------
const fallbackBadge = { color: 'text-muted-foreground', bg: 'bg-muted', icon: <Clock className="w-3.5 h-3.5" /> }

const statusConfig: Record<SessionStatus, { color: string; bg: string; icon: React.ReactNode }> = {
  created:   { color: 'text-muted-foreground', bg: 'bg-muted',               icon: <Clock className="w-3.5 h-3.5" /> },
  running:   { color: 'text-blue-600 dark:text-blue-400',   bg: 'bg-blue-50 dark:bg-blue-950',   icon: <Play className="w-3.5 h-3.5" /> },
  paused:    { color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-950', icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  completed: { color: 'text-green-600 dark:text-green-400', bg: 'bg-green-50 dark:bg-green-950', icon: <CheckCircle2 className="w-3.5 h-3.5" /> },
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
  const pausedSessions = sessionList?.sessions.filter(
    (s) => s.status === 'paused' || s.status === 'created',
  ) ?? []

  if (pausedSessions.length === 0) return null

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-lg p-4 mb-6"
    >
      <div className="flex items-start gap-3">
        <div className="flex-shrink-0 w-8 h-8 rounded-full bg-amber-100 dark:bg-amber-900 flex items-center justify-center">
          <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-200">
            Interrupted Workloads Detected
          </h3>
          <p className="text-xs text-amber-700 dark:text-amber-300 mt-1">
            {pausedSessions.length} session{pausedSessions.length > 1 ? 's' : ''} can be resumed.
          </p>
          <div className="flex gap-2 mt-2">
            <button
              onClick={() => recovery.mutate()}
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
// System Stats (static info badges)
// ---------------------------------------------------------------------------
const sysStats = [
  { icon: Database, label: 'Storage', value: 'SQLite WAL' },
  { icon: Shield, label: 'Hashing', value: 'BLAKE3' },
  { icon: Zap, label: 'Pipeline', value: 'Two-Hop' },
  { icon: Activity, label: 'Status', value: 'Online' },
]

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
    if (session.session_report_path) {
      window.electronAPI?.showItemInFolder(session.session_report_path)
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
          {session.status === 'completed' && session.session_report_path && isElectron && (
            <button
              onClick={handleViewReport}
              className="no-drag inline-flex items-center gap-1 px-2 py-1 bg-secondary text-secondary-foreground rounded text-xs font-medium hover:bg-secondary/80 transition-colors"
            >
              <FileText className="w-3 h-3" />
              Report
            </button>
          )}
          {session.status === 'completed' && (
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
        {sysStats.map((s) => (
          <div key={s.label} className="bg-card border border-border rounded-lg p-3 flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center">
              <s.icon className="w-4.5 h-4.5 text-primary" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">{s.label}</p>
              <p className="text-sm font-semibold text-foreground">{s.value}</p>
            </div>
          </div>
        ))}
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
          {sessionList && sessionList.total > 20 && (
            <button className="text-xs text-primary hover:underline">View All</button>
          )}
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
