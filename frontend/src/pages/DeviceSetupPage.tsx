// ---------------------------------------------------------------------------
// MediaVault v2 — Device Setup Page
// Configuration selectors and directory destination target validation.
// ---------------------------------------------------------------------------

import { useState } from 'react'
import { motion } from 'framer-motion'
import {
  FolderOpen,
  FolderCheck,
  ChevronRight,
  Settings,
  HardDrive,
  FileImage,
  Film,
  Music,
  FileText,
} from 'lucide-react'
import { useConfig, useCreateSession } from '@/lib/queries'
import { useTransferStore } from '@/store/transfer'
import { cn } from '@/lib/utils'

interface FolderPickerProps {
  label: string
  value: string
  onChange: (path: string) => void
  placeholder?: string
}

function FolderPicker({ label, value, onChange, placeholder }: FolderPickerProps) {
  const handleBrowse = async () => {
    if (window.electronAPI) {
      const result = await window.electronAPI.showOpenDialog({
        title: `Select ${label}`,
        properties: ['openDirectory'],
      })
      if (!result.canceled && result.filePaths[0]) {
        onChange(result.filePaths[0])
      }
    } else {
      // Fallback for browser dev: prompt
      const input = prompt(`Enter ${label} path:`, value)
      if (input !== null) onChange(input)
    }
  }

  const isValid = value.trim().length > 0

  return (
    <div>
      <label className="text-sm font-medium text-foreground mb-1.5 block">{label}</label>
      <div className="flex gap-2">
        <div className="flex-1 relative">
          <input
            type="text"
            value={value}
            readOnly
            placeholder={placeholder ?? `Select ${label.toLowerCase()}...`}
            className={cn(
              'w-full px-3 py-2 bg-background border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring',
              isValid ? 'border-green-300' : 'border-input',
            )}
          />
          {isValid && (
            <FolderCheck className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-green-500" />
          )}
        </div>
        <button
          onClick={handleBrowse}
          className="no-drag px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors flex items-center gap-1.5"
        >
          <FolderOpen className="w-4 h-4" />
          Browse
        </button>
      </div>
    </div>
  )
}

function ExtensionBadges({ label, icon, extensions }: { label: string; icon: React.ReactNode; extensions: string[] }) {
  return (
    <div className="flex items-start gap-2">
      <div className="w-7 h-7 rounded bg-muted flex items-center justify-center flex-shrink-0 mt-0.5">
        {icon}
      </div>
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{label}</p>
        <div className="flex flex-wrap gap-1 mt-1">
          {extensions.slice(0, 8).map((ext) => (
            <span key={ext} className="px-1.5 py-0.5 bg-muted rounded text-[10px] text-muted-foreground font-mono">
              {ext}
            </span>
          ))}
          {extensions.length > 8 && (
            <span className="px-1.5 py-0.5 bg-muted rounded text-[10px] text-muted-foreground">
              +{extensions.length - 8}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

export default function DeviceSetupPage() {
  const [sourcePath, setSourcePath] = useState('')
  const [destPath, setDestPath] = useState('')
  const [sessionName, setSessionName] = useState('')

  const { data: config, isLoading: configLoading } = useConfig()
  const createSession = useCreateSession()
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const initTransfer = useTransferStore((s) => s.initTransfer)

  const canStart = sourcePath.trim().length > 0 && destPath.trim().length > 0 && !createSession.isPending

  const handleStart = async () => {
    const name = sessionName.trim() || `backup-${Date.now()}`
    const session = await createSession.mutateAsync({
      session_name: name,
      source_root: sourcePath,
      dest_root: destPath,
    })
    initTransfer(session)
    setCurrentPage('transfer')
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Device Setup</h1>
        <p className="text-sm text-muted-foreground mt-1">Configure source and destination for your backup</p>
      </div>

      {/* Directory Selection */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-card border border-border rounded-lg p-6 space-y-5"
      >
        <div className="flex items-center gap-2 mb-1">
          <Settings className="w-5 h-5 text-primary" />
          <h2 className="text-base font-semibold text-foreground">Directories</h2>
        </div>

        <FolderPicker
          label="Source Directory"
          value={sourcePath}
          onChange={setSourcePath}
          placeholder="Folder containing media to back up..."
        />

        <FolderPicker
          label="Destination Directory"
          value={destPath}
          onChange={setDestPath}
          placeholder="Where to store the backup archive..."
        />

        <div>
          <label className="text-sm font-medium text-foreground mb-1.5 block">Session Name (optional)</label>
          <input
            type="text"
            value={sessionName}
            onChange={(e) => setSessionName(e.target.value)}
            placeholder="My Backup"
            className="w-full px-3 py-2 bg-background border border-input rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
      </motion.div>

      {/* Supported Formats */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="bg-card border border-border rounded-lg p-6"
      >
        <h2 className="text-base font-semibold text-foreground mb-4 flex items-center gap-2">
          <HardDrive className="w-5 h-5 text-primary" />
          Supported Formats
        </h2>

        {configLoading ? (
          <div className="space-y-3">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="h-8 bg-muted rounded animate-pulse" />
            ))}
          </div>
        ) : config ? (
          <div className="space-y-4">
            <ExtensionBadges
              label="Images"
              icon={<FileImage className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.image_extensions}
            />
            <ExtensionBadges
              label="Video"
              icon={<Film className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.video_extensions}
            />
            <ExtensionBadges
              label="Audio"
              icon={<Music className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.audio_extensions}
            />
            <ExtensionBadges
              label="Documents"
              icon={<FileText className="w-3.5 h-3.5 text-muted-foreground" />}
              extensions={config.document_extensions}
            />
          </div>
        ) : null}

        {config && (
          <div className="mt-4 pt-3 border-t border-border flex items-center gap-4 text-xs text-muted-foreground">
            <span>Batch size: {config.batch_size}</span>
            <span>Max retries: {config.max_retry}</span>
            <span>Port: {config.port}</span>
          </div>
        )}
      </motion.div>

      {/* Start Button */}
      <motion.button
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        whileHover={canStart ? { scale: 1.01 } : undefined}
        whileTap={canStart ? { scale: 0.99 } : undefined}
        disabled={!canStart}
        onClick={handleStart}
        className={cn(
          'no-drag w-full flex items-center justify-center gap-2 px-6 py-3 rounded-lg text-sm font-semibold transition-colors',
          canStart
            ? 'bg-primary text-primary-foreground hover:bg-primary/90'
            : 'bg-muted text-muted-foreground cursor-not-allowed',
        )}
      >
        {createSession.isPending ? (
          <div className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
        ) : (
          <>
            Start Backup
            <ChevronRight className="w-4 h-4" />
          </>
        )}
      </motion.button>
    </div>
  )
}
