import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Check, ImageOff, Upload, Image, Film, SlidersHorizontal } from 'lucide-react'
import { CheckCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

const API_BASE = window.location.origin || 'http://127.0.0.1:47821'
const THUMBNAIL_SIZE = 200
const GRID_COLUMNS = 4

// --- Thumbnail request queue (module-level, shared across all cells) ---
const _thumbQueue = []
let _activeThumbRequests = 0
const _MAX_CONCURRENT_THUMBS = 4

function _requestThumbSlot(onGranted) {
  if (_activeThumbRequests < _MAX_CONCURRENT_THUMBS) {
    _activeThumbRequests++
    onGranted()
  } else {
    _thumbQueue.push(onGranted)
  }
}

function _releaseThumbSlot() {
  _activeThumbRequests--
  if (_thumbQueue.length > 0) {
    const next = _thumbQueue.shift()
    _activeThumbRequests++
    next()
  }
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

function MediaThumbCell({ item, isSelected, onToggle, isLikelyDuplicate, isActive, makeThumbnailUrl }) {
  const cellRef = useRef(null)
  const [loadState, setLoadState] = useState('none') // none | loading | loaded | error
  const [imgSrc, setImgSrc] = useState(null)
  const retryCountRef = useRef(0)
  const retryTimerRef = useRef(null)
  const slotGrantedRef = useRef(false)

  // IntersectionObserver: request a thumbnail slot when cell enters viewport
  useEffect(() => {
    const el = cellRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          if (loadState === 'none' && !slotGrantedRef.current) {
            slotGrantedRef.current = true
            const url = makeThumbnailUrl(item)
            _requestThumbSlot(() => {
              setImgSrc(url)
              setLoadState('loading')
            })
          }
          observer.unobserve(el)
        }
      },
      { rootMargin: '200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [item, makeThumbnailUrl, loadState])

  // Cleanup retry timer on unmount
  useEffect(() => {
    return () => {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current)
    }
  }, [])

  const handleLoad = () => {
    setLoadState('loaded')
    _releaseThumbSlot()
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
      setLoadState('error')
      _releaseThumbSlot()
    }
  }

  return (
    <div
      ref={cellRef}
      role="button"
      tabIndex={0}
      onClick={onToggle}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onToggle() }}
      className={cn(
        'relative aspect-square rounded-lg overflow-hidden cursor-pointer group bg-muted',
        isActive && 'ring-2 ring-action ring-offset-1',
      )}
    >
      {/* Loading shimmer */}
      {loadState === 'loading' && (
        <div className="absolute inset-0 bg-muted">
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent bg-[length:200%_100%] animate-[shimmer_1.5s_infinite]" />
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
            className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent bg-[length:200%_100%] animate-[shimmer_1.5s_infinite]"
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

export default function SourcePreviewPanel({
  sourcePath,
  deviceSource,
  onSelectionConfirm,
}) {
  const [items, setItems] = useState([])
  const [selected, setSelected] = useState(new Set())
  const [filter, setFilter] = useState('all')
  const [loading, setLoading] = useState(false)
  const [metadata, setMetadata] = useState({ total: 0, photos: 0, videos: 0, total_size_bytes: 0 })
  const [likelyDupPaths, setLikelyDupPaths] = useState(new Set())
  const [activeIndex, setActiveIndex] = useState(0)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [sortBy, setSortBy] = useState('newest')

  const abortRef = useRef(null)
  const mountedRef = useRef(true)

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
      setActiveIndex(0)
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
      setItems([])
      setSelected(new Set())
      setActiveIndex(0)
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
        setActiveIndex(0)
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
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(absPath)) {
        next.delete(absPath)
      } else {
        next.add(absPath)
      }
      return next
    })
  }, [])

  const visibleItems = useMemo(() => {
    if (filter === 'all') return items
    return items.filter((item) => item.type === filter)
  }, [items, filter])

  // Clamp activeIndex when visibleItems shrinks (e.g. filter change)
  useEffect(() => {
    setActiveIndex((prev) => Math.min(prev, Math.max(0, visibleItems.length - 1)))
  }, [visibleItems.length])

  const allVisibleSelected = useMemo(() => {
    if (visibleItems.length === 0) return false
    return visibleItems.every((item) => selected.has(item.abs_path))
  }, [visibleItems, selected])

  const handleSelectAll = useCallback(() => {
    if (allVisibleSelected) {
      setSelected((prev) => {
        const next = new Set(prev)
        for (const item of visibleItems) next.delete(item.abs_path)
        return next
      })
    } else {
      setSelected((prev) => {
        const next = new Set(prev)
        for (const item of visibleItems) next.add(item.abs_path)
        return next
      })
    }
  }, [visibleItems, allVisibleSelected])

  // Keyboard navigation and global shortcuts
  useEffect(() => {
    if (!sourcePath && !deviceSource) return
    const handler = (e) => {
      if (visibleItems.length === 0) return

      if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A')) {
        e.preventDefault()
        handleSelectAll()
        return
      }

      if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
        e.preventDefault()
        setActiveIndex((prev) => {
          let next = prev
          if (e.key === 'ArrowUp') next = prev - GRID_COLUMNS
          if (e.key === 'ArrowDown') next = prev + GRID_COLUMNS
          if (e.key === 'ArrowLeft') next = prev - 1
          if (e.key === 'ArrowRight') next = prev + 1
          return Math.max(0, Math.min(next, visibleItems.length - 1))
        })
        return
      }

      if (e.key === ' ' || e.key === 'Enter') {
        if (e.target?.getAttribute?.('role') === 'button') return
        e.preventDefault()
        const item = visibleItems[activeIndex]
        if (item) toggleItem(item.abs_path)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [sourcePath, deviceSource, visibleItems, handleSelectAll, toggleItem, activeIndex])

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
          <div className="flex items-center gap-1">
            {(['all', 'photo', 'video']).map((f) => (
              <button
                key={f}
                type="button"
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
              onChange={(e) => { setSortBy(e.target.value); setPage(1); setItems([]); }}
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
              setSelected((prev) => {
                const next = new Set(prev)
                for (const item of visibleItems) {
                  if (!likelyDupPaths.has(item.abs_path)) {
                    next.add(item.abs_path)
                  } else {
                    next.delete(item.abs_path)
                  }
                }
                return next
              })
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
            className="grid grid-cols-4 gap-0.5"
          >
            {visibleItems.map((item, index) => (
              <MediaThumbCell
                key={item.id}
                item={item}
                isSelected={selected.has(item.abs_path)}
                onToggle={() => toggleItem(item.abs_path)}
                isLikelyDuplicate={likelyDupPaths.has(item.abs_path)}
                isActive={index === activeIndex}
                makeThumbnailUrl={getThumbnailUrl}
              />
            ))}
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
          onClick={() => onSelectionConfirm(Array.from(selected))}
          disabled={selectedCount === 0}
          className={cn(
            'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all',
            selectedCount > 0
              ? 'bg-action text-white hover:bg-action/90 active:scale-[0.95]'
              : 'bg-muted text-muted-foreground cursor-default opacity-40',
          )}
        >
          <Upload className="w-3.5 h-3.5" />
          Transfer selected
        </button>
      </div>
    </div>
  )
}
