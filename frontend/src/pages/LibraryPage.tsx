// ---------------------------------------------------------------------------
// Transfera v2 — Library Page
// Masonry view of completed items with infinite scroll.
// ---------------------------------------------------------------------------

import { useEffect, useRef, useCallback, useState, Component } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Image,
  Film,
  Music,
  FileText,
  Search,
  History,
  Grid3X3,
  LayoutList,
  CheckCircle2,
  XCircle,
  Loader2,
  HardDrive,
  RefreshCw,
  Trash2,
  AlertTriangle,
} from 'lucide-react'
import { useMediaList, useClearLibrary } from '@/lib/queries'
import { useQueryClient } from '@tanstack/react-query'
import TransferHistoryTable from '@/components/TransferHistoryTable'
import apiClient from '@/lib/api-client'
import { useTransferStore } from '@/store/transfer'
import { cn } from '@/lib/utils'
import { fetchThumbnail } from '@/lib/thumbnail-fetch'
import type { MediaItemInfo, HopStatus } from '@/types/api'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const extIconMap: Record<string, React.ReactNode> = {
  '.jpg': <Image className="w-5 h-5" />,
  '.jpeg': <Image className="w-5 h-5" />,
  '.png': <Image className="w-5 h-5" />,
  '.gif': <Image className="w-5 h-5" />,
  '.heic': <Image className="w-5 h-5" />,
  '.heif': <Image className="w-5 h-5" />,
  '.webp': <Image className="w-5 h-5" />,
  '.raw': <Image className="w-5 h-5" />,
  '.bmp': <Image className="w-5 h-5" />,
  '.tiff': <Image className="w-5 h-5" />,
  '.tif': <Image className="w-5 h-5" />,
  '.cr2': <Image className="w-5 h-5" />,
  '.cr3': <Image className="w-5 h-5" />,
  '.nef': <Image className="w-5 h-5" />,
  '.arw': <Image className="w-5 h-5" />,
  '.dng': <Image className="w-5 h-5" />,
  '.avif': <Image className="w-5 h-5" />,
  '.jxl': <Image className="w-5 h-5" />,
  '.mp4': <Film className="w-5 h-5" />,
  '.mkv': <Film className="w-5 h-5" />,
  '.mov': <Film className="w-5 h-5" />,
  '.avi': <Film className="w-5 h-5" />,
  '.webm': <Film className="w-5 h-5" />,
  '.m4v': <Film className="w-5 h-5" />,
  '.3gp': <Film className="w-5 h-5" />,
  '.mp3': <Music className="w-5 h-5" />,
  '.flac': <Music className="w-5 h-5" />,
  '.wav': <Music className="w-5 h-5" />,
  '.aac': <Music className="w-5 h-5" />,
  '.ogg': <Music className="w-5 h-5" />,
  '.m4a': <Music className="w-5 h-5" />,
  '.wma': <Music className="w-5 h-5" />,
  '.pdf': <FileText className="w-5 h-5" />,
}

function getIcon(ext?: string) {
  if (!ext) return <FileText className="w-5 h-5 text-muted-foreground" />
  return extIconMap[ext.toLowerCase()] ?? <FileText className="w-5 h-5 text-muted-foreground" />
}

function getStatusIcon(status: HopStatus) {
  switch (status) {
    case 'completed':
      return <CheckCircle2 className="w-3 h-3 text-green-500" />
    case 'failed':
      return <XCircle className="w-3 h-3 text-red-500" />
    case 'transferring':
    case 'hashing':
      return <Loader2 className="w-3 h-3 text-blue-500 animate-spin" />
    default:
      return null
  }
}

