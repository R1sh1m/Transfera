// ---------------------------------------------------------------------------
// Transfera v2 — Duplicate Modal
// Sheet modal for handling structural collisions with manual action selectors
// and bulk execution toggles.
// ---------------------------------------------------------------------------

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X,
  AlertTriangle,
  SkipForward,
  Trash2,
  Copy,
  Check,
  Layers,
  Eye,
} from 'lucide-react'
import { useTransferStore } from '@/store/transfer'
import { cn } from '@/lib/utils'
import type { DuplicateAction, DuplicateEntry } from '@/types/api'

const actionConfig: Record<DuplicateAction, { label: string; icon: React.ReactNode; color: string; bg: string }> = {
  skip: {
    label: 'Skip',
    icon: <SkipForward className="w-4 h-4" />,
    color: 'text-muted-foreground',
    bg: 'bg-muted hover:bg-muted/80',
  },
  overwrite: {
    label: 'Overwrite',
    icon: <Trash2 className="w-4 h-4" />,
    color: 'text-red-600 dark:text-red-400',
    bg: 'bg-red-50 dark:bg-red-950 hover:bg-red-100 dark:hover:bg-red-900',
  },
  keep_both: {
    label: 'Keep Both',
    icon: <Copy className="w-4 h-4" />,
    color: 'text-blue-600 dark:text-blue-400',
    bg: 'bg-blue-50 dark:bg-blue-950 hover:bg-blue-100 dark:hover:bg-blue-900',
  },
}

function ActionButton({
  action,
  active,
  onClick,
}: {
  action: DuplicateAction
  active: boolean
  onClick: () => void
}) {
  const c = actionConfig[action]
  return (
    <button
      onClick={onClick}
      className={cn(
        'no-drag inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors border',
        active
          ? `${c.bg} ${c.color} border-current`
          : 'bg-background text-muted-foreground border-border hover:bg-muted',
      )}
    >
      {c.icon}
      {c.label}
    </button>
  )
}

