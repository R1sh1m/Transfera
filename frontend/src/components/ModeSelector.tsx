// ---------------------------------------------------------------------------
// Transfera v2 — Mode Selector
// Explicit Backup (copy) vs Space Saver (move) mode toggle.
// ---------------------------------------------------------------------------

import { motion } from 'framer-motion'
import { Copy, ArrowRightLeft, Shield, AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TransferMode } from '@/types/api'

interface ModeSelectorProps {
  value: TransferMode
  onChange: (mode: TransferMode) => void
}

const modes = [
  {
    id: 'copy' as const,
    label: 'Backup Mode',
    subtitle: 'Copy',
    icon: Copy,
    description: 'Safely copies files from your device into the archive destination. Source files remain completely untouched.',
    color: 'text-blue-600 dark:text-blue-400',
    activeBg: 'bg-blue-50 dark:bg-blue-950 border-blue-200 dark:border-blue-800',
    iconBg: 'bg-blue-100 dark:bg-blue-900',
  },
  {
    id: 'move' as const,
    label: 'Space Saver Mode',
    subtitle: 'Move',
    icon: ArrowRightLeft,
    description: 'Transfers files directly. Assets are deleted from source ONLY after verified two-stage byte hash confirmation.',
    color: 'text-amber-600 dark:text-amber-400',
    activeBg: 'bg-amber-50 dark:bg-amber-950 border-amber-200 dark:border-amber-800',
    iconBg: 'bg-amber-100 dark:bg-amber-900',
  },
] as const

export default function ModeSelector({ value, onChange }: ModeSelectorProps) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <label className="text-sm font-medium text-foreground">Transfer Mode</label>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {modes.map((mode) => {
          const isActive = value === mode.id
          const Icon = mode.icon
          return (
            <motion.button
              key={mode.id}
              type="button"
              whileHover={{ scale: 1.01 }}
              whileTap={{ scale: 0.99 }}
              onClick={() => onChange(mode.id)}
              className={cn(
                'no-drag relative text-left p-4 rounded-lg border-2 transition-colors',
                isActive
                  ? mode.activeBg
                  : 'border-border bg-card hover:bg-muted/50',
              )}
            >
              <div className="flex items-start gap-3">
                <div className={cn('w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0', isActive ? mode.iconBg : 'bg-muted')}>
                  <Icon className={cn('w-4.5 h-4.5', isActive ? mode.color : 'text-muted-foreground')} />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <p className={cn('text-sm font-semibold', isActive ? 'text-foreground' : 'text-foreground')}>
                      {mode.label}
                    </p>
                    <span className={cn(
                      'text-[10px] font-mono px-1.5 py-0.5 rounded',
                      isActive ? 'bg-foreground/10 text-foreground' : 'bg-muted text-muted-foreground',
                    )}>
                      {mode.subtitle}
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                    {mode.description}
                  </p>
                </div>
              </div>

              {/* Safety badge */}
              {mode.id === 'copy' && isActive && (
                <div className="flex items-center gap-1.5 mt-3 ml-12">
                  <Shield className="w-3 h-3 text-green-600 dark:text-green-400" />
                  <span className="text-[11px] text-green-700 dark:text-green-300 font-medium">
                    Zero risk — source files always preserved
                  </span>
                </div>
              )}

              {/* Warning badge for move mode */}
              {mode.id === 'move' && isActive && (
                <div className="flex items-center gap-1.5 mt-3 ml-12">
                  <AlertTriangle className="w-3 h-3 text-amber-600 dark:text-amber-400" />
                  <span className="text-[11px] text-amber-700 dark:text-amber-300 font-medium">
                    Source files removed after hash verification
                  </span>
                </div>
              )}
            </motion.button>
          )
        })}
      </div>
    </div>
  )
}
