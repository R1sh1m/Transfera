// ---------------------------------------------------------------------------
// MediaVault v2 — Dashboard Page
// Activity cards, historical session logs, amber alert for paused workloads.
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
  Activity,
  Zap,
  Database,
  Shield,
} from 'lucide-react'
import { useSessionList, useRecovery } from '@/lib/queries'
import { useTransferStore } from '@/store/transfer'
import { cn } from '@/lib/utils'
import type { SessionInfo, SessionStatus } from '@/types/api'

const fallbackBadge = { color: 'text-muted-foreground', bg: 'bg-muted', icon: <Clock className="w-3.5 h-3.5" /> }

const statusConfig: Record<SessionStatus, { color: string; bg: string; icon: React.ReactNode }> = {
  created:   { color: 'text-muted-foreground', bg: 'bg-muted',         icon: <Clock className="w-3.5 h-3.5" /> },
  running:   { color: 'text-blue-600',         bg: 'bg-blue-50',       icon: <Play className="w-3.5 h-3.5" /> },
  paused:    { color: 'text-amber-600',        bg: 'bg-amber-50',      icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  completed: { color: 'text-green-600',        bg: 'bg-green-50',      icon: <CheckCircle2 className="w-3.5 h-3.5" /> },
  failed:    { color: 'text-red-600',           bg: 'bg-red-50',         icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  cancelled: { color: 'text-muted-foreground', bg: 'bg-muted',         icon: <Clock className="w-3.5 h-3.5" /> },
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

function ProgressBar({ completed, total }: { completed: number; total: number }) {
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0
  return (
    <div className="w-full">
      <div className="flex justify-between text-xs text-muted-foreground mb-1">
        <span>{completed} / {total} files</span>
        <span>{pct}%</span>
      </div>
      <div className="w-full h-1.5 bg-muted rounded-full overflow-hidden">
        <motion.div
          className="h-full bg-primary rounded-full"
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.3 }}
        />
      </div>
    </div>
  )
}

function SessionCard({ session }: { session: SessionInfo }) {
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const initTransfer = useTransferStore((s) => s.initTransfer)

  const handleResume = () => {
    initTransfer(session)
    setCurrentPage('transfer')
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card border border-border rounded-lg p-4 hover:shadow-md transition-shadow"
    >
      <div className="flex items-start justify-between mb-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-foreground truncate">{session.session_name}</h3>
          <p className="text-xs text-muted-foreground mt-0.5 truncate">{session.source_root}</p>
        </div>
        <StatusBadge status={session.status} />
      </div>

      <ProgressBar completed={session.completed_items} total={session.total_items} />

      <div className="flex items-center justify-between mt-3">
        <span className="text-xs text-muted-foreground">
          {new Date(session.created_at).toLocaleDateString()}
        </span>
        {session.status === 'paused' && (
          <button
            onClick={handleResume}
            className="no-drag inline-flex items-center gap-1 px-2.5 py-1 bg-amber-500 text-white rounded text-xs font-medium hover:bg-amber-600 transition-colors"
          >
            <Play className="w-3 h-3" />
            Resume
          </button>
        )}
        {session.status === 'completed' && (
          <button
            onClick={handleResume}
            className="no-drag inline-flex items-center gap-1 px-2.5 py-1 bg-secondary text-secondary-foreground rounded text-xs font-medium hover:bg-secondary/80 transition-colors"
          >
            <ArrowRight className="w-3 h-3" />
            View
          </button>
        )}
      </div>
    </motion.div>
  )
}

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
      className="bg-amber-50 border border-amber-200 rounded-lg p-4 mb-6"
    >
      <div className="flex items-start gap-3">
        <div className="flex-shrink-0 w-8 h-8 rounded-full bg-amber-100 flex items-center justify-center">
          <AlertTriangle className="w-4 h-4 text-amber-600" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-amber-800">
            Interrupted Workloads Detected
          </h3>
          <p className="text-xs text-amber-700 mt-1">
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

const stats = [
  { icon: Database, label: 'SQLite WAL', value: 'Atomic' },
  { icon: Shield, label: 'Hashing', value: 'BLAKE3' },
  { icon: Zap, label: 'Pipeline', value: 'Two-Hop' },
  { icon: Activity, label: 'Status', value: 'Online' },
]

export default function DashboardPage() {
  const { data: sessionList, isLoading } = useSessionList(1, 20)
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Media backup overview and session management</p>
      </div>

      {/* Resume Alert */}
      <ResumeAlert />

      {/* Stats Row */}
      <div className="grid grid-cols-4 gap-3">
        {stats.map((s) => (
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

      {/* Session History */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-foreground">Recent Sessions</h2>
          {sessionList && sessionList.total > 20 && (
            <button className="text-xs text-primary hover:underline">View All</button>
          )}
        </div>

        {isLoading ? (
          <div className="grid grid-cols-2 gap-3">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="bg-card border border-border rounded-lg p-4 animate-pulse">
                <div className="h-4 bg-muted rounded w-1/2 mb-2" />
                <div className="h-3 bg-muted rounded w-3/4 mb-3" />
                <div className="h-1.5 bg-muted rounded-full" />
              </div>
            ))}
          </div>
        ) : sessionList?.sessions.length === 0 ? (
          <div className="bg-card border border-border rounded-lg p-8 text-center">
            <HardDrive className="w-10 h-10 text-muted-foreground mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">No sessions yet. Start your first backup!</p>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            {sessionList?.sessions.map((s) => (
              <SessionCard key={s.id} session={s} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