function formatSize(bytes: number) {
  if (bytes > 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
  if (bytes > 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes > 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}

// ---------------------------------------------------------------------------
// Masonry Grid Column Heights
// ---------------------------------------------------------------------------
function useMasonryColumns(items: MediaItemInfo[], containerWidth: number, gap = 8) {
  const [columns, setColumns] = useState<MediaItemInfo[][]>([])

  useEffect(() => {
    const colCount = containerWidth > 900 ? 4 : containerWidth > 600 ? 3 : 2
    const cols: MediaItemInfo[][] = Array.from({ length: colCount }, () => [])
    const heights = Array(colCount).fill(0)

    for (const item of items) {
      const shortestCol = heights.indexOf(Math.min(...heights))
      const col = cols[shortestCol]
      if (col) {
        col.push(item)
      }
      // Vary height by extension type
      const isVideo = item.extension === '.mp4' || item.extension === '.mov' || item.extension === '.mkv'
      heights[shortestCol] += isVideo ? 220 : 160
    }

    setColumns(cols)
  }, [items, containerWidth, gap])

  return columns
}

// ---------------------------------------------------------------------------
// LibraryCard
// ---------------------------------------------------------------------------
function LibraryCard({ item }: { item: MediaItemInfo }) {
  const [thumbUrl, setThumbUrl] = useState<string | null>(null)
  const [noThumb, setNoThumb] = useState(false)
  const extLower = item.extension?.toLowerCase()
  const isImage = extLower && ['.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif', '.webp', '.raw', '.bmp', '.tiff', '.tif', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.avif', '.jxl'].includes(extLower)
  const isVideo = extLower && ['.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.3gp'].includes(extLower)
  const isAudio = extLower && ['.mp3', '.flac', '.wav', '.aac', '.ogg', '.m4a', '.wma'].includes(extLower)
  const isFailed = item.thumbnail_status === 'failed'

  useEffect(() => {
    if (isFailed || noThumb) return
    // Allow fetching if thumbnail_status is 'ready' OR 'pending' (endpoint generates on demand)
    if (item.thumbnail_status !== 'ready' && item.thumbnail_status !== 'pending') return

    const controller = new AbortController()
    let cancelled = false

    fetchThumbnail(item.id, item.updated_at, controller.signal).then((url) => {
      if (cancelled) return
      if (url) {
        setThumbUrl(url)
      } else {
        setNoThumb(true)
      }
    })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [item.id, item.thumbnail_status, isFailed, noThumb])

  // Cleanup blob URLs on unmount
  useEffect(() => {
    return () => {
      if (thumbUrl) URL.revokeObjectURL(thumbUrl)
    }
  }, [thumbUrl])

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card border border-border rounded-lg overflow-hidden hover:shadow-md transition-shadow group"
    >
      {/* Preview Area */}
      <div className={cn(
        'relative flex items-center justify-center overflow-hidden',
        isVideo ? 'aspect-4/3' : isImage ? 'aspect-square' : 'aspect-4/3',
        isImage && !thumbUrl && !isFailed ? 'bg-blue-50 dark:bg-blue-950' : '',
        isVideo && !thumbUrl && !isFailed ? 'bg-purple-50 dark:bg-purple-950' : '',
        isAudio ? 'bg-green-50 dark:bg-green-950' : '',
        isFailed ? 'bg-muted' : '',
        !isImage && !isVideo && !isAudio && !isFailed ? 'bg-muted' : '',
      )}>
        {thumbUrl ? (
          <img
            src={thumbUrl}
            alt={item.file_name}
            className="w-full h-full object-cover"
            onError={() => { setThumbUrl(null); setNoThumb(true) }}
          />
        ) : (
          <div className={cn(
            'opacity-40 group-hover:opacity-60 transition-opacity',
            isImage ? 'text-blue-400' : isVideo ? 'text-purple-400' : isAudio ? 'text-green-400' : 'text-muted-foreground',
          )}>
            {getIcon(item.extension)}
          </div>
        )}
        {/* Status overlay */}
        <div className="absolute top-2 right-2">
          {getStatusIcon(item.final_status)}
        </div>
        {item.live_photo_group && (
          <div className="absolute top-2 left-2 px-1.5 py-0.5 bg-primary/80 text-primary-foreground rounded text-[9px] font-medium">
            Live Photo
          </div>
        )}
      </div>

      {/* Info */}
      <div className="p-2.5">
        <p className="text-xs font-medium text-foreground truncate" title={item.file_name}>
          {item.file_name}
        </p>
        <div className="flex items-center justify-between mt-1">
          <span className="text-[10px] text-muted-foreground">{formatSize(item.file_size)}</span>
          <span className="text-[10px] text-muted-foreground font-mono">{item.extension}</span>
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
// MediaGridBoundary
// ---------------------------------------------------------------------------
class MediaGridBoundary extends Component<
  { children: React.ReactNode },
  { crashed: boolean }
> {
  state = { crashed: false }

  static getDerivedStateFromError(): { crashed: true } {
    return { crashed: true }
  }

  componentDidCatch(error: Error) {
    console.error('MediaGrid error:', error)
  }

  render() {
    if (this.state.crashed) {
      return (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <AlertTriangle className="w-12 h-12 mb-3 opacity-30" />
          <p className="text-sm">Unable to load media</p>
          <button
            onClick={() => this.setState({ crashed: false })}
            className="mt-3 px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-md hover:opacity-90 transition-opacity"
          >
            Retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

// ---------------------------------------------------------------------------
// LibraryPage
// ---------------------------------------------------------------------------
export default function LibraryPage() {
  const [search, setSearch] = useState('')
  const [extension, setExtension] = useState('')
  const [finalStatus, setFinalStatus] = useState('completed')
  const [viewMode, setViewMode] = useState<'masonry' | 'list' | 'history'>('masonry')
  const [regenStatus, setRegenStatus] = useState<'idle' | 'loading' | 'done'>('idle')
  const [showClearDialog, setShowClearDialog] = useState(false)
  const clearLibrary = useClearLibrary()

  const library = useTransferStore((s) => s.library)
  const appendLibraryItems = useTransferStore((s) => s.appendLibraryItems)
  const resetLibrary = useTransferStore((s) => s.resetLibrary)
  const setLoadingMore = useTransferStore((s) => s.setLoadingMore)
  const queryClient = useQueryClient()
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const hasAutoRegenRef = useRef(false)

  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current)
    }
  }, [])

  const lastSessionId = useTransferStore((s) => s.ui.lastCompletedSessionId)
  const lastRegeneratedSessionId = useTransferStore((s) => s.ui.lastRegeneratedSessionId)
  const setLastRegeneratedSessionId = useTransferStore((s) => s.setLastRegeneratedSessionId)
  const [showingRecent, setShowingRecent] = useState(true)

  useEffect(() => {
    if (lastSessionId) {
      setShowingRecent(true)
    }
  }, [lastSessionId])

  // Auto-trigger thumbnail regeneration when a transfer completes
  useEffect(() => {
    if (
      lastSessionId != null &&
      lastSessionId !== lastRegeneratedSessionId
    ) {
      setLastRegeneratedSessionId(lastSessionId)
      handleRegenThumbnails()
    }
  }, [lastSessionId, lastRegeneratedSessionId])

  const sessionFilter = showingRecent && lastSessionId ? lastSessionId : undefined
  const [fetchPage, setFetchPage] = useState(1)

  const { data, isLoading, isFetching } = useMediaList({
    page: fetchPage,
    pageSize: 50,
    sessionId: sessionFilter,
    extension: extension || undefined,
    finalStatus: finalStatus || undefined,
    search: search || undefined,
  })

  const sentinelRef = useRef<HTMLDivElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [containerWidth, setContainerWidth] = useState(800)
  const filterKeyRef = useRef(0)
  const loadedPages = useRef<Set<number>>(new Set())

  const [filterKey, setFilterKey] = useState(0)

  useEffect(() => {
    filterKeyRef.current += 1
    const myKey = filterKeyRef.current
    loadedPages.current = new Set()
    hasAutoRegenRef.current = false
    resetLibrary()
    setFetchPage(1)
    setFilterKey(myKey)
  }, [search, extension, finalStatus, sessionFilter, resetLibrary])

  useEffect(() => {
    if (!data || data.items.length === 0) return
    if (filterKey !== filterKeyRef.current) return
    if (loadedPages.current.has(fetchPage)) return
    loadedPages.current.add(fetchPage)
    appendLibraryItems(data.items, data.total, data.pages)
  }, [data, filterKey, fetchPage, appendLibraryItems])

  // Auto-trigger thumbnail regeneration when library loads items with pending
  // thumbnails, firing at most once per filter/mount cycle.
  useEffect(() => {
    if (hasAutoRegenRef.current) return
    if (!data || data.items.length === 0) return
    const hasPending = data.items.some(
      (item) => item.thumbnail_status === 'pending',
    )
    if (!hasPending) return

    hasAutoRegenRef.current = true

    apiClient.post('/media/regenerate-thumbnails').then((res) => {
      const resData = res.data as { total?: number; count?: number }
      const total = resData.total ?? resData.count ?? 0
      if (total === 0) return
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current)
      let attempts = 0
      pollIntervalRef.current = setInterval(() => {
        attempts++
        queryClient.invalidateQueries({ queryKey: ['media'] })
        if (attempts >= 20) {
          clearInterval(pollIntervalRef.current!)
          pollIntervalRef.current = null
        }
      }, 2000)
    }).catch(() => {
      // Silently ignore — user can always click the manual button
    })
  }, [data, queryClient])

  // Measure container
  useEffect(() => {
    if (!containerRef.current) return
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setContainerWidth(entry.contentRect.width)
      }
    })
    observer.observe(containerRef.current)
    return () => observer.disconnect()
  }, [])

  const columns = useMasonryColumns(library.items, containerWidth)

  // Infinite scroll observer
  const loadMore = useCallback(() => {
    if (library.isLoadingMore || isFetching) return
    if (library.total === 0 || library.page > library.totalPages) return
    setLoadingMore(true)
    setFetchPage(p => p + 1)
  }, [library.isLoadingMore, library.page, library.totalPages, isFetching, setLoadingMore])

  useEffect(() => {
    if (!sentinelRef.current) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          loadMore()
        }
      },
      { threshold: 0.1 },
    )
    observer.observe(sentinelRef.current)
    return () => observer.disconnect()
  }, [loadMore])

  const allExtensions = ['.jpg', '.png', '.mp4', '.mov', '.mp3', '.pdf', '.raw', '.heic']

  const handleRegenThumbnails = async () => {
    if (pollIntervalRef.current) clearInterval(pollIntervalRef.current)
    setRegenStatus('loading')
    try {
      const res = await apiClient.post('/media/regenerate-thumbnails')
      const data = res.data as { total?: number; count?: number }
      const total = data.total ?? data.count ?? 0
      if (total === 0) {
        setRegenStatus('done')
        return
      }
      let attempts = 0
      pollIntervalRef.current = setInterval(() => {
        attempts++
        queryClient.invalidateQueries({ queryKey: ['media'] })
        if (attempts >= 15) {
          clearInterval(pollIntervalRef.current!)
          pollIntervalRef.current = null
          setRegenStatus('done')
        }
      }, 3000)
    } catch {
      setRegenStatus('idle')
    }
  }

  return (
    <div className="h-full flex flex-col space-y-4">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Library</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {library.total} items completed
        </p>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search files..."
            className="w-full pl-9 pr-3 py-2 bg-background border border-input rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring"
          />
        </div>

        <select
          value={extension}
          onChange={(e) => setExtension(e.target.value)}
          className="px-3 py-2 bg-background border border-input rounded-md text-sm text-foreground focus:outline-hidden focus:ring-2 focus:ring-ring"
        >
          <option value="">All types</option>
          {allExtensions.map((ext) => (
            <option key={ext} value={ext}>{ext}</option>
          ))}
        </select>

        <select
          value={finalStatus}
          onChange={(e) => setFinalStatus(e.target.value)}
          className="px-3 py-2 bg-background border border-input rounded-md text-sm text-foreground focus:outline-hidden focus:ring-2 focus:ring-ring"
        >
          <option value="">All statuses</option>
          <option value="completed">Completed (default)</option>
          <option value="failed">Failed</option>
          <option value="pending">Pending</option>
        </select>

        <div className="flex items-center border border-input rounded-md">
          <button
            onClick={() => setViewMode('masonry')}
            className={cn(
              'p-2 rounded-l-md transition-colors',
              viewMode === 'masonry' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted',
            )}
            title="Grid view"
          >
            <Grid3X3 className="w-4 h-4" />
          </button>
          <button
            onClick={() => setViewMode('list')}
            className={cn(
              'p-2 transition-colors',
              viewMode === 'list' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted',
            )}
            title="List view"
          >
            <LayoutList className="w-4 h-4" />
          </button>
          <button
            onClick={() => setViewMode('history')}
            className={cn(
              'p-2 rounded-r-md transition-colors',
              viewMode === 'history' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted',
            )}
            title="Transfer history"
          >
            <History className="w-4 h-4" />
          </button>
        </div>

        <button
          onClick={handleRegenThumbnails}
          disabled={regenStatus === 'loading'}
          className={cn(
            'flex items-center gap-1.5 px-3 py-2 rounded-md text-sm border transition-colors',
            regenStatus === 'done'
              ? 'border-green-500 text-green-600 bg-green-50 dark:bg-green-950'
              : 'border-input text-muted-foreground hover:bg-muted hover:text-foreground',
            regenStatus === 'loading' && 'opacity-60 cursor-not-allowed',
          )}
          title="Re-generate thumbnails for items missing preview images"
        >
          {regenStatus === 'loading' ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : regenStatus === 'done' ? (
            <CheckCircle2 className="w-4 h-4" />
          ) : (
            <RefreshCw className="w-4 h-4" />
          )}
          {regenStatus === 'done' ? 'Done' : 'Regen Thumbnails'}
        </button>

        <button
          onClick={() => setShowClearDialog(true)}
          disabled={library.total === 0}
          className="flex items-center gap-1.5 px-3 py-2 rounded-md text-sm border border-input text-muted-foreground hover:bg-red-50 dark:hover:bg-red-950 hover:text-red-600 dark:hover:text-red-400 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          title="Clear all library entries"
        >
          <Trash2 className="w-4 h-4" />
          Clear Library
        </button>
      </div>

      {/* Session filter banner */}
      {lastSessionId && (
        <div className="flex items-center gap-2 px-3 py-2 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg text-xs">
          <span className="flex-1 text-blue-700 dark:text-blue-300">
            {showingRecent
              ? `Showing recent transfer (Session #${lastSessionId})`
              : `Showing all transfers`}
          </span>
          <button
            type="button"
            onClick={() => setShowingRecent(r => !r)}
            className="px-2.5 py-1 bg-blue-600 text-white rounded-md font-medium hover:bg-blue-700 transition-colors shrink-0"
          >
            {showingRecent ? 'Show all' : 'Show recent'}
          </button>
        </div>
      )}

      <ConfirmDialog
        open={showClearDialog}
        title="Clear Library"
        description={`This will permanently remove all ${library.total} library item(s), their database records, and generated thumbnails from the app.\n\nThis only clears app-managed records and cached thumbnails — your actual photos and videos at the transfer destination are NOT affected and will not be deleted.\n\nThis action cannot be undone.`}
        confirmLabel="Clear Library"
        onConfirm={() => {
          if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current)
            pollIntervalRef.current = null
          }
          setRegenStatus('idle')
          clearLibrary.mutate(undefined, {
            onSettled: () => setShowClearDialog(false),
          })
        }}
        onCancel={() => setShowClearDialog(false)}
        loading={clearLibrary.isPending}
      />

      {/* Content */}
      <div ref={containerRef} className="flex-1 overflow-y-auto">
        {viewMode === 'history' ? (
          <TransferHistoryTable />
        ) : isLoading && library.items.length === 0 ? (
          <div className="grid grid-cols-3 gap-3">
            {Array.from({ length: 9 }).map((_, i) => (
              <div key={i} className="bg-card border border-border rounded-lg overflow-hidden animate-pulse">
                <div className="aspect-square bg-muted" />
                <div className="p-2.5 space-y-1.5">
                  <div className="h-3 bg-muted rounded w-3/4" />
                  <div className="h-2 bg-muted rounded w-1/2" />
                </div>
              </div>
            ))}
          </div>
        ) : library.items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
            <HardDrive className="w-12 h-12 mb-3 opacity-30" />
            <p className="text-sm">No items in library yet</p>
            <p className="text-xs mt-1">Complete a transfer to see files here</p>
          </div>
        ) : (
          <MediaGridBoundary>
            {viewMode === 'masonry' ? (
              <div className="flex gap-2" style={{ minHeight: 400 }}>
                {columns.map((col, colIdx) => (
                  <div key={colIdx} className="flex-1 space-y-2">
                    {col.map((item) => (
                      <LibraryCard key={item.id} item={item} />
                    ))}
                  </div>
                ))}
              </div>
            ) : (
              <div className="space-y-1">
                <AnimatePresence>
                  {library.items.map((item) => (
                    <div
                      key={item.id}
                      className="flex items-center gap-3 px-3 py-2 bg-card border border-border rounded-md hover:bg-muted/50 transition-colors"
                    >
                      <div className="w-8 h-8 rounded bg-muted flex items-center justify-center shrink-0">
                        {getIcon(item.extension)}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm text-foreground truncate">{item.file_name}</p>
                        <p className="text-xs text-muted-foreground truncate">{item.source_path}</p>
                      </div>
                      <span className="text-xs text-muted-foreground shrink-0">{formatSize(item.file_size)}</span>
                      {getStatusIcon(item.final_status)}
                    </div>
                  ))}
                </AnimatePresence>
              </div>
            )}
          </MediaGridBoundary>
        )}

        {/* Infinite scroll sentinel — only for non-history views */}
        {viewMode !== 'history' && library.page <= library.totalPages && (
          <>
            <div ref={sentinelRef} className="h-10" />
            {(library.isLoadingMore || isFetching) && (
              <div className="flex justify-center py-4">
                <Loader2 className="w-5 h-5 text-muted-foreground animate-spin" />
              </div>
            )}
            {library.total > library.items.length && !library.isLoadingMore && !isFetching && (
              <p className="text-center text-xs text-muted-foreground py-2">
                {library.items.length} of {library.total} items shown
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
