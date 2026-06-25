// ---------------------------------------------------------------------------
// Transfera v2 — Transfer Page
// Split layout: PreviewPanel (media thumbnails) + TransferMonitor (stats).
// Polling-based: REST is the source of truth, WebSocket is a bonus.
// ---------------------------------------------------------------------------

import { useEffect, useState, useRef, useMemo } from 'react'
import type { ThumbQueue } from '@/lib/thumb-queue'
import { createThumbQueue } from '@/lib/thumb-queue'
import { fetchThumbnail } from '@/lib/thumbnail-fetch'
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
import { cn, isElectron } from '@/lib/utils'
import type { SessionProgress, RecentItemProgress } from '@/types/api'

// Keep track of session IDs that have already been started to prevent double-triggering
// on component mount/remount in React Strict Mode.
const startedSessions = new Set<number>()

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fileIcon(name: string) {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  if (['jpg', 'jpeg', 'png', 'gif', 'heic', 'heif', 'raw', 'tiff', 'tif', 'webp', 'bmp', 'svg', 'cr2', 'cr3', 'nef', 'arw', 'dng', 'avif', 'jxl'].includes(ext))
    return <Image className="w-5 h-5 text-blue-400" />
  if (['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', '3gp'].includes(ext))
    return <Film className="w-5 h-5 text-purple-400" />
  if (['mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a', 'wma'].includes(ext))
    return <Music className="w-5 h-5 text-green-400" />
  if (['pdf', 'doc', 'docx', 'txt', 'md', 'rtf'].includes(ext))
    return <FileText className="w-5 h-5 text-orange-400" />
  return <FileText className="w-5 h-5 text-muted-foreground" />
}

