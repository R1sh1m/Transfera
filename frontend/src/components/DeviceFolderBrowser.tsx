// ---------------------------------------------------------------------------
// Transfera v2 — Device Folder Browser
// Virtual folder browser for connected devices (e.g. iPhone via AFC).
// Fetches folder structure through the device API, not the native OS dialog.
// Handles device disconnect gracefully.
// ---------------------------------------------------------------------------

import { useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Smartphone,
  FolderOpen,
  Folder,
  ChevronRight,
  ArrowLeft,
  Loader2,
  AlertTriangle,
  WifiOff,
  Image,
  Film,
  Music,
  FileText,
  RefreshCw,
  Usb,
  Wifi,
  HardDrive,
} from 'lucide-react'
import { useIOSBrowse } from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { IOSDeviceInfo, IOSDeviceFileEntry } from '@/types/api'

// ---------------------------------------------------------------------------
// File icon helper
// ---------------------------------------------------------------------------
function fileIcon(entry: IOSDeviceFileEntry) {
  if (entry.is_dir) return <Folder className="w-5 h-5 text-blue-400" />
  const ext = entry.name.split('.').pop()?.toLowerCase() ?? ''
  if (['jpg', 'jpeg', 'png', 'gif', 'heic', 'raw', 'tiff', 'webp', 'bmp'].includes(ext))
    return <Image className="w-5 h-5 text-blue-400" />
  if (['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', '3gp'].includes(ext))
    return <Film className="w-5 h-5 text-purple-400" />
  if (['mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a', 'wma'].includes(ext))
    return <Music className="w-5 h-5 text-green-400" />
  if (['pdf', 'doc', 'docx', 'txt', 'md', 'rtf'].includes(ext))
    return <FileText className="w-5 h-5 text-orange-400" />
  return <FileText className="w-5 h-5 text-muted-foreground" />
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

// ---------------------------------------------------------------------------
// Breadcrumb path navigation
// ---------------------------------------------------------------------------
function Breadcrumb({
  path,
  onNavigate,
}: {
  path: string
  onNavigate: (path: string) => void
}) {
  const parts = path.split('/').filter(Boolean)
  const crumbs: { label: string; path: string }[] = [{ label: 'Root', path: '/' }]
  let current = ''
  for (const part of parts) {
    current += `/${part}`
    crumbs.push({ label: part, path: current })
  }

  return (
    <div className="flex items-center gap-1 text-xs text-muted-foreground overflow-x-auto">
      {crumbs.map((crumb, i) => (
        <span key={crumb.path} className="flex items-center gap-1">
          {i > 0 && <ChevronRight className="w-3 h-3 shrink-0" />}
          <button
            type="button"
            onClick={() => onNavigate(crumb.path)}
            className={cn(
              'px-1 py-0.5 rounded hover:bg-muted transition-colors whitespace-nowrap',
              i === crumbs.length - 1
                ? 'text-foreground font-medium'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {crumb.label}
          </button>
        </span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// DeviceFolderBrowserProps
// ---------------------------------------------------------------------------
interface DeviceFolderBrowserProps {
  device: IOSDeviceInfo
  onSelectPath: (devicePath: string) => void
  onBack: () => void
}

// ---------------------------------------------------------------------------
// DeviceFolderBrowser
// ---------------------------------------------------------------------------
export function DeviceFolderBrowser({
  device,
  onSelectPath,
  onBack,
}: DeviceFolderBrowserProps) {
  const [currentPath, setCurrentPath] = useState('/DCIM')
  const [selectionMode, setSelectionMode] = useState<'folder' | 'file'>('folder')

  const { data, isLoading, isError, error, refetch } = useIOSBrowse(
    device.serial,
    currentPath,
  )

  // Check if this is a disconnect error
  const isDisconnected = isError && (
    error?.message?.includes('not found') ||
    error?.message?.includes('Device not found') ||
    error?.message?.includes('not connected') ||
    (error as any)?.response?.status === 404
  )

  // Filter entries: show only directories in folder mode, everything in file mode
  const entries = data?.entries ?? []
  const visibleEntries = selectionMode === 'folder'
    ? entries.filter(e => e.is_dir)
    : entries

  // Sort: directories first, then files, alphabetically
  const sortedEntries = [...visibleEntries].sort((a, b) => {
    if (a.is_dir && !b.is_dir) return -1
    if (!a.is_dir && b.is_dir) return 1
    return a.name.localeCompare(b.name)
  })

  const handleNavigate = useCallback((path: string) => {
    setCurrentPath(path)
  }, [])

  const handleEntryClick = useCallback((entry: IOSDeviceFileEntry) => {
    if (entry.is_dir) {
      setCurrentPath(entry.path)
    } else if (selectionMode === 'file') {
      onSelectPath(entry.path)
    }
  }, [selectionMode, onSelectPath])

  const handleSelectCurrent = useCallback(() => {
    onSelectPath(currentPath)
  }, [currentPath, onSelectPath])

  // Handle device disconnect
  if (isDisconnected) {
    return (
      <div className="bg-card border border-border rounded-xl p-6 space-y-4">
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={onBack}
            className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            Back to devices
          </button>
        </div>

        <div className="flex flex-col items-center justify-center py-8 text-center">
          <div className="w-12 h-12 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center mb-3">
            <WifiOff className="w-6 h-6 text-red-500" />
          </div>
          <h3 className="text-sm font-semibold text-foreground mb-1">
            Device disconnected
          </h3>
          <p className="text-xs text-muted-foreground max-w-xs">
            {device.name} is no longer connected. Reconnect the device via USB
            and unlock it, then try again.
          </p>
          <button
            type="button"
            onClick={() => refetch()}
            className="mt-4 inline-flex items-center gap-1.5 px-3 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:bg-primary/90 transition-colors"
          >
            <RefreshCw className="w-3 h-3" />
            Retry
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onBack}
            className="text-muted-foreground hover:text-foreground transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div className="flex items-center gap-2">
            <Smartphone className="w-4 h-4 text-blue-500" />
            <span className="text-sm font-medium text-foreground">{device.name}</span>
            {device.active_tier && (() => {
              const tierConfig = device.active_tier === 'tier1'
                ? {
                    label: 'Apple Support',
                    title: 'Connected via: Apple Mobile Device Support',
                    icon: <Usb className="w-2.5 h-2.5" />,
                    className: 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400',
                  }
                : device.active_tier === 'wpd'
                ? {
                    label: 'Windows',
                    title: 'Connected via: Windows Portable Devices (WPD)',
                    icon: <HardDrive className="w-2.5 h-2.5" />,
                    className: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400',
                  }
                : {
                    label: 'Open-source bridge',
                    title: 'Connected via: Open-source WSL bridge',
                    icon: <Wifi className="w-2.5 h-2.5" />,
                    className: 'bg-teal-100 dark:bg-teal-900/30 text-teal-700 dark:text-teal-400',
                  }
              return (
                <span
                  className={cn(
                    'inline-flex items-center gap-0.5 text-[9px] px-1 py-0.5 rounded',
                    tierConfig.className,
                  )}
                  title={tierConfig.title}
                >
                  {tierConfig.icon}
                  {tierConfig.label}
                </span>
              )
            })()}
          </div>
        </div>

        {/* Selection mode toggle */}
        <div className="flex gap-1 p-0.5 bg-muted rounded-lg">
          <button
            type="button"
            onClick={() => setSelectionMode('folder')}
            className={cn(
              'px-2.5 py-1 rounded-md text-xs font-medium transition-colors',
              selectionMode === 'folder'
                ? 'bg-background text-foreground shadow-xs'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            Folder
          </button>
          <button
            type="button"
            onClick={() => setSelectionMode('file')}
            className={cn(
              'px-2.5 py-1 rounded-md text-xs font-medium transition-colors',
              selectionMode === 'file'
                ? 'bg-background text-foreground shadow-xs'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            File
          </button>
        </div>
      </div>

      {/* Breadcrumb */}
      <div className="px-4 py-2 border-b border-border bg-muted/30">
        <Breadcrumb path={currentPath} onNavigate={handleNavigate} />
      </div>

      {/* File list */}
      <div className="max-h-80 overflow-y-auto">
        {isLoading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 text-muted-foreground animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">Loading...</span>
          </div>
        )}

        {isError && !isDisconnected && (
          <div className="flex flex-col items-center justify-center py-8 text-center px-4">
            <AlertTriangle className="w-8 h-8 text-amber-500 mb-2" />
            <p className="text-sm text-foreground font-medium">Failed to load folder</p>
            <p className="text-xs text-muted-foreground mt-1">
              {error?.message || 'An error occurred while browsing the device.'}
            </p>
            <button
              type="button"
              onClick={() => refetch()}
              className="mt-3 text-xs text-primary hover:underline"
            >
              Try again
            </button>
          </div>
        )}

        {!isLoading && !isError && sortedEntries.length === 0 && (
          <div className="flex flex-col items-center justify-center py-8 text-center px-4">
            <FolderOpen className="w-8 h-8 text-muted-foreground mb-2 opacity-40" />
            <p className="text-sm text-muted-foreground">
              {selectionMode === 'folder'
                ? 'No subfolders in this directory'
                : 'This directory is empty'}
            </p>
          </div>
        )}

        {!isLoading && !isError && (
          <AnimatePresence>
            {sortedEntries.map((entry, i) => (
              <motion.button
                key={entry.path}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: Math.min(i * 0.02, 0.3) }}
                type="button"
                onClick={() => handleEntryClick(entry)}
                className={cn(
                  'w-full flex items-center gap-3 px-4 py-2.5 hover:bg-muted transition-colors text-left',
                  entry.is_dir && 'cursor-pointer',
                  !entry.is_dir && selectionMode === 'folder' && 'opacity-50 cursor-default',
                )}
                disabled={!entry.is_dir && selectionMode === 'folder'}
              >
                <div className="w-8 h-8 rounded-lg bg-muted flex items-center justify-center shrink-0">
                  {fileIcon(entry)}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-foreground truncate">{entry.name}</p>
                  {!entry.is_dir && (
                    <p className="text-[10px] text-muted-foreground">
                      {formatBytes(entry.size)}
                    </p>
                  )}
                </div>
                {entry.is_dir && (
                  <ChevronRight className="w-4 h-4 text-muted-foreground shrink-0" />
                )}
              </motion.button>
            ))}
          </AnimatePresence>
        )}
      </div>

      {/* Footer: Select current folder */}
      <div className="px-4 py-3 border-t border-border bg-muted/20">
        <button
          type="button"
          onClick={handleSelectCurrent}
          className="w-full px-4 py-2.5 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors"
        >
          {selectionMode === 'folder'
            ? `Select "${currentPath.split('/').filter(Boolean).pop() || 'Root'}" folder`
            : `Browse from "${currentPath.split('/').filter(Boolean).pop() || 'Root'}"`}
        </button>
      </div>
    </div>
  )
}
