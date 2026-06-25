import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Check, ImageOff, Upload, Image, Film, SlidersHorizontal } from 'lucide-react'
import { CheckCircle, AlertTriangle, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'
import ErrorBoundary from './ErrorBoundary'

const API_BASE = window.location.origin || 'http://127.0.0.1:47821'
const THUMBNAIL_SIZE = 200
const GRID_COLUMNS = 4
// Raised from 6 → 12 to saturate the parallel thumbnail worker pool on the backend.
// The backend now has up to 8 pre-scan workers + expanded uvicorn threadpool,
// so 12 concurrent requests keep them fully occupied without overwhelming the AFC layer.
const MAX_CONCURRENT_THUMBS = 4

// --- Per-panel thumbnail request queue ---
// Created fresh each time the panel mounts / resets, so stale callbacks
// from a previous sort/path never clog the queue of a new render.
function createThumbQueue() {
  let active = 0
  const queue = []
  let epoch = 0

  function request(onGranted) {
    if (active < MAX_CONCURRENT_THUMBS) {
      active++
      onGranted()
    } else {
      queue.push(onGranted)
    }
  }

  function release() {
    // Guard against going negative (e.g. double-release)
    if (active > 0) active--
    if (queue.length > 0 && active < MAX_CONCURRENT_THUMBS) {
      const next = queue.shift()
      active++
      next()
    }
  }

  // Drain the queue (called on sort/path change so stale callbacks are discarded)
  function reset() {
    epoch++
    queue.length = 0
    active = 0
  }

  function getEpoch() {
    return epoch
  }

  return { request, release, reset, getEpoch }
}

function formatBytes(bytes) {
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

function formatDuration(seconds) {
  if (seconds == null) return null
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function totalSelectedSize(items, selectedSet) {
  let bytes = 0
  for (const item of items) {
    if (selectedSet.has(item.abs_path)) {
      bytes += item.size_bytes
    }
  }
  return bytes
}

function MediaThumbCell({ item, isSelected, onToggle, isLikelyDuplicate, isFocused, cellIndex, makeThumbnailUrl, thumbQueue }) {
  const cellRef = useRef(null)
  const [loadState, setLoadState] = useState('none') // none | loading | loaded | error
  const [imgSrc, setImgSrc] = useState(null)
  const retryCountRef = useRef(0)
  const retryTimerRef = useRef(null)
  // Track whether this particular item has been queued already
  const slotGrantedRef = useRef(false)
  // Track whether the slot was granted but the image hasn't finished yet
  // (so we release on unmount if needed)
  const slotHeldRef = useRef(false)

  // Reset state whenever the item identity changes (e.g. sort order flip reuses cells)
  const prevItemIdRef = useRef(null)
  if (prevItemIdRef.current !== item.abs_path) {
    prevItemIdRef.current = item.abs_path
    // Synchronously reset so the effect below sees a clean slate.
    // We can't call setState here (would cause re-render loop), so we use refs to gate
    // the observer, and set state only through the normal flow.
    slotGrantedRef.current = false
    slotHeldRef.current = false
    retryCountRef.current = 0
  }

  // IntersectionObserver: request a thumbnail slot when the cell enters viewport.
  // IMPORTANT: this effect does NOT depend on `loadState` — that caused a new observer
  // to be created on every state change, leading to the observer firing multiple times.
  // Instead we use refs to guard against double-requests.
  useEffect(() => {
    const el = cellRef.current
    if (!el) return

    // If this cell already has a slot (or loaded), don't re-observe
    if (slotGrantedRef.current) return

    const scrollContainer = el.closest('.overflow-y-auto') || el.closest('.overflow-auto') || null

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !slotGrantedRef.current) {
          slotGrantedRef.current = true
          slotHeldRef.current = true
          const url = makeThumbnailUrl(item)
          const myEpoch = thumbQueue.getEpoch()
          thumbQueue.request(() => {
            if (thumbQueue.getEpoch() !== myEpoch) {
              slotHeldRef.current = false
              thumbQueue.release()
              return
            }
            setImgSrc(url)
            setLoadState('loading')
          })
          observer.unobserve(el)
        }
      },
      // Increased to 1200px so thumbnails start loading well before the user
      // scrolls to them, and using the nearest scrollPort ancestor as root.
      { root: scrollContainer, rootMargin: '1200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id, makeThumbnailUrl, thumbQueue])

  // On unmount: release the slot if we're still holding it (avoids permanent queue stall)
  useEffect(() => {
    return () => {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current)
      if (slotHeldRef.current) {
        slotHeldRef.current = false
        thumbQueue.release()
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleLoad = () => {
    if (slotHeldRef.current) {
      slotHeldRef.current = false
      thumbQueue.release()
    }
    setLoadState('loaded')
  }

  const handleError = () => {
    if (retryCountRef.current < 3) {
      const delays = [1000, 2000, 4000]
      const delay = delays[retryCountRef.current]
      retryTimerRef.current = setTimeout(() => {
        retryCountRef.current += 1
        const base = makeThumbnailUrl(item)
        const separator = base.includes('?') ? '&' : '?'
        setImgSrc(`${base}${separator}retry=${retryCountRef.current}`)
        setLoadState('loading')
      }, delay)
    } else {
      if (slotHeldRef.current) {
        slotHeldRef.current = false
        thumbQueue.release()
      }
      setLoadState('error')
    }
  }

  return (
    <div
      ref={cellRef}
      role="button"
      onClick={onToggle}
      data-cell-index={cellIndex}
      aria-label={`${item.filename}, ${isSelected ? 'selected' : 'not selected'}`}
      className={cn(
        'relative aspect-square rounded-lg overflow-hidden cursor-pointer group bg-muted',
        isFocused && 'outline-2 outline-[#378ADD] outline-offset-[-2px] shadow-[0_0_0_3px_rgba(55,138,221,0.25)]',
      )}
    >
      {/* Loading shimmer */}
      {loadState === 'loading' && (
        <div className="absolute inset-0 bg-muted">
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent shimmer-animate" />
        </div>
      )}

      {/* Actual image */}
      {imgSrc && (
        <img
          src={imgSrc}
          alt={item.filename}
          onLoad={handleLoad}
          onError={handleError}
          className={cn(
            'w-full h-full object-cover transition-opacity duration-200',
            loadState === 'loaded' ? 'opacity-100' : 'opacity-0',
          )}
        />
      )}

      {/* Error state */}
      {loadState === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center bg-muted">
          <ImageOff className="w-5 h-5 text-muted-foreground/40" />
        </div>
      )}

      {/* Selection circle — top right */}
      <div
        role="checkbox"
        aria-checked={isSelected}
        className={cn(
          'absolute top-1.5 right-1.5 w-5 h-5 rounded-full flex items-center justify-center transition-all duration-150',
          isSelected
            ? 'bg-action border-2 border-white shadow-xs'
            : 'border-2 border-white/85 bg-black/15 group-hover:bg-black/25',
        )}
      >
        <AnimatePresence mode="wait">
          {isSelected && (
            <motion.div
              key="check"
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0, opacity: 0 }}
              transition={{ duration: 0.15, ease: 'easeOut' }}
            >
              <Check className="w-2.5 h-2.5 text-white stroke-[3]" />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Video badge — bottom left */}
      {item.type === 'video' && (
        <div className="absolute bottom-1 left-1 px-1 py-0.5 rounded bg-black/45 text-white text-[10px] leading-none flex items-center gap-0.5">
          <Film className="w-2.5 h-2.5" />
          {item.duration_s != null ? formatDuration(item.duration_s) : ''}
        </div>
      )}

      {/* Likely duplicate badge — bottom right */}
      {isLikelyDuplicate && (
        <div className="absolute bottom-1 right-1 px-1 py-0.5 rounded bg-black/45 text-white text-[10px] leading-none flex items-center gap-0.5">
          <CheckCircle className="w-2.5 h-2.5" />
          In library
        </div>
      )}
    </div>
  )
}

function SkeletonGrid({ count = 12 }) {
  return (
    <div className="grid grid-cols-4 gap-0.5">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="aspect-square rounded-lg bg-muted relative overflow-hidden">
          <div
            className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent shimmer-animate"
            style={{ animationDelay: `${i * 0.05}s` }}
          />
        </div>
      ))}
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
      <ImageOff className="w-8 h-8 mb-2" />
      <p className="text-sm">No media files found in this directory</p>
    </div>
  )
}

function SourcePreviewFallback({ onRetry }) {
  return (
    <div className="border border-border rounded-xl p-3 bg-card">
      <div className="flex flex-col items-center justify-center py-8 text-center space-y-3">
        <div className="w-10 h-10 rounded-full bg-destructive/10 flex items-center justify-center">
          <AlertTriangle className="w-5 h-5 text-destructive" />
        </div>
        <div>
          <p className="text-sm font-medium text-foreground">Preview unavailable</p>
          <p className="text-xs text-muted-foreground mt-1">
            Preview unavailable — you can still proceed with a full directory transfer.
          </p>
        </div>
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:bg-primary/90 transition-colors"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry
        </button>
      </div>
    </div>
  )
}

function SourcePreviewPanelInner({
  sourcePath,
  deviceSource,
  onSelectionConfirm,
  onTransferStart,
}) {
  const [items, setItems] = useState([])
  const [selected, setSelected] = useState(new Set())
  const selectedRef = useRef(selected)
  useEffect(() => { selectedRef.current = selected }, [selected])
  const [filter, setFilter] = useState('all')
  const [loading, setLoading] = useState(false)
  const [metadata, setMetadata] = useState({ total: 0, photos: 0, videos: 0, total_size_bytes: 0 })
  const [likelyDupPaths, setLikelyDupPaths] = useState(new Set())
  const [focusedIndex, setFocusedIndex] = useState(null)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [sortBy, setSortBy] = useState('newest')

  const abortRef = useRef(null)
  const mountedRef = useRef(true)
  const gridRef = useRef(null)

  // Each time source/sort changes we create a fresh queue so stale callbacks
  // from the previous render can't corrupt the new batch of thumbnails.
  const thumbQueueRef = useRef(null)
  if (!thumbQueueRef.current) {
    thumbQueueRef.current = createThumbQueue()
  }
  const epochRef = useRef(0)

  function getPreviewUrl(page, pageSize, sortBy) {
    if (deviceSource) {
      const p = new URLSearchParams({
        device_id: deviceSource.device_id,
        path: deviceSource.device_path,
        page: String(page),
        page_size: String(pageSize),
        sort_by: sortBy,
      })
      return `${API_BASE}/api/device/ios-preview?${p}`
    }
    const p = new URLSearchParams({
      path: sourcePath,
      recursive: 'false',
      page: String(page),
      page_size: String(pageSize),
      sort_by: sortBy,
    })
    return `${API_BASE}/api/device/preview?${p}`
  }

  function getThumbnailUrl(item) {
    if (deviceSource) {
      const virtualPath = item.abs_path.replace(`ios://${deviceSource.device_id}`, '')
      const p = new URLSearchParams({
        device_id: deviceSource.device_id,
        path: virtualPath,
        size: String(THUMBNAIL_SIZE),
      })
      return `${API_BASE}/api/device/ios-thumbnail?${p}`
    }
    return `${API_BASE}/api/device/thumbnail?path=${encodeURIComponent(item.abs_path)}&size=${THUMBNAIL_SIZE}`
  }

  // Cancel and clean up on unmount or path change
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (abortRef.current) abortRef.current.abort()
    }
  }, [])

  // Fetch preview data when sourcePath, page, or sortBy changes
  useEffect(() => {
    if (!sourcePath && !deviceSource) {
      setItems([])
      setSelected(new Set())
      setFocusedIndex(null)
      setPage(1)
      setTotalPages(1)
      setLoading(false)
      return
    }

    let cancelled = false
    const controller = new AbortController()
    abortRef.current = controller

    setLoading(true)
    if (page === 1) {
      // Reset the thumb queue so stale callbacks from the old sort/page don't fire
      thumbQueueRef.current.reset()
      epochRef.current++
      setItems([])
      setFocusedIndex(null)
    }

    const url = getPreviewUrl(page, 100, sortBy)

    fetch(url, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error('Preview fetch failed')
        return res.json()
      })
      .then((data) => {
        if (cancelled) return
        setItems(prev => page === 1 ? (data.items || []) : [...prev, ...(data.items || [])])
        if (page === 1) setFocusedIndex(null)
        setMetadata({
          total: data.total || 0,
          photos: data.photos || 0,
          videos: data.videos || 0,
          total_size_bytes: data.total_size_bytes || 0,
        })
        setTotalPages(data.pages || 1)
        setLoading(false)
      })
      .catch((err) => {
        if (err.name === 'AbortError' || cancelled) return
        setLoading(false)
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourcePath, deviceSource?.device_id, deviceSource?.device_path, page, sortBy])

  // Pre-scan: check items against library by (filename, size) — no hashing
  useEffect(() => {
    if (items.length === 0) {
      setLikelyDupPaths(new Set())
      return
    }
    // Cap at first 2000 items to keep request fast
    const candidates = items.slice(0, 2000).map((item) => ({
      abs_path: item.abs_path,
      filename: item.filename,
      size_bytes: item.size_bytes,
    }))
    fetch(`${API_BASE}/api/duplicates/prescan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidates }),
    })
      .then((res) => {
        if (!res.ok) throw new Error('Prescan failed')
        return res.json()
      })
      .then((data) => {
        setLikelyDupPaths(new Set(data.likely_duplicate_paths || []))
      })
      .catch(() => {
        // Fail silently — this is a convenience feature, not a hard gate
      })
  }, [items])

  const toggleItem = useCallback((absPath) => {
    const next = new Set(selectedRef.current)
    if (next.has(absPath)) next.delete(absPath)
    else next.add(absPath)
    selectedRef.current = next
    setSelected(next)
    onSelectionConfirm?.(Array.from(next))
  }, [onSelectionConfirm])

  const visibleItems = useMemo(() => {
    if (filter === 'all') return items
    return items.filter((item) => item.type === filter)
  }, [items, filter])

  // Clamp focusedIndex when visibleItems shrinks (e.g. filter change)
  useEffect(() => {
    setFocusedIndex((prev) => {
      if (prev === null) return null
      return Math.min(prev, Math.max(0, visibleItems.length - 1))
    })
  }, [visibleItems.length])

  const allVisibleSelected = useMemo(() => {
    if (visibleItems.length === 0) return false
    return visibleItems.every((item) => selected.has(item.abs_path))
  }, [visibleItems, selected])

  const handleSelectAll = useCallback(() => {
    const next = new Set(selectedRef.current)
    if (allVisibleSelected) {
      for (const item of visibleItems) next.delete(item.abs_path)
    } else {
      for (const item of visibleItems) next.add(item.abs_path)
    }
    selectedRef.current = next
    setSelected(next)
    onSelectionConfirm?.(Array.from(next))
  }, [visibleItems, allVisibleSelected, onSelectionConfirm])

  const handleKeyDown = useCallback((e) => {
    if (visibleItems.length === 0) return

    if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A')) {
      e.preventDefault()
      handleSelectAll()
      return
    }

    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
      e.preventDefault()
      setFocusedIndex((prev) => {
        if (prev === null) return 0
        const COLS = GRID_COLUMNS
        if (e.key === 'ArrowRight') return (prev + 1) % visibleItems.length
        if (e.key === 'ArrowLeft') return (prev - 1 + visibleItems.length) % visibleItems.length
        if (e.key === 'ArrowDown') return (prev + COLS) % visibleItems.length
        if (e.key === 'ArrowUp') return (prev - COLS + visibleItems.length) % visibleItems.length
        return prev
      })
      return
    }

    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault()
      if (focusedIndex === null) return
      const item = visibleItems[focusedIndex]
      if (item) toggleItem(item.abs_path)
    }
  }, [visibleItems, handleSelectAll, toggleItem, focusedIndex])

  const handleGridBlur = useCallback((e) => {
    if (gridRef.current && !gridRef.current.contains(e.relatedTarget)) {
      setFocusedIndex(null)
    }
  }, [])

  // Scroll focused cell into view
  useEffect(() => {
    if (focusedIndex === null) return
    const cell = gridRef.current?.querySelector(`[data-cell-index="${focusedIndex}"]`)
    if (cell) cell.scrollIntoView({ block: 'nearest' })
  }, [focusedIndex])

  const selectedCount = selected.size
  const selectedBytes = totalSelectedSize(items, selected)

  if (!sourcePath && !deviceSource) return null

  return (
    <div className="space-y-2">
      {/* Header bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-foreground">Source preview</span>
          {!loading && (
            <span className="text-xs text-muted-foreground">
              {metadata.total} files &middot; {formatBytes(metadata.total_size_bytes)}
            </span>
          )}
        </div>
        {!loading && items.length > 0 && (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={handleSelectAll}
              className="text-xs font-medium text-primary hover:text-primary/80 transition-colors"
            >
              {allVisibleSelected ? 'Deselect all' : 'Select all'}
            </button>
            <span className="text-[11px] text-muted-foreground">(Ctrl+A)</span>
          </div>
        )}
      </div>

      {/* Filter pills + sort row */}
      {!loading && items.length > 0 && (
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1" role="tablist">
            {(['all', 'photo', 'video']).map((f) => (
              <button
                key={f}
                type="button"
                role="tab"
                aria-selected={filter === f}
                onClick={() => setFilter(f)}
                className={cn(
                  'px-2.5 py-1 rounded-full text-[11px] font-medium transition-colors',
                  filter === f
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-muted text-muted-foreground hover:bg-muted/80',
                )}
              >
                {f === 'all' && 'All'}
                {f === 'photo' && (
                  <span className="flex items-center gap-1">
                    <Image className="w-3 h-3" />
                    Photos
                  </span>
                )}
                {f === 'video' && (
                  <span className="flex items-center gap-1">
                    <Film className="w-3 h-3" />
                    Videos
                  </span>
                )}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 text-xs text-muted-foreground">
            <SlidersHorizontal className="w-3 h-3 shrink-0" />
            <select
              value={sortBy}
              onChange={(e) => {
                // Reset queue before switching sort so stale callbacks
                // from the previous sort don't run in the new batch.
                thumbQueueRef.current.reset()
                epochRef.current++
                setSortBy(e.target.value)
                setPage(1)
                setItems([])
              }}
              className="bg-transparent text-xs text-muted-foreground border-none outline-none cursor-pointer hover:text-foreground transition-colors"
            >
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="name_asc">Name A–Z</option>
              <option value="name_desc">Name Z–A</option>
              <option value="size_desc">Largest first</option>
              <option value="size_asc">Smallest first</option>
            </select>
          </div>
        </div>
      )}

      {/* Pre-scan banner */}
      {!loading && likelyDupPaths.size > 0 && (
        <div className="flex items-center gap-2 px-3 py-2 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 rounded-lg text-xs text-blue-700 dark:text-blue-300">
          <span className="flex-1">
            {likelyDupPaths.size} file{likelyDupPaths.size !== 1 ? 's' : ''} already appear to be in your library.
          </span>
          <button
            type="button"
            onClick={() => {
              const next = new Set(selectedRef.current)
              for (const item of visibleItems) {
                if (!likelyDupPaths.has(item.abs_path)) {
                  next.add(item.abs_path)
                } else {
                  next.delete(item.abs_path)
                }
              }
              selectedRef.current = next
              setSelected(next)
              onSelectionConfirm?.(Array.from(next))
            }}
            className="px-2 py-1 bg-blue-600 text-white rounded-md font-medium hover:bg-blue-700 transition-colors shrink-0"
          >
            Select only new files
          </button>
        </div>
      )}

      {/* Thumbnail grid area */}
      <AnimatePresence mode="wait">
        {loading && <SkeletonGrid key="skeleton" count={20} />}

        {!loading && items.length === 0 && (
          <motion.div
            key="empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <EmptyState />
          </motion.div>
        )}

        {!loading && items.length > 0 && (
          <motion.div
            key="grid"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div
              ref={gridRef}
              tabIndex={0}
              onKeyDown={handleKeyDown}
              onBlur={handleGridBlur}
              role="grid"
              aria-label={`Source preview — ${items.length} files`}
              className="grid grid-cols-4 gap-0.5 focus-visible:outline-none"
            >
              {visibleItems.map((item, index) => (
                <MediaThumbCell
                  key={`${sortBy}-${page}-${item.abs_path}`}
                  item={item}
                  isSelected={selected.has(item.abs_path)}
                  onToggle={() => { setFocusedIndex(index); gridRef.current?.focus(); toggleItem(item.abs_path) }}
                  isLikelyDuplicate={likelyDupPaths.has(item.abs_path)}
                  isFocused={index === focusedIndex}
                  cellIndex={index}
                  makeThumbnailUrl={getThumbnailUrl}
                  thumbQueue={thumbQueueRef.current}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Load more button */}
      {!loading && page < totalPages && (
        <button
          type="button"
          onClick={() => setPage(p => p + 1)}
          className="w-full py-2 text-xs font-medium text-primary hover:text-primary/80 transition-colors border border-border rounded-lg"
        >
          Load more ({metadata.total - items.length} remaining)
        </button>
      )}

      {/* Bottom action bar */}
      <div className="flex items-center justify-between pt-1">
        <div>
          <p className="text-xs font-medium text-foreground">
            {selectedCount > 0 ? `${selectedCount} selected` : '0 selected'}
          </p>
          <p className="text-xs text-muted-foreground">
            {selectedCount > 0
              ? `${formatBytes(selectedBytes)} to transfer`
              : 'Select files to transfer'}
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            const paths = Array.from(selected)
            onSelectionConfirm(paths)
            onTransferStart?.(paths)
          }}
          disabled={selectedCount === 0}
          className={cn(
            'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all',
            selectedCount > 0
              ? 'bg-action text-white hover:bg-action/90 active:scale-[0.95]'
              : 'bg-muted text-muted-foreground cursor-default opacity-40',
          )}
        >
          <Upload className="w-3.5 h-3.5" />
          Start transfer
        </button>
      </div>
    </div>
  )
}

export default function SourcePreviewPanel(props) {
  const [resetKey, setResetKey] = useState(0)
  return (
    <ErrorBoundary
      key={resetKey}
      fallback={<SourcePreviewFallback onRetry={() => setResetKey(k => k + 1)} />}
    >
      <SourcePreviewPanelInner key={resetKey} {...props} />
    </ErrorBoundary>
  )
}
