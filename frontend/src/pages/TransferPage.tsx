// ---------------------------------------------------------------------------
// MediaVault v2 — Transfer Page
// Split layout: PreviewPanel (media thumbnails) + TransferMonitor (stats).
// ---------------------------------------------------------------------------

import { useEffect, useState } from 'react'
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
  Loader2,
  Zap,
  HardDrive,
} from 'lucide-react'
import { useSession, useStartSession, usePauseSession, useCancelSession, useSessionBatches } from '@/lib/queries'
import { useTransferStore } from '@/store/transfer'
import { useTransferWs } from '@/hooks/use-transfer-ws'
import { cn } from '@/lib/utils'

// ---------------------------------------------------------------------------
// PreviewPanel — Animated media thumbnail grid
// ---------------------------------------------------------------------------
function PreviewPanel() {
  const activeBatch = useTransferStore((s) => s.transfer.activeBatch)
  const [previewItems, setPreviewItems] = useState<{ name: string; type: 'image' | 'video' | 'audio' | 'other' }[]>([])

  // Simulate live preview items as batches process
  useEffect(() => {
    if (!activeBatch) return
    const fakeNames = [
      'IMG_2024_001.jpg', 'IMG_2024_002.png', 'VID_2024_001.mp4',
      'DSC_003.heic', 'sunset.jpg', 'IMG_2024_005.raw',
      'birthday.mp4', 'photo_001.tiff', 'screencast.webm',
      'podcast.mp3', 'notes.pdf', 'IMG_2024_008.jpg',
    ]
    const types: ('image' | 'video' | 'audio' | 'other')[] = [
      'image', 'image', 'video', 'image', 'image', 'image',
      'video', 'image', 'video', 'audio', 'other', 'image',
    ]
    const count = Math.min(
      Math.floor((activeBatch.hop1Progress / 100) * fakeNames.length),
      fakeNames.length,
    )
    setPreviewItems(
      fakeNames.slice(0, count).map((name, i) => ({
        name,
        type: types[i] ?? 'other',
      })),
    )
  }, [activeBatch?.hop1Progress, activeBatch])

  const iconMap = {
    image: <Image className="w-5 h-5 text-blue-400" />,
    video: <Film className="w-5 h-5 text-purple-400" />,
    audio: <Music className="w-5 h-5 text-green-400" />,
    other: <FileText className="w-5 h-5 text-muted-foreground" />,
  }

  return (
    <div className="flex-1 bg-card border border-border rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
          <HardDrive className="w-4 h-4 text-primary" />
          Media Preview
        </h3>
        <span className="text-xs text-muted-foreground">{previewItems.length} files</span>
      </div>

      <div className="p-4 h-[calc(100%-48px)] overflow-y-auto">
        {previewItems.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
            <Image className="w-12 h-12 mb-3 opacity-30" />
            <p className="text-sm">Waiting for transfer to begin...</p>
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-2">
            <AnimatePresence>
              {previewItems.map((item, i) => (
                <motion.div
                  key={item.name + i}
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                  transition={{ duration: 0.2, delay: i * 0.03 }}
                  className="aspect-square bg-muted rounded-md flex flex-col items-center justify-center gap-1 hover:bg-muted/80 transition-colors"
                >
                  {iconMap[item.type]}
                  <span className="text-[10px] text-muted-foreground truncate w-full text-center px-1">
                    {item.name}
                  </span>
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// TransferMonitor — Performance stats and progress
// ---------------------------------------------------------------------------
function TransferMonitor() {
  const transfer = useTransferStore((s) => s.transfer)
  const { sessionId } = transfer
  const { data: batches } = useSessionBatches(sessionId)

  const activeBatch = transfer.activeBatch
  const overallPct = transfer.totalItems > 0
    ? Math.round((transfer.completedItems / transfer.totalItems) * 100)
    : 0

  const formatEta = (ms: number | null) => {
    if (ms === null) return '--:--'
    const sec = Math.floor(ms / 1000)
    const min = Math.floor(sec / 60)
    const s = sec % 60
    return `${min}:${String(s).padStart(2, '0')}`
  }

  const formatElapsed = (ms: number) => {
    const sec = Math.floor(ms / 1000)
    const min = Math.floor(sec / 60)
    const s = sec % 60
    return `${min}:${String(s).padStart(2, '0')}`
  }

  return (
    <div className="w-80 bg-card border border-border rounded-lg flex flex-col">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
          <Zap className="w-4 h-4 text-primary" />
          Transfer Monitor
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
                transfer.status === 'completed' ? 'bg-green-500' : 'bg-primary',
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
              {transfer.speed > 0 ? `${transfer.speed.toFixed(1)} f/s` : '--'}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">ETA</p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {formatEta(transfer.eta)}
            </p>
          </div>
          <div className="bg-muted/50 rounded-md p-2.5">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">Elapsed</p>
            <p className="text-sm font-semibold text-foreground mt-0.5">
              {formatElapsed(transfer.elapsed)}
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
  const resetTransfer = useTransferStore((s) => s.resetTransfer)

  const { data: session } = useSession(transfer.sessionId)
  const startSession = useStartSession()
  const pauseSession = usePauseSession()
  const cancelSession = useCancelSession()

  // Connect to WS for real-time events
  useTransferWs(transfer.sessionId)

  // Sync session data into store
  useEffect(() => {
    if (session) {
      initTransfer(session)
    }
  }, [session, initTransfer])

  const handleBack = () => {
    resetTransfer()
    setCurrentPage('dashboard')
  }

  const handleStart = () => {
    if (transfer.sessionId) startSession.mutate(transfer.sessionId)
  }

  const handlePause = () => {
    if (transfer.sessionId) pauseSession.mutate(transfer.sessionId)
  }

  const handleCancel = async () => {
    if (transfer.sessionId) {
      await cancelSession.mutateAsync(transfer.sessionId)
      resetTransfer()
      setCurrentPage('dashboard')
    }
  }

  const isRunning = transfer.status === 'running'
  const isPaused = transfer.status === 'paused'
  const isCompleted = transfer.status === 'completed'
  const isIdle = transfer.status === 'created'

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
            <p className="text-xs text-muted-foreground">{transfer.sourceRoot} {'->'} {transfer.destRoot}</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {isIdle && (
            <button
              onClick={handleStart}
              disabled={startSession.isPending}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              <Play className="w-4 h-4" />
              Start
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
              <Play className="w-4 h-4" />
              Resume
            </button>
          )}
          {!isCompleted && (
            <button
              onClick={handleCancel}
              disabled={cancelSession.isPending}
              className="no-drag inline-flex items-center gap-1.5 px-3 py-2 bg-destructive text-destructive-foreground rounded-md text-sm font-medium hover:bg-destructive/90 transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          )}
          {isCompleted && (
            <button
              onClick={() => setCurrentPage('library')}
              className="no-drag inline-flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              View Library
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 flex gap-4 min-h-0">
        <PreviewPanel />
        <TransferMonitor />
      </div>
    </div>
  )
}
