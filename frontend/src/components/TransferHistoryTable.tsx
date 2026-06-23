// ---------------------------------------------------------------------------
// Transfera v2 — Transfer History Table
// Browsable list of past sessions on the Library page.
// ---------------------------------------------------------------------------

import { useState } from 'react'
import { History, ChevronLeft, ChevronRight, Loader2, AlertTriangle } from 'lucide-react'
import { useSessionList } from '@/lib/queries'
import { StatusBadge } from '@/pages/DashboardPage'
import { cn, isElectron } from '@/lib/utils'
import type { SessionInfo } from '@/types/api'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return '—'
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function formatDate(iso: string | undefined): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
  } catch {
    return '—'
  }
}

function truncatePath(path: string, maxLen = 40): string {
  if (path.length <= maxLen) return path
  const sep = path.includes('\\') ? '\\' : '/'
  const parts = path.split(sep)
  if (parts.length > 2) {
    const head = parts[0] ?? ''
    const tail = parts.slice(-2).join(sep)
    const available = maxLen - 3 - head.length
    if (available > tail.length) return head + sep + '...' + sep + tail
  }
  return '...' + path.slice(-maxLen + 3)
}

function formatDuration(startedAt: string | undefined, completedAt: string | undefined): string {
  if (!startedAt || !completedAt) return '—'
  try {
    const diff = (new Date(completedAt).getTime() - new Date(startedAt).getTime()) / 1000
    if (diff < 0) return '—'
    const hours = Math.floor(diff / 3600)
    const minutes = Math.floor((diff % 3600) / 60)
    const seconds = Math.floor(diff % 60)
    if (hours > 0) return `${hours}h ${minutes}m`
    if (minutes > 0) return `${minutes}m ${seconds}s`
    return `${seconds}s`
  } catch {
    return '—'
  }
}

function handleOpenReport(session: SessionInfo) {
  if (!session.session_report_path) return
  if (isElectron && window.electronAPI?.openPath) {
    window.electronAPI.openPath(session.session_report_path)
  } else {
    window.open(`/api/sessions/${session.id}/report?fmt=html`, '_blank')
  }
}

// ---------------------------------------------------------------------------
// TransferHistoryTable
// ---------------------------------------------------------------------------
const PAGE_SIZE = 50

export default function TransferHistoryTable() {
  const [page, setPage] = useState(1)

  const { data, isLoading } = useSessionList(page, PAGE_SIZE)

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-6 h-6 text-muted-foreground animate-spin" />
      </div>
    )
  }

  const sessions = data?.sessions ?? []
  const total = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  if (sessions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
        <History className="w-12 h-12 mb-3 opacity-30" />
        <p className="text-sm">No transfer history yet</p>
        <p className="text-xs mt-1">Complete a transfer to see it listed here</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border">
              <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Date</th>
              <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Source</th>
              <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Destination</th>
              <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Files</th>
              <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Size</th>
              <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Duration</th>
              <th className="text-left text-xs font-medium text-muted-foreground py-2 pr-3">Status</th>
              <th className="text-left text-xs font-medium text-muted-foreground py-2">Failures</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((session) => (
              <tr
                key={session.id}
                onClick={() => handleOpenReport(session)}
                className={cn(
                  'border-b border-border transition-colors',
                  session.session_report_path
                    ? 'cursor-pointer hover:bg-muted/50'
                    : 'cursor-default',
                )}
                title={session.session_report_path ? 'Open report' : undefined}
              >
                <td className="py-2.5 pr-3 whitespace-nowrap">
                  <span className="text-xs text-muted-foreground">
                    {formatDate(session.started_at || session.created_at)}
                  </span>
                </td>
                <td className="py-2.5 pr-3 max-w-[180px]">
                  <p className="text-xs text-foreground truncate" title={session.source_root}>
                    {truncatePath(session.source_root)}
                  </p>
                </td>
                <td className="py-2.5 pr-3 max-w-[180px]">
                  <p className="text-xs text-foreground truncate" title={session.dest_root}>
                    {truncatePath(session.dest_root)}
                  </p>
                </td>
                <td className="py-2.5 pr-3 whitespace-nowrap">
                  <span className="text-xs text-foreground">
                    {session.completed_items}/{session.total_items}
                  </span>
                </td>
                <td className="py-2.5 pr-3 whitespace-nowrap">
                  <span className="text-xs text-foreground">
                    {formatBytes(session.total_bytes_volume)}
                  </span>
                </td>
                <td className="py-2.5 pr-3 whitespace-nowrap">
                  <span className="text-xs text-foreground">
                    {formatDuration(session.started_at, session.completed_at)}
                  </span>
                </td>
                <td className="py-2.5 pr-3">
                  <StatusBadge status={session.status} />
                </td>
                <td className="py-2.5 whitespace-nowrap">
                  {session.failed_items > 0 ? (
                    <span className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
                      <AlertTriangle className="w-3 h-3" />
                      {session.failed_items}
                    </span>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between pt-2">
        <p className="text-xs text-muted-foreground">
          {total} session{total !== 1 ? 's' : ''}
        </p>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="text-xs text-muted-foreground">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  )
}
