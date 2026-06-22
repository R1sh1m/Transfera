// ---------------------------------------------------------------------------
// Transfera v2 — Transfer Page
// Split layout: PreviewPanel (media thumbnails) + TransferMonitor (stats).
// Polling-based: REST is the source of truth, WebSocket is a bonus.
// ---------------------------------------------------------------------------

import { useEffect, useState, useRef, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Play,
  Pause,
  X,
  ArrowLeft,
  Image,
  Film,
  Music,
  FileText,
  Clock,
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  Loader2,
  Zap,
  HardDrive,
} from 'lucide-react'
import { useSession, useStartSession, usePauseSession, useCancelSession, useSessionProgress, useSessionBatches, useMediaList } from '@/lib/queries'
import { useTransferStore } from '@/store/transfer'
import { useTransferWs } from '@/hooks/use-transfer-ws'
import { cn } from '@/lib/utils'
import type { SessionProgress, RecentItemProgress } from '@/types/api'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fileIcon(name: string) {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  if (['jpg', 'jpeg', 'png', 'gif', 'heic', 'raw', 'tiff', 'webp', 'bmp', 'svg'].includes(ext))
    return <Image className="w-5 h-5 text-blue-400" />
  if (['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', '3gp'].includes(ext))
    return <Film className="w-5 h-5 text-purple-400" />
  if (['mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a', 'wma'].includes(ext))
    return <Music className="w-5 h-5 text-green-400" />
  if (['pdf', 'doc', 'docx', 'txt', 'md', 'rtf'].includes(ext))
    return <FileText className="w-5 h-5 text-orange-400" />
  return <FileText className="w-5 h-5 text-muted-foreground" />
}

function formatEta(ms: number | null) {
  if (ms === null || ms <= 0) return '--:--'
  const sec = Math.floor(ms / 1000)
  const min = Math.floor(sec / 60)
  const s = sec % 60
  return `${min}:${String(s).padStart(2, '0')}`
}

function formatElapsed(ms: number) {
  const totalSec = Math.floor(ms / 1000)
  const h = Math.floor(totalSec / 3600)
  const min = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  if (h > 0) return `${h}:${String(min).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${min}:${String(s).padStart(2, '0')}`
}