function formatEta(ms: number | null | 'done') {
  if (ms === 'done') return '--'
  if (ms === null || ms <= 0) return 'Calculating...'
  const totalSec = Math.ceil(ms / 1000)
  if (totalSec < 60) return `${totalSec}s remaining`
  const min = Math.floor(totalSec / 60)
  const sec = totalSec % 60
  if (min < 60) return `${min}m ${sec}s remaining`
  const hr = Math.floor(min / 60)
  const remMin = min % 60
  return `${hr}h ${remMin}m remaining`
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return ''
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
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
// PreviewThumbnail — Lazy loaded thumbnail with concurrency queue
// ---------------------------------------------------------------------------
function PreviewThumbnail({ itemId, name, thumbQueue }: { itemId: number | null; name: string; thumbQueue: ThumbQueue }) {
  const [imgSrc, setImgSrc] = useState<string | null>(null)
  const [loadState, setLoadState] = useState<'idle' | 'loading' | 'loaded' | 'error'>('idle')
  const [retryCount, setRetryCount] = useState(0)
  const [isIntersecting, setIsIntersecting] = useState(false)
  const cellRef = useRef<HTMLDivElement>(null)
  const slotHeld = useRef(false)
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    return () => {
      if (imgSrc) {
        URL.revokeObjectURL(imgSrc)
      }
    }
  }, [imgSrc])

  useEffect(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      abortControllerRef.current = null
    }
    if (slotHeld.current) {
      slotHeld.current = false
      thumbQueue.release()
    }
    setImgSrc(null)
    setLoadState('idle')
    setRetryCount(0)
    setIsIntersecting(false)
  }, [itemId, thumbQueue])

  useEffect(() => {
    if (!itemId || isIntersecting) return
    const el = cellRef.current
    if (!el) return

    const obs = new IntersectionObserver(([entry]) => {
      if (entry?.isIntersecting) {
        setIsIntersecting(true)
        obs.unobserve(el)
      }
    }, { rootMargin: '600px' })

    obs.observe(el)
    return () => obs.disconnect()
  }, [itemId, isIntersecting])

  useEffect(() => {
    if (!itemId || !isIntersecting || loadState === 'loaded' || loadState === 'error') return

    let cancelled = false
    const controller = new AbortController()
    abortControllerRef.current = controller

    const run = async () => {
      slotHeld.current = true
      thumbQueue.request(async () => {
        if (cancelled) {
          slotHeld.current = false
          thumbQueue.release()
          return
        }

        setLoadState('loading')
        try {
          const url = await fetchThumbnail(itemId, controller.signal)
          if (cancelled) {
            slotHeld.current = false
            thumbQueue.release()
            return
          }

          if (url) {
            setImgSrc(url)
            setLoadState('loaded')
            slotHeld.current = false
            thumbQueue.release()
          } else {
            slotHeld.current = false
            thumbQueue.release()

            if (retryCount < 10) {
              timeoutRef.current = setTimeout(() => {
                if (cancelled) return
                setRetryCount(c => c + 1)
              }, 2000)
            } else {
              setLoadState('error')
            }
          }
        } catch {
          slotHeld.current = false
          thumbQueue.release()

          if (cancelled) return

          if (retryCount < 10) {
            timeoutRef.current = setTimeout(() => {
              if (cancelled) return
              setRetryCount(c => c + 1)
            }, 2000)
          } else {
            setLoadState('error')
          }
        }
      })
    }

    run()

    return () => {
      cancelled = true
      controller.abort()
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
        timeoutRef.current = null
      }
      if (slotHeld.current) {
        slotHeld.current = false
        thumbQueue.release()
      }
    }
  }, [itemId, isIntersecting, retryCount, thumbQueue])

  if (!itemId) {
    return (
      <div className="w-full h-full flex items-center justify-center">
        {fileIcon(name)}
      </div>
    )
  }

  return (
    <div ref={cellRef} className="w-full h-full min-h-[80px]">
      {loadState === 'loading' && (
        <div className="w-full h-20 animate-pulse bg-muted" />
      )}
      {imgSrc && loadState === 'loaded' && (
        <img
          src={imgSrc}
          alt={name}
          className="w-full h-auto block object-cover"
        />
      )}
      {(loadState === 'error' || loadState === 'idle') && (
        <div className="w-full h-20 flex items-center justify-center opacity-40 bg-muted">
          {fileIcon(name)}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// PreviewPanel — Live file transfer preview from polling data
// Items are accumulated keyed by item_id so thumbnails never disappear
// once they appear — polls only add new entries to the view.
// ---------------------------------------------------------------------------
function PreviewPanel({ progress }: { progress: SessionProgress | undefined }) {
  const status = useTransferStore((s) => s.transfer.status)
  const completedItems = progress?.imported_files ?? progress?.completed_items ?? 0
  const totalItems = progress?.total_files ?? progress?.total_items ?? 0
  const isRunning = status === 'running'

  const thumbQueue = useMemo(() => createThumbQueue(8), [])
  useEffect(() => () => { thumbQueue.reset() }, [thumbQueue])

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
      .slice(0, 30)
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
                    <PreviewThumbnail itemId={file.item_id} name={file.file_name} thumbQueue={thumbQueue} />
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
// TransferCompleteSummary — Post-transfer confirmation
// ---------------------------------------------------------------------------
interface TransferCompleteSummaryProps {
  completedItems: number
  failedItems: number
  totalBytesVolume: number | null | undefined
  transferMode: 'copy' | 'move'
  destRoot: string
}

function TransferCompleteSummary({
  completedItems,
  failedItems,
  totalBytesVolume,
  transferMode,
  destRoot,
}: TransferCompleteSummaryProps) {
  const hasSuccess = completedItems > 0
  const sizeText = totalBytesVolume != null ? ` (${formatBytes(totalBytesVolume)})` : ''

  return (
    <div className="p-3 bg-muted/50 rounded-md space-y-2">
      {hasSuccess ? (
        <div className="flex items-start gap-2.5">
          <CheckCircle2 className="w-5 h-5 text-green-500 mt-0.5 shrink-0" />
          <div>
            <p className="text-sm font-semibold text-foreground">
              {completedItems} file{completedItems !== 1 ? 's' : ''}{sizeText} safely backed up and verified
            </p>
            {failedItems > 0 && (
              <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
                {failedItems} file{failedItems !== 1 ? 's' : ''} could not be verified — see details below
              </p>
            )}
            <p className="text-xs text-muted-foreground mt-1.5 leading-relaxed">
              {transferMode === 'copy'
                ? `These files have been copied and verified at ${destRoot}. It is now safe to delete them from your device if you'd like to free up space.`
                : 'These files have been moved and verified — they were already removed from your device automatically.'}
            </p>
          </div>
        </div>
      ) : (
        <div className="flex items-start gap-2.5">
          <AlertTriangle className="w-5 h-5 text-amber-500 mt-0.5 shrink-0" />
          <div>
            <p className="text-sm font-semibold text-foreground">No files were successfully transferred</p>
            <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
              {failedItems} file{failedItems !== 1 ? 's' : ''} could not be verified — see details below
            </p>
          </div>
        </div>
      )}
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
  const { data: session } = useSession(sessionId)
  const { data: batches } = useSessionBatches(sessionId)

  // Fetch failed items
  const { data: failedItemsData } = useMediaList({
    sessionId: sessionId ?? undefined,
    finalStatus: 'failed',
    pageSize: 100,
  })

  const activeBatch = transfer.activeBatch
  const totalFiles = transfer.totalFiles || transfer.totalItems || 1
  const isCancelled = transfer.status === 'cancelled'
  const isCompleted = transfer.status === 'completed'
  const isCompletedWithErrors = transfer.status === 'completed_with_errors'
  const isFailed = transfer.status === 'failed'
  const isPaused = transfer.status === 'paused'
  const isTerminal = [isCompleted, isCompletedWithErrors, isFailed, isCancelled].some(Boolean)

  // Use server-provided progress_percent for the main bar
  // On completion: animate to 100%. On cancel/failure: freeze at last value.
  let displayPct = transfer.progressPercent
  if (isCompleted || isCompletedWithErrors) {
    displayPct = 100
  }

  // Use server-computed timing values (updated each poll)
  const elapsedMs = transfer.elapsed
  const speed = transfer.speed
  const etaMs = isTerminal ? 'done' : transfer.eta

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
  else if (isCompleted) phaseText = 'Transfer complete'
  else if (isCompletedWithErrors) phaseText = 'Transfer completed with errors'
  else if (isFailed) phaseText = 'Transfer failed'
  else if (isCancelled) phaseText = 'Transfer cancelled'
  else phaseText = transfer.status

  // Sync progress to Windows taskbar overlay via tray IPC
  useEffect(() => {
    if (!isElectron) return
    if (isTerminal) {
      window.electronAPI?.setTrayProgress?.(null)
    } else if (transfer.status === 'running' || transfer.status === 'paused') {
      const fraction = Math.max(0, Math.min(1, (transfer.progressPercent ?? 0) / 100))
      window.electronAPI?.setTrayProgress?.(fraction)
    }
  }, [transfer.progressPercent, transfer.status, isTerminal])

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
        {/* Overall Progress — cumulative, never resets between batches */}
        <div>
          <div className="flex justify-between text-xs text-muted-foreground mb-1">
            <span>Overall Progress</span>
            <span className="font-medium text-foreground">{displayPct}%</span>
          </div>
          <div className="w-full h-3 bg-muted rounded-full overflow-hidden">
            <motion.div
              className={cn(
                'h-full rounded-full transition-colors',
                isCompleted ? 'bg-green-500' :
                isCompletedWithErrors ? 'bg-amber-500' :
                isFailed ? 'bg-red-500' :
                'bg-primary',
              )}
              initial={{ width: 0 }}
              animate={{ width: `${displayPct}%` }}
              transition={{ duration: isTerminal ? 0.8 : 0.3 }}
            />
          </div>
          <div className="flex justify-between text-[11px] text-muted-foreground mt-1">
            <span>
              Cached {transfer.cachedFiles} / {totalFiles}
              {' · '}
              Transferred {transfer.importedFiles} / {totalFiles}
            </span>
            <span>{transfer.failedFiles} failed</span>
          </div>
          {transfer.totalBatches > 0 && (
            <div className="text-[10px] text-muted-foreground mt-0.5">
              Batch {transfer.currentBatch} of {transfer.totalBatches}
            </div>
          )}
        </div>

        {/* Phase / Status — summary on completion, phase text otherwise */}
        {isCompleted || isCompletedWithErrors ? (
          <TransferCompleteSummary
            completedItems={transfer.importedFiles}
            failedItems={transfer.failedFiles}
            totalBytesVolume={session?.total_bytes_volume}
            transferMode={transfer.transferMode as 'copy' | 'move'}
            destRoot={transfer.destRoot}
          />
        ) : (
          <div className="p-3 bg-muted/50 rounded-md">
            <p className="text-xs font-medium text-foreground">{phaseText}</p>
            {transfer.currentFileName && (
              <p className="text-[11px] text-muted-foreground mt-1 truncate">
                {transfer.currentFileName}
              </p>
            )}
          </div>
        )}

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
              {transfer.status === 'running' && speed === 0 && elapsedMs < 2000
                ? 'Starting...'
                : transfer.status === 'running' && speed === 0
                  ? 'Calculating...'
                  : speed > 0
                    ? speed >= 1.0
                      ? `${speed.toFixed(1)} files/sec`
                      : `${(speed * 60).toFixed(0)} files/min`
                    : '--'}
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
            <p className="text-sm font-semibold text-foreground mt-0.5 flex items-center gap-1.5">
              {transfer.status === 'created' || elapsedMs === 0
                ? '--'
                : formatElapsed(elapsedMs)}
              {isPaused && (
                <span className="text-[10px] font-bold text-amber-500 uppercase">Paused</span>
              )}
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
  const setCompletedSnapshot = useTransferStore((s) => s.setCompletedSnapshot)
  const completedSnapshot = useTransferStore((s) => s.ui.completedSnapshot)
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
  // Connect to WS for real-time events (bonus, not required)
  useTransferWs(transfer.sessionId)

  // Cleanup timers on unmount — do NOT reset transfer state
  useEffect(() => {
    return () => {
      if (cancelTimerRef.current) clearTimeout(cancelTimerRef.current)
      // NOTE: Intentionally NOT calling resetTransfer() here.
      // The completed transfer state is preserved so the user can navigate back
      // to this page and review their transfer results.
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
    if (session && initialisedRef.current && session.status === 'created' && transfer.sessionId && !startedSessions.has(transfer.sessionId)) {
      startedSessions.add(transfer.sessionId)
      autoStartedRef.current = true
      startSession.mutate(transfer.sessionId)
    }
  }, [session, transfer.sessionId, startSession])

  // Capture the completed snapshot when a transfer reaches a terminal state.
  // This freezes the final state into the store so the page shows results even
  // after navigating away and back.
  useEffect(() => {
    if (
      transfer.status === 'completed' ||
      transfer.status === 'completed_with_errors' ||
      transfer.status === 'failed'
    ) {
      setCompletedSnapshot({ ...transfer })

      if (transfer.sessionId && (transfer.status === 'completed' || transfer.status === 'completed_with_errors')) {
        useTransferStore.getState().setLastCompletedSessionId(transfer.sessionId)
      }

      if (!useTransferStore.getState().ui.notification) {
        if (transfer.status === 'completed') {
          useTransferStore.getState().showNotification(
            'success',
            `${transfer.importedFiles} file${transfer.importedFiles !== 1 ? 's' : ''} transferred successfully.`,
          )
        } else if (transfer.status === 'completed_with_errors') {
          useTransferStore.getState().showNotification(
            'warning',
            `${transfer.importedFiles} transferred, ${transfer.failedFiles} failed.`,
          )
        } else if (transfer.status === 'failed') {
          useTransferStore.getState().showNotification('error', 'Transfer failed. Check the details below.')
        }
      }
    }
  }, [transfer.status, transfer.sessionId, transfer.importedFiles, transfer.failedFiles, setCompletedSnapshot])

  // Sync polling data into store (authoritative source for all live fields).
  // No auto-navigation — the snapshot-capture effect handles terminal states.
  useEffect(() => {
    if (!progress) return
    updateFromPolling(progress)
  }, [progress, updateFromPolling])

  // On mount: if there's no active session but there IS a completed snapshot,
  // restore it so the page shows the last transfer result.
  useEffect(() => {
    if (!transfer.sessionId && completedSnapshot && !initialisedRef.current) {
      initialisedRef.current = true
    }
  }, [transfer.sessionId, completedSnapshot])

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
    // Do NOT reset transfer state — user can come back to see results
    setCurrentPage('dashboard')
  }

  const handleStart = () => {
    if (transfer.sessionId) {
      // If auto-start already initiated for a fresh (created) session, don't
      // double-start. Paused sessions (Resume) are unaffected by this guard.
      if (transfer.status === 'created' && (autoStartedRef.current || startedSessions.has(transfer.sessionId))) return
      if (transfer.status === 'created') {
        startedSessions.add(transfer.sessionId)
      }
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