function DuplicateEntryRow({
  entry,
  resolution,
  onSetResolution,
}: {
  entry: DuplicateEntry
  resolution: DuplicateAction | undefined
  onSetResolution: (action: DuplicateAction) => void
}) {
  return (
    <div className="flex items-center gap-3 py-3 border-b border-border last:border-b-0">
      <div className="w-10 h-10 rounded bg-muted flex items-center justify-center flex-shrink-0">
        {entry.match_type === 'exact' ? (
          <AlertTriangle className="w-5 h-5 text-amber-500" />
        ) : (
          <Eye className="w-5 h-5 text-muted-foreground" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-foreground truncate">{entry.file_name}</p>
        <p className="text-xs text-muted-foreground truncate">{entry.source_path}</p>
        <div className="flex items-center gap-2 mt-1">
          <span className="text-[10px] px-1.5 py-0.5 bg-muted rounded text-muted-foreground">
            {entry.file_size > 1024 * 1024
              ? `${(entry.file_size / (1024 * 1024)).toFixed(1)} MB`
              : `${(entry.file_size / 1024).toFixed(0)} KB`}
          </span>
          <span className={cn(
            'text-[10px] px-1.5 py-0.5 rounded',
            entry.match_type === 'exact' ? 'bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300' : 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300',
          )}>
            {entry.match_type === 'exact' ? 'Exact Match' : 'Potential Match'}
          </span>
          {entry.matched_path && (
            <span className="text-[10px] text-muted-foreground truncate max-w-[200px]">
              matches: {entry.matched_path}
            </span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-1.5 flex-shrink-0">
        <ActionButton action="skip" active={resolution === 'skip'} onClick={() => onSetResolution('skip')} />
        <ActionButton action="overwrite" active={resolution === 'overwrite'} onClick={() => onSetResolution('overwrite')} />
        <ActionButton action="keep_both" active={resolution === 'keep_both'} onClick={() => onSetResolution('keep_both')} />
      </div>
    </div>
  )
}

export default function DuplicateModal() {
  const { isOpen, report, resolutions, applyToAll } = useTransferStore((s) => s.duplicates)
  const setResolution = useTransferStore((s) => s.setResolution)
  const setApplyToAll = useTransferStore((s) => s.setApplyToAll)
  const closeDuplicates = useTransferStore((s) => s.closeDuplicates)
  const clearResolutions = useTransferStore((s) => s.clearResolutions)

  const [activeBulkAction, setActiveBulkAction] = useState<DuplicateAction | null>(null)

  if (!isOpen || !report) return null

  const allEntries = [...report.exact_duplicates, ...report.potential_duplicates]
  const resolvedCount = resolutions.size
  const totalCount = allEntries.length
  const allResolved = resolvedCount === totalCount && totalCount > 0

  const handleBulkApply = () => {
    if (!activeBulkAction) return
    clearResolutions()
    setApplyToAll(activeBulkAction)
    for (const entry of allEntries) {
      setResolution(entry.item_id, activeBulkAction)
    }
  }

  const handleConfirm = () => {
    // In production this would POST resolutions to backend
    console.log('Duplicate resolutions:', Object.fromEntries(resolutions))
    closeDuplicates()
  }

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={closeDuplicates}
            className="fixed inset-0 z-50 bg-black/50"
          />

          {/* Modal */}
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 20 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
          >
            <div className="bg-card border border-border rounded-lg shadow-xl w-full max-w-2xl max-h-[80vh] flex flex-col">
              {/* Header */}
              <div className="flex items-center justify-between px-5 py-4 border-b border-border">
                <div className="flex items-center gap-3">
                  <div className="w-9 h-9 rounded-lg bg-amber-100 dark:bg-amber-900 flex items-center justify-center">
                    <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400" />
                  </div>
                  <div>
                    <h2 className="text-base font-semibold text-foreground">Duplicate Files Detected</h2>
                    <p className="text-xs text-muted-foreground">{report.summary}</p>
                  </div>
                </div>
                <button
                  onClick={closeDuplicates}
                  className="p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>

              {/* Bulk Actions */}
              <div className="px-5 py-3 border-b border-border bg-muted/30">
                <div className="flex items-center gap-3">
                  <Layers className="w-4 h-4 text-muted-foreground" />
                  <span className="text-xs font-medium text-foreground">Bulk Action:</span>
                  <div className="flex items-center gap-1.5">
                    {(['skip', 'overwrite', 'keep_both'] as const).map((action) => (
                      <button
                        key={action}
                        onClick={() => setActiveBulkAction(action)}
                        className={cn(
                          'no-drag px-2.5 py-1 rounded text-xs font-medium border transition-colors',
                          activeBulkAction === action
                            ? 'bg-primary text-primary-foreground border-primary'
                            : 'bg-background text-muted-foreground border-border hover:bg-muted',
                        )}
                      >
                        {actionConfig[action].label}
                      </button>
                    ))}
                  </div>
                  <button
                    onClick={handleBulkApply}
                    disabled={!activeBulkAction}
                    className="no-drag px-3 py-1 bg-primary text-primary-foreground rounded text-xs font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Apply to All
                  </button>
                </div>
              </div>

              {/* Entry List */}
              <div className="flex-1 overflow-y-auto px-5 py-2">
                {allEntries.map((entry) => (
                  <DuplicateEntryRow
                    key={entry.item_id}
                    entry={entry}
                    resolution={resolutions.get(entry.item_id) ?? applyToAll ?? undefined}
                    onSetResolution={(action) => setResolution(entry.item_id, action)}
                  />
                ))}
              </div>

              {/* Footer */}
              <div className="flex items-center justify-between px-5 py-3 border-t border-border">
                <span className="text-xs text-muted-foreground">
                  {resolvedCount} / {totalCount} resolved
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
                    disabled={!allResolved}
                    className={cn(
                      'no-drag inline-flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors',
                      allResolved
                        ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                        : 'bg-muted text-muted-foreground cursor-not-allowed',
                    )}
                  >
                    <Check className="w-4 h-4" />
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