// ---------------------------------------------------------------------------
// PreviewThumbnail — Reusable thumbnail component
// ---------------------------------------------------------------------------
function PreviewThumbnail({ itemId, name, thumbnailUrl: _ }: { itemId: number | null; name: string; thumbnailUrl?: string | null }) {
  const MAX_RETRIES = 2
  const [thumbState, setThumbState] = useState<'loading' | 'loaded' | 'failed'>('loading')
  const [retryKey, setRetryKey] = useState(0)
  const [retryCount, setRetryCount] = useState(0)

  const thumbUrl = itemId != null ? `/api/media/${itemId}/thumbnail?r=${retryKey}` : null

  useEffect(() => {
    setThumbState('loading')
    setRetryCount(0)
  }, [itemId])

  if (!itemId) {
    return (
      <div className="w-full h-full flex items-center justify-center">
        {fileIcon(name)}
      </div>
    )
  }

  return (
    <>
      {thumbState === 'loading' && (
        <div className="w-full h-20 animate-pulse bg-muted" />
      )}
      {thumbState !== 'failed' && (
        <img
          src={thumbUrl!}
          alt={name}
          className={cn(
            'w-full h-auto block',
            thumbState === 'loading' && 'hidden',
          )}
          onLoad={() => setThumbState('loaded')}
          onError={() => {
            if (retryCount < MAX_RETRIES) {
              setRetryCount(c => c + 1)
              setTimeout(() => setRetryKey(k => k + 1), 500 * (retryCount + 1))
              setThumbState('loading')
            } else {
              setThumbState('failed')
            }
          }}
        />
      )}
      {thumbState === 'failed' && (
        <div className="w-full h-full flex items-center justify-center opacity-50">
          {fileIcon(name)}
        </div>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// PreviewPanel — Live file transfer preview from polling data
// Items are accumulated keyed by item_id so thumbnails never disappear
// once they appear — polls only add new entries to the view.
// ---------------------------------------------------------------------------
function PreviewPanel({ progress }: { progress: SessionProgress | undefined }) {
  const status = useTransferStore((s) => s.transfer.status)
  const completedItems = progress?.completed_items ?? 0
  const totalItems = progress?.total_items ?? 0
  const isRunning = status === 'running'

  // Accumulate items so they stay visible once they appear.
  // Polls return the N most-recently-updated items with thumbnails,
  // which means earlier items silently fall out of the server response.
  // By accumulating in a Map keyed on item_id we preserve every item
  // that has ever been reported — new polls only ADD entries.
  const [itemMap, setItemMap] = useState<Map<number, RecentItemProgress>>(new Map())

  useEffect(() => {
    if (!progress) {
      setItemMap(new Map())
      return
    }
    if (!progress.recent_items?.length) return
    setItemMap(prev => {
      const next = new Map(prev)
      for (const item of progress.recent_items) {
        next.set(item.item_id, item)
      }
      return next
    })
  }, [progress])

  // Sort by most recently updated so the current/latest item is first
  const recentFiles = useMemo(() => {
    return [...itemMap.values()]
      .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
      .slice(0, 60)
  }, [itemMap])

  return (
    <div className="flex-1 bg-card border border-border rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
          <HardDrive className="w-4 h-4 text-primary" />
          Media Preview
        </h3>
        <span className="text-xs text-muted-foreground">
          {completedItems} / {totalItems} files
        </span>
      </div>

      <div className="p-4 h-[calc(100%-48px)] overflow-y-auto">
        {recentFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3">
            {isRunning ? (
              <>
                <Loader2 className="w-8 h-8 animate-spin opacity-40" />
                <p className="text-xs">Processing files...</p>
              </>
            ) : (
              <>
                <Image className="w-10 h-10 opacity-20" />
                <p className="text-xs">No preview available</p>
              </>
            )}
          </div>
        ) : (
          <div className="columns-3 gap-2">
            <AnimatePresence>
              {recentFiles.map((file, i) => {
                const opacity = i === 0 ? 1.0 : i < 3 ? 0.90 : i < 9 ? 0.75 : 0.55
                return (
                  <motion.div
                    key={file.item_id}
                    initial={{ opacity: 0, scale: 0.8 }}
                    animate={{ opacity, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.8 }}
                    transition={{ duration: 0.2, delay: i * 0.03 }}
                    className={cn(
                      'break-inside-avoid mb-2 rounded-md overflow-hidden relative transition-colors',
                      i === 0 ? 'bg-primary/10 ring-2 ring-primary' : 'bg-muted hover:bg-muted/80',
                    )}
                  >
                    <PreviewThumbnail itemId={file.item_id} name={file.file_name} thumbnailUrl={file.thumbnail_url} />
                    <span className="text-[10px] text-muted-foreground truncate w-full text-center px-1 py-0.5 block">
                      {file.file_name}
                    </span>
                  </motion.div>
                )
              })}
            </AnimatePresence>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// TransferMonitor — Performance stats and progress (polling-based)
// ---------------------------------------------------------------------------
function TransferMonitor(_props: { progress: SessionProgress | undefined }) {
  const transfer = useTransferStore((s) => s.transfer)
  const wsConnected = useTransferStore((s) => s.wsConnected)
  const { sessionId } = transfer
  const { data: batches } = useSessionBatches(sessionId)
  const [, setTick] = useState(0)

  // Fetch failed items
  const { data: failedItemsData } = useMediaList({
    sessionId: sessionId ?? undefined,
    finalStatus: 'failed',
    pageSize: 100,
  })

  // Re-render every second while transfer is active to update elapsed/speed/ETA
  useEffect(() => {
    if (transfer.status !== 'running' && transfer.status !== 'paused') return
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [transfer.status])

  const activeBatch = transfer.activeBatch
  const overallPct = transfer.totalItems > 0
    ? Math.round((transfer.completedItems / transfer.totalItems) * 100)
    : 0

  // Compute elapsed and speed — use fixed completion time for terminal states
  const isTerminal = ['completed', 'completed_with_errors', 'failed', 'cancelled'].includes(transfer.status)
  const now = Date.now()
  const elapsedMs = transfer.startedAt
    ? isTerminal && transfer.completedAt
      ? transfer.completedAt - transfer.startedAt
      : now - transfer.startedAt
    : 0
  const speed = elapsedMs > 0 ? transfer.completedItems / (elapsedMs / 1000) : 0
  const remaining = transfer.totalItems - transfer.completedItems
  const etaMs = isTerminal ? null : (speed > 0 ? (remaining / speed) * 1000 : null)

  // Determine phase text
  let phaseText = ''
  if (transfer.status === 'created') phaseText = 'Ready to start'
  else if (transfer.status === 'running' && activeBatch) {
    if (activeBatch.hop1Status === 'transferring') phaseText = `Caching file ${activeBatch.completedItems} of ${activeBatch.totalItems}`
    else if (activeBatch.hop1Status === 'completed' && activeBatch.hop2Status !== 'completed') phaseText = `Importing file ${activeBatch.completedItems} of ${activeBatch.totalItems}`
    else phaseText = `Processing batch ${activeBatch.batchNumber}...`
  }
  else if (transfer.status === 'running') phaseText = 'Processing...'
  else if (transfer.status === 'paused') phaseText = 'Paused'
  else if (transfer.status === 'completed') phaseText = 'Transfer complete'
  else if (transfer.status === 'completed_with_errors') phaseText = 'Transfer completed with errors'
  else if (transfer.status === 'failed') phaseText = 'Transfer failed'
  else if (transfer.status === 'cancelled') phaseText = 'Transfer cancelled'
  else phaseText = transfer.status

  return (
    <div className="w-80 bg-card border border-border rounded-lg flex flex-col">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
          <Zap className="w-4 h-4 text-primary" />
          Transfer Monitor
          {!wsConnected && transfer.status === 'running' && (
            <span className="ml-auto text-[10px] text-muted-foreground">(polling)</span>
          )}
        </h3>
      </div>

      <div className="flex-1 p-4 space-y-4 overflow-y-auto">
        {/* Overall Progress */}
        <div>
          <div className="flex justify-between text-xs text-muted-foreground mb-1">
            <span>Overall Progress</span>
            <span className="font-medium text-foreground">{overallPct}%</span>
          </div>
          <div className="w-full h-3 bg-muted rounded-full overflow-hidden">
            <motion.div
              className={cn(
                'h-full rounded-full transition-colors',
                transfer.status === 'completed' ? 'bg-green-500' :
                transfer.status === 'completed_with_errors' ? 'bg-amber-500' :
                transfer.status === 'failed' ? 'bg-red-500' :
                'bg-primary',
              )}
              initial={{ width: 0 }}
              animate={{ width: `${overallPct}%` }}
              transition={{ duration: 0.3 }}
            />
          </div>
          <div className="flex justify-between text-[11px] text-muted-foreground mt-1">
            <span>{transfer.completedItems} / {transfer.totalItems}</span>
            <span>{transfer.failedItems} failed</span>
          </div>
        </div>

        {/* Phase / Status */}
        <div className="p-3 bg-muted/50 rounded-md">
          <p className="text-xs font-medium text-foreground">{phaseText}</p>
          {transfer.currentFileName && (
            <p className="text-[11px] text-muted-foreground mt-1 truncate">
              {transfer.currentFileName}
            </p>
          )}
        </div>

        {/* Hop Progress */}
        {activeBatch && (
          <div className="space-y-3">
            <div className="p-3 bg-muted/50 rounded-md">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-foreground">Hop 1: Source {'->'} Cache</span>
                <span className="text-xs text-muted-foreground">{activeBatch.hop1Progress}%</span>
              </div>
              <div className="w-full h-1.5 bg-muted rounded-full overflow-hidden">
                <motion.div
                  className="h-full bg-blue-500 rounded-full"
                  animate={{ width: `${activeBatch.hop1Progress}%` }}
                />
              </div>
            </div>

            <div className="p-3 bg-muted/50 rounded-md">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-foreground">Hop 2: Cache {'->'} Archive</span>
                <span className="text-xs text-muted-foreground">{activeBatch.hop2Progress}%</span>
              </div>
              <div className="w-full h-1.5 bg-muted rounded-full overflow-hidden">
                <motion.div
                  className="h-full bg-green-500 rounded-full"
                  animate={{ width: `${activeBatch.hop2Progress}%` }}
                />
              </div>
            </div>
          </div>
        )}

        {/* Stats */}
        <div className="grid grid-cols-2 gap-2">
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Speed</p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {speed > 0 ? `${speed.toFixed(1)} f/s` : '--'}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">ETA</p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {formatEta(etaMs)}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Elapsed</p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {formatElapsed(elapsedMs)}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Status</p>
            <p className="text-sm font-semibold text-foreground mt-0.5 capitalize">
              {transfer.status}
            </p>
          </div>
        </div>

        {/* Batch List */}
        {batches && batches.batches.length > 0 && (
          <div>
            <p className="text-xs font-medium text-foreground mb-2">Batches</p>
            <div className="space-y-1.5">
              {batches.batches.map((b) => (
                <div key={b.id} className="flex items-center justify-between text-xs px-2 py-1.5 bg-muted/50 rounded">
                  <span className="text-foreground">Batch {b.batch_number}</span>
                  <div className="flex items-center gap-1.5">
                    <span className="text-muted-foreground">{b.completed_items}/{b.total_items}</span>
                    {b.status === 'completed' ? (
                      <CheckCircle2 className="w-3 h-3 text-green-500" />
                    ) : b.status === 'processing' ? (
                      <Loader2 className="w-3 h-3 text-blue-500 animate-spin" />
                    ) : b.status === 'failed' ? (
                      <AlertCircle className="w-3 h-3 text-red-500" />
                    ) : (
                      <Clock className="w-3 h-3 text-muted-foreground" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Failed Items list */}
        {failedItemsData && failedItemsData.items.length > 0 && (
          <div className="space-y-2 mt-4 pt-3 border-t border-border">
            <p className="text-xs font-semibold text-red-600 dark:text-red-400 flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5" />
              Failed Items ({failedItemsData.items.length})
            </p>
            <div className="max-h-48 overflow-y-auto space-y-1.5 border border-red-200/20 dark:border-red-900/20 rounded-md p-2.5 bg-red-500/5 dark:bg-red-500/5">
              {failedItemsData.items.map((item) => (
                <div key={item.id} className="text-[11px] leading-relaxed">
                  <div className="font-semibold text-foreground truncate" title={item.file_name}>
                    {item.file_name}
                  </div>
                  <div className="text-red-600 dark:text-red-400 break-words font-mono text-[10px] mt-0.5">
                    {item.error_message || 'Unknown error'}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// TransferPage
// ---------------------------------------------------------------------------
export default function TransferPage() {
  const transfer = useTransferStore((s) => s.transfer)
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const initTransfer = useTransferStore((s) => s.initTransfer)
  const updateFromPolling = useTransferStore((s) => s.updateFromPolling)
  const resetTransfer = useTransferStore((s) => s.resetTransfer)
  const duplicates = useTransferStore((s) => s.duplicates)

  const { data: session } = useSession(transfer.sessionId)
  const { data: progress } = useSessionProgress(transfer.sessionId)
  const startSession = useStartSession()
  const pauseSession = usePauseSession()
  const cancelSession = useCancelSession()

  const [confirmCancel, setConfirmCancel] = useState(false)
  const cancelTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const initialisedRef = useRef(false)
  const autoStartedRef = useRef(false)
  const navigateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Connect to WS for real-time events (bonus, not required)
  useTransferWs(transfer.sessionId)

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      if (cancelTimerRef.current) clearTimeout(cancelTimerRef.current)
      if (navigateTimerRef.current) clearTimeout(navigateTimerRef.current)
    }
  }, [])

  // Sync session data into store once on mount (initial values from REST)
  useEffect(() => {
    if (session && !initialisedRef.current) {
      initialisedRef.current = true
      initTransfer(session)
    }
  }, [session, initTransfer])

  // Auto-start: when a fresh session (status 'created') is loaded, immediately
  // call the start endpoint so the user doesn't need to click "Start" manually.
  useEffect(() => {
    if (session && initialisedRef.current && session.status === 'created' && !autoStartedRef.current) {
      autoStartedRef.current = true
      if (transfer.sessionId) {
        startSession.mutate(transfer.sessionId)
      }
    }
  }, [session, transfer.sessionId, startSession])

  // Auto-navigate to Library when a transfer reaches a terminal status.
  // completed/completed_with_errors → navigate after 2s delay with success toast.
  // failed → show error toast but stay on page so user can inspect.
  useEffect(() => {
    if (transfer.status === 'completed' || transfer.status === 'completed_with_errors') {
      const message = 'Transfer complete — opening Library...'
      useTransferStore.getState().showNotification('success', message)
      navigateTimerRef.current = setTimeout(() => {
        setCurrentPage('library')
      }, 2000)
      return () => {
        if (navigateTimerRef.current) {
          clearTimeout(navigateTimerRef.current)
          navigateTimerRef.current = null
        }
      }
    }
    if (transfer.status === 'failed') {
      useTransferStore.getState().showNotification('error', 'Transfer failed. Check the error log.')
    }
  }, [transfer.status, setCurrentPage])

  // Sync polling data into store (authoritative source for all live fields).
  // This runs at ~750ms and replaces WS events as the primary data source.
  useEffect(() => {
    if (progress) {
      updateFromPolling(progress)
    }
  }, [progress, updateFromPolling])

  // Esc key to go back (safe guard for mid-transfer)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        if (transfer.status === 'running') {
          return
        }
        handleBack()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [transfer.status])

  const handleBack = () => {
    resetTransfer()
    setCurrentPage('dashboard')
  }

  const handleStart = () => {
    if (transfer.sessionId) {
      // If auto-start already initiated for a fresh (created) session, don't
      // double-start. Paused sessions (Resume) are unaffected by this guard.
      if (transfer.status === 'created' && autoStartedRef.current) return
      startSession.mutate(transfer.sessionId)
    }
  }

  const handlePause = () => {
    if (transfer.sessionId) pauseSession.mutate(transfer.sessionId)
  }

  const handleCancel = async () => {
    if (!confirmCancel) {
      setConfirmCancel(true)
      cancelTimerRef.current = setTimeout(() => setConfirmCancel(false), 2000)
      return
    }
    if (cancelTimerRef.current) clearTimeout(cancelTimerRef.current)
    setConfirmCancel(false)
    if (transfer.sessionId) {
      await cancelSession.mutateAsync(transfer.sessionId)
      resetTransfer()
      setCurrentPage('dashboard')
    }
  }

  const isRunning = transfer.status === 'running'
  const isPaused = transfer.status === 'paused'
  const isIdle = transfer.status === 'created'
  const isFinished = ['completed', 'completed_with_errors', 'failed', 'cancelled'].includes(transfer.status)

  return (
    <div className="h-full flex flex-col space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            onClick={handleBack}
            className="no-drag p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <h1 className="text-lg font-bold text-foreground">{transfer.sessionName || 'Transfer'}</h1>
            <div className="flex items-center gap-2 mt-0.5">
              <p className="text-xs text-muted-foreground">{transfer.sourceRoot} {'->'} {transfer.destRoot}</p>
              <span className={cn(
                'text-[10px] font-mono px-1.5 py-0.5 rounded',
                transfer.transferMode === 'copy'
                  ? 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300'
                  : 'bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300',
              )}>
                {transfer.transferMode === 'copy' ? 'COPY' : 'MOVE'}
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {isIdle && (
            <button
              onClick={handleStart}
              disabled={startSession.isPending}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              {startSession.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <>
                  <Play className="w-4 h-4" />
                  Start
                </>
              )}
            </button>
          )}
          {isRunning && (
            <button
              onClick={handlePause}
              disabled={pauseSession.isPending}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-amber-500 text-white rounded-md text-sm font-medium hover:bg-amber-600 transition-colors"
            >
              <Pause className="w-4 h-4" />
              Pause
            </button>
          )}
          {isPaused && (
            <button
              onClick={handleStart}
              disabled={startSession.isPending}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              {startSession.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <>
                  <Play className="w-4 h-4" />
                  Resume
                </>
              )}
            </button>
          )}
          {!isFinished && (
            <button
              onClick={handleCancel}
              disabled={cancelSession.isPending}
              className={cn(
                'no-drag inline-flex items-center gap-1.5 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                confirmCancel
                  ? 'bg-red-600 text-white hover:bg-red-700'
                  : 'bg-destructive text-destructive-foreground hover:bg-destructive/90',
              )}
            >
              <X className="w-4 h-4" />
              {confirmCancel && <span>Confirm?</span>}
            </button>
          )}
          {isFinished && (
            <button
              onClick={() => setCurrentPage('library')}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              View Library
            </button>
          )}
        </div>
      </div>

      {/* Duplicates Paused Banner */}
      <AnimatePresence>
        {isPaused && duplicates.report && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="flex items-center gap-2.5 px-4 py-2.5 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg text-sm text-amber-700 dark:text-amber-300">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              <span className="flex-1">
                Duplicate files detected — {duplicates.report.summary}.
              </span>
              {!duplicates.isOpen && (
                <button
                  onClick={() => useTransferStore.getState().openDuplicates(duplicates.report!)}
                  className="no-drag px-3 py-1 bg-amber-600 text-white rounded-md text-xs font-medium hover:bg-amber-700 transition-colors"
                >
                  Review
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Content */}
      <div className="flex-1 flex gap-4 min-h-0">
        <PreviewPanel progress={progress} />
        <TransferMonitor progress={progress} />
      </div>
    </div>
  )
}
