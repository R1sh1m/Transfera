// ---------------------------------------------------------------------------
// Transfera v2 — Duplicate Review Modal
// Side-by-side thumbnail comparison for flagged duplicate pairs.
// Shows new incoming item vs. matched library item with metadata highlighting,
// per-item resolution actions, progress tracking, and bulk "apply to remaining".
// ---------------------------------------------------------------------------

import { useState, useMemo, useCallback, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X,
  AlertTriangle,
  SkipForward,
  Trash2,
  Copy,
  Check,
  Layers,
  Loader2,
  ChevronLeft,
  ChevronRight,
  FileImage,
  HardDrive,
} from 'lucide-react'
import { useTransferStore } from '@/store/transfer'
import { useResolveDuplicates } from '@/lib/queries'
import { fetchThumbnail } from '@/lib/thumbnail-fetch'
import { cn } from '@/lib/utils'
import type { DuplicateAction, DuplicateEntry } from '@/types/api'

// ---------------------------------------------------------------------------
// Action config
// ---------------------------------------------------------------------------
const actionConfig: Record<DuplicateAction, { label: string; icon: React.ReactNode; color: string; bg: string; border: string }> = {
  skip: {
    label: 'Skip',
    icon: <SkipForward className="w-3.5 h-3.5" />,
    color: 'text-muted-foreground',
    bg: 'bg-muted hover:bg-muted/80',
    border: 'border-border',
  },
  overwrite: {
    label: 'Overwrite',
    icon: <Trash2 className="w-3.5 h-3.5" />,
    color: 'text-red-600 dark:text-red-400',
    bg: 'bg-red-50 dark:bg-red-950 hover:bg-red-100 dark:hover:bg-red-900',
    border: 'border-red-200 dark:border-red-800',
  },
  keep_both: {
    label: 'Import Anyway',
    icon: <Copy className="w-3.5 h-3.5" />,
    color: 'text-blue-600 dark:text-blue-400',
    bg: 'bg-blue-50 dark:bg-blue-950 hover:bg-blue-100 dark:hover:bg-blue-900',
    border: 'border-blue-200 dark:border-blue-800',
  },
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatBytes(bytes: number): string {
  if (bytes > 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes > 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
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

function truncateFilename(name: string, maxLen = 28): string {
  if (name.length <= maxLen) return name
  const ext = name.lastIndexOf('.')
  if (ext > 0) {
    const base = name.slice(0, ext)
    const extension = name.slice(ext)
    const available = maxLen - extension.length - 3
    if (available > 4) return base.slice(0, available) + '...' + extension
  }
  return name.slice(0, maxLen - 3) + '...'
}

// ---------------------------------------------------------------------------
// ActionButton
// ---------------------------------------------------------------------------
function ActionButton({
  action,
  active,
  onClick,
  size = 'sm',
}: {
  action: DuplicateAction
  active: boolean
  onClick: () => void
  size?: 'sm' | 'md'
}) {
  const c = actionConfig[action]
  return (
    <button
      onClick={onClick}
      className={cn(
        'no-drag inline-flex items-center gap-1.5 rounded-md font-medium transition-colors border',
        size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-sm',
        active
          ? `${c.bg} ${c.color} border-current`
          : `bg-background text-muted-foreground ${c.border} hover:bg-muted`,
      )}
    >
      {c.icon}
      {c.label}
    </button>
  )
}

// ---------------------------------------------------------------------------
// ThumbnailImage — renders thumbnail or placeholder icon
// ---------------------------------------------------------------------------
function ThumbnailImage({
  mediaId,
  fileName,
  size = 'md',
}: {
  mediaId?: number | null
  fileName: string
  size?: 'md' | 'lg'
}) {
  const dim = size === 'lg' ? 'w-36 h-36' : 'w-28 h-28'
  const iconSize = size === 'lg' ? 'w-8 h-8' : 'w-6 h-6'
  const [thumbUrl, setThumbUrl] = useState<string | null>(null)
  const [noThumb, setNoThumb] = useState(false)

  useEffect(() => {
    setThumbUrl(null)
    setNoThumb(false)
    if (!mediaId) return

    let cancelled = false
    const controller = new AbortController()

    fetchThumbnail(mediaId, controller.signal).then((url) => {
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
  }, [mediaId])

  useEffect(() => {
    return () => {
      if (thumbUrl) {
        URL.revokeObjectURL(thumbUrl)
      }
    }
  }, [thumbUrl])

  if (noThumb || !thumbUrl || !mediaId) {
    return (
      <div className={cn(dim, 'rounded-lg bg-muted flex items-center justify-center shrink-0')}>
        <FileImage className={cn(iconSize, 'text-muted-foreground/50')} />
      </div>
    )
  }

  return (
    <div className={cn(dim, 'rounded-lg bg-muted shrink-0 overflow-hidden relative')}>
      <img
        src={thumbUrl}
        alt={fileName}
        className="w-full h-full object-cover"
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// DiffBadge — highlights metadata differences between new and matched items
// ---------------------------------------------------------------------------
function DiffBadge({ label, newval, matched }: { label: string; newval: string; matched: string }) {
  const isDifferent = newval !== matched
  return (
    <div className="flex items-center gap-1.5 text-[11px]">
      <span className="text-muted-foreground">{label}</span>
      {isDifferent ? (
        <span className="px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 font-medium">
          {newval} vs {matched}
        </span>
      ) : (
        <span className="text-foreground">{newval}</span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// DuplicatePairCard — side-by-side comparison for one flagged pair
// ---------------------------------------------------------------------------
function DuplicatePairCard({
  entry,
  resolution,
  onSetResolution,
  isReviewed,
}: {
  entry: DuplicateEntry
  resolution: DuplicateAction | undefined
  onSetResolution: (action: DuplicateAction) => void
  isReviewed: boolean
}) {
  const matchReason = entry.match_type === 'exact'
    ? 'Same content (identical hash & size)'
    : 'Same filename, different content'

  return (
    <div className={cn(
      'border rounded-lg p-4 transition-colors',
      isReviewed
        ? 'border-green-200 dark:border-green-800 bg-green-50/30 dark:bg-green-950/20'
        : 'border-border bg-card',
    )}>
      {/* Header: match type + match reason */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={cn(
            'text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wider',
            entry.match_type === 'exact'
              ? 'bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300'
              : 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300',
          )}>
            {entry.match_type === 'exact' ? 'Exact Match' : 'Potential Match'}
          </span>
          <span className="text-[11px] text-muted-foreground">{matchReason}</span>
        </div>
        {isReviewed && (
          <span className="text-[10px] font-medium text-green-600 dark:text-green-400 flex items-center gap-1">
            <Check className="w-3 h-3" /> Reviewed
          </span>
        )}
      </div>

      {/* Side-by-side comparison */}
      <div className="flex gap-4">
        {/* New incoming item */}
        <div className="flex-1 min-w-0">
          <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
            Incoming
          </p>
          <div className="flex gap-3">
            <ThumbnailImage
              mediaId={entry.item_id}
              fileName={entry.file_name}
            />
            <div className="flex-1 min-w-0 space-y-1">
              <p className="text-sm font-medium text-foreground truncate" title={entry.file_name}>
                {truncateFilename(entry.file_name)}
              </p>
              <p className="text-[11px] text-muted-foreground truncate" title={entry.source_path}>
                {entry.source_path.length > 40 ? '...' + entry.source_path.slice(-37) : entry.source_path}
              </p>
              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <HardDrive className="w-3 h-3" />
                {formatBytes(entry.file_size)}
              </div>
            </div>
          </div>
        </div>

        {/* Divider with arrow */}
        <div className="flex flex-col items-center justify-center px-1">
          <div className="w-px h-6 bg-border" />
          <div className="w-6 h-6 rounded-full bg-muted flex items-center justify-center my-1">
            <AlertTriangle className="w-3 h-3 text-muted-foreground" />
          </div>
          <div className="w-px h-6 bg-border" />
        </div>

        {/* Existing library item */}
        <div className="flex-1 min-w-0">
          <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
            In Library
          </p>
          <div className="flex gap-3">
            <ThumbnailImage
              mediaId={entry.matched_item_id}
              fileName={entry.file_name}
            />
            <div className="flex-1 min-w-0 space-y-1">
              <p className="text-sm font-medium text-foreground truncate" title={entry.file_name}>
                {truncateFilename(entry.file_name)}
              </p>
              {entry.matched_path && (
                <p className="text-[11px] text-muted-foreground truncate" title={entry.matched_path}>
                  {entry.matched_path.length > 40 ? '...' + entry.matched_path.slice(-37) : entry.matched_path}
                </p>
              )}
              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <HardDrive className="w-3 h-3" />
                {formatBytes(entry.matched_file_size ?? 0)}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Metadata diff line */}
      <div className="mt-3 pt-3 border-t border-border flex flex-wrap gap-x-4 gap-y-1">
        <DiffBadge
          label="Size:"
          newval={formatBytes(entry.file_size)}
          matched={formatBytes(entry.matched_file_size ?? 0)}
        />
        <DiffBadge
          label="Date:"
          newval={formatDate(entry.source_path.includes('/') ? undefined : undefined)}
          matched={formatDate(entry.matched_date_taken)}
        />
      </div>

      {/* Action buttons */}
      <div className="mt-3 flex items-center gap-2">
        <ActionButton action="skip" active={resolution === 'skip'} onClick={() => onSetResolution('skip')} />
        <ActionButton action="overwrite" active={resolution === 'overwrite'} onClick={() => onSetResolution('overwrite')} />
        <ActionButton action="keep_both" active={resolution === 'keep_both'} onClick={() => onSetResolution('keep_both')} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// DuplicateModal
// ---------------------------------------------------------------------------
export default function DuplicateModal() {
  const { isOpen, report, resolutions, applyToAll } = useTransferStore((s) => s.duplicates)
  const setResolution = useTransferStore((s) => s.setResolution)
  const setApplyToAll = useTransferStore((s) => s.setApplyToAll)
  const closeDuplicates = useTransferStore((s) => s.closeDuplicates)
  const clearResolutions = useTransferStore((s) => s.clearResolutions)
  const resolveDuplicates = useResolveDuplicates()

  const [activeBulkAction, setActiveBulkAction] = useState<DuplicateAction | null>(null)
  const [currentViewIdx, setCurrentViewIdx] = useState(0)

  if (!isOpen || !report) return null

  const resolutionsMap = resolutions instanceof Map ? resolutions : new Map<number, DuplicateAction>()

  const allEntries = [...report.exact_duplicates, ...report.potential_duplicates]
  const resolvedCount = resolutionsMap.size
  const totalCount = allEntries.length
  const allResolved = resolvedCount === totalCount && totalCount > 0

  // Current entry for focused view
  const currentEntry = allEntries[currentViewIdx]

  const handleBulkApply = () => {
    if (!activeBulkAction) return
    clearResolutions()
    setApplyToAll(activeBulkAction)
    for (const entry of allEntries) {
      setResolution(entry.item_id, activeBulkAction)
    }
  }

  const handleApplyToRemaining = () => {
    if (!activeBulkAction) return
    for (let i = currentViewIdx; i < allEntries.length; i++) {
      const entry = allEntries[i]
      if (entry && !resolutionsMap.has(entry.item_id)) {
        setResolution(entry.item_id, activeBulkAction)
      }
    }
  }

  const handleConfirm = async () => {
    if (!report) return
    const resolutionList = allEntries.map((entry) => ({
      item_id: entry.item_id,
      action: resolutionsMap.get(entry.item_id) ?? applyToAll ?? ('skip' as DuplicateAction),
    }))
    await resolveDuplicates.mutateAsync({
      sessionId: report.session_id,
      batchId: report.batch_id,
      resolutions: resolutionList,
    })
    closeDuplicates()
  }

  const handlePrev = useCallback(() => {
    setCurrentViewIdx((i) => Math.max(0, i - 1))
  }, [])

  const handleNext = useCallback(() => {
    setCurrentViewIdx((i) => Math.min(allEntries.length - 1, i + 1))
  }, [allEntries.length])

  // Compute remaining unreviewed count
  const remainingCount = useMemo(() => {
    return allEntries.filter((e) => !resolutionsMap.has(e.item_id)).length
  }, [allEntries, resolutionsMap])

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-40 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4"
          />

          {/* Modal Container */}
          <motion.div
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            className="fixed inset-0 z-50 flex items-center justify-center pointer-events-none"
          >
            <div className="bg-card border border-border w-full max-w-4xl h-[640px] rounded-xl shadow-2xl flex flex-col overflow-hidden pointer-events-auto">
              {/* Header */}
              <div className="flex items-center justify-between px-5 py-4 border-b border-border bg-muted/30">
                <div className="flex items-center gap-2">
                  <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0" />
                  <div>
                    <h2 className="text-base font-semibold text-foreground leading-none">Review Duplicate Items</h2>
                    <p className="text-xs text-muted-foreground mt-1">
                      {report.summary}
                    </p>
                  </div>
                </div>
                <button
                  onClick={closeDuplicates}
                  className="no-drag p-1 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>

              {/* Bulk Actions */}
              <div className="px-5 py-3 border-b border-border bg-muted/10 flex items-center gap-3">
                <span className="text-xs font-medium text-muted-foreground shrink-0">Bulk Action:</span>
                <div className="flex items-center gap-1.5">
                  {(['skip', 'overwrite', 'keep_both'] as DuplicateAction[]).map((action) => (
                    <button
                      key={action}
                      onClick={() => setActiveBulkAction(action)}
                      className={cn(
                        'no-drag px-2.5 py-1.5 rounded-md text-xs font-medium border transition-all flex items-center gap-1.5',
                        activeBulkAction === action
                          ? cn(actionConfig[action].bg, actionConfig[action].border, actionConfig[action].color)
                          : 'border-input hover:bg-muted text-muted-foreground hover:text-foreground',
                      )}
                    >
                      {actionConfig[action].icon}
                      {actionConfig[action].label}
                    </button>
                  ))}
                </div>

                {activeBulkAction && (
                  <button
                    onClick={handleBulkApply}
                    className="no-drag ml-auto inline-flex items-center gap-1.5 px-3 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:bg-primary/90 transition-colors shadow-xs"
                  >
                    Apply to All
                  </button>
                )}
              </div>

              {/* Content: Side-by-side comparison */}
              <div className="flex-1 overflow-y-auto px-5 py-4">
                {/* Navigation arrows + current card */}
                <div className="flex items-start gap-3">
                  {/* Prev arrow */}
                  <button
                    onClick={handlePrev}
                    disabled={currentViewIdx === 0}
                    className="no-drag mt-32 p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    <ChevronLeft className="w-5 h-5" />
                  </button>

                  {/* Current pair card */}
                  <div className="flex-1">
                    {currentEntry && (
                      <DuplicatePairCard
                        key={currentEntry.item_id}
                        entry={currentEntry}
                        resolution={resolutionsMap.get(currentEntry.item_id) ?? applyToAll ?? undefined}
                        onSetResolution={(action) => setResolution(currentEntry.item_id, action)}
                        isReviewed={resolutionsMap.has(currentEntry.item_id)}
                      />
                    )}

                    {/* Navigation dots */}
                    <div className="flex items-center justify-center gap-1.5 mt-4">
                      {allEntries.map((entry, idx) => (
                        <button
                          key={entry.item_id}
                          onClick={() => setCurrentViewIdx(idx)}
                          className={cn(
                            'w-2 h-2 rounded-full transition-colors',
                            idx === currentViewIdx
                              ? 'bg-primary'
                              : resolutionsMap.has(entry.item_id)
                                ? 'bg-green-400 dark:bg-green-600'
                                : 'bg-muted-foreground/30 hover:bg-muted-foreground/50',
                          )}
                          title={`Item ${idx + 1}${resolutionsMap.has(entry.item_id) ? ' (reviewed)' : ''}`}
                        />
                      ))}
                    </div>
                  </div>

                  {/* Next arrow */}
                  <button
                    onClick={handleNext}
                    disabled={currentViewIdx >= allEntries.length - 1}
                    className="no-drag mt-32 p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    <ChevronRight className="w-5 h-5" />
                  </button>
                </div>

                {/* Apply to remaining (shown when there are unresolved items ahead) */}
                {activeBulkAction && remainingCount > 0 && (
                  <div className="mt-4 flex items-center justify-center">
                    <button
                      onClick={handleApplyToRemaining}
                      className="no-drag inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium border border-dashed border-muted-foreground/30 text-muted-foreground hover:bg-muted hover:border-muted-foreground/50 transition-colors"
                    >
                      <Layers className="w-3 h-3" />
                      Apply "{actionConfig[activeBulkAction].label}" to {remainingCount} remaining item{remainingCount !== 1 ? 's' : ''}
                    </button>
                  </div>
                )}
              </div>

              {/* Footer */}
              <div className="flex items-center justify-between px-5 py-3 border-t border-border">
                <span className="text-xs text-muted-foreground">
                  {allEntries.length === 1 ? '1 duplicate' : `${allEntries.length} duplicates`} to review
                </span>
                <div className="flex items-center gap-2">
                  <button
                    onClick={closeDuplicates}
                    className="no-drag px-4 py-2 bg-secondary text-secondary-foreground rounded-md text-sm font-medium hover:bg-secondary/80 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleConfirm}
                    disabled={!allResolved || resolveDuplicates.isPending}
                    className={cn(
                      'no-drag inline-flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors',
                      allResolved && !resolveDuplicates.isPending
                        ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                        : 'bg-muted text-muted-foreground cursor-not-allowed',
                    )}
                  >
                    {resolveDuplicates.isPending ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Check className="w-4 h-4" />
                    )}
                    Confirm
                  </button>
                </div>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
