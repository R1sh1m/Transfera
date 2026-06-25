// ---------------------------------------------------------------------------
// Transfera v2 — App Shell
// Zustand-driven page routing, providers, notification toast, duplicate modal.
// ---------------------------------------------------------------------------

import { useEffect, useState } from 'react'
import { QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import { queryClient } from '@/lib/query-client'
import { AnimatePresence, motion } from 'framer-motion'
import {
  LayoutDashboard,
  Settings,
  ArrowRightLeft,
  Library,
  HardDrive,
  X,
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  Info,
  ServerCrash,
  RefreshCw,
} from 'lucide-react'
import { useTransferStore } from '@/store/transfer'
import { cn, isElectron } from '@/lib/utils'
import { useHealth } from '@/lib/queries'
import type { UIState } from '@/store/transfer'

import DashboardPage from '@/pages/DashboardPage'
import DeviceSetupPage from '@/pages/DeviceSetupPage'
import TransferPage from '@/pages/TransferPage'
import LibraryPage from '@/pages/LibraryPage'
import DuplicateModal from '@/components/DuplicateModal'
import ThemeToggle from '@/components/ThemeToggle'
import PageErrorBoundary from '@/components/PageErrorBoundary'

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
const navItems: { id: UIState['currentPage']; label: string; icon: React.ReactNode }[] = [
  { id: 'dashboard', label: 'Dashboard', icon: <LayoutDashboard className="w-4 h-4" /> },
  { id: 'setup',     label: 'Setup',      icon: <Settings className="w-4 h-4" /> },
  { id: 'transfer',  label: 'Transfer',   icon: <ArrowRightLeft className="w-4 h-4" /> },
  { id: 'library',   label: 'Library',    icon: <Library className="w-4 h-4" /> },
]

function Sidebar() {
  const currentPage = useTransferStore((s) => s.ui.currentPage)
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage)
  const { data: health, isLoading, isError } = useHealth()

  const backendColor = isLoading
    ? 'bg-muted-foreground/30 animate-pulse'
    : isError || !health
      ? 'bg-red-500'
      : 'bg-green-500'
  const backendTitle = isLoading
    ? 'Backend: Connecting...'
    : isError || !health
      ? 'Backend: Disconnected'
      : 'Backend: Connected'

  return (
    <div className="w-14 flex flex-col items-center py-3 gap-1 border-r border-border bg-card/50">
      {/* Nav items */}
      {navItems.map((item) => (
        <button
          key={item.id}
          onClick={() => setCurrentPage(item.id)}
          className={cn(
            'no-drag w-10 h-10 rounded-lg flex items-center justify-center transition-colors relative',
            currentPage === item.id
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-muted hover:text-foreground',
          )}
          title={item.label}
        >
          {item.icon}
        </button>
      ))}

      {/* Spacer */}
      <div className="flex-1" />

      {/* Theme Toggle */}
      <ThemeToggle />

      {/* Connection indicator */}
      <div
        className={cn('w-2.5 h-2.5 rounded-full', backendColor)}
        title={backendTitle}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Notification Toast
// ---------------------------------------------------------------------------
const notifIcons: Record<'success' | 'error' | 'warning' | 'info', React.ReactNode> = {
  success: <CheckCircle2 className="w-4 h-4 text-green-500" />,
  error: <AlertCircle className="w-4 h-4 text-red-500" />,
  warning: <AlertTriangle className="w-4 h-4 text-amber-500" />,
  info: <Info className="w-4 h-4 text-blue-500" />,
}

function NotificationToast() {
  const notification = useTransferStore((s) => s.ui.notification)
  const clearNotification = useTransferStore((s) => s.clearNotification)

  return (
    <AnimatePresence>
      {notification && (
        <motion.div
          initial={{ opacity: 0, y: 20, x: 20 }}
          animate={{ opacity: 1, y: 0, x: 0 }}
          exit={{ opacity: 0, y: 20 }}
          className="fixed bottom-4 right-4 z-50 bg-card border border-border rounded-lg shadow-lg p-3 flex items-center gap-3 max-w-sm"
        >
          {notifIcons[notification.type]}
          <p className="text-sm text-foreground flex-1">{notification.message}</p>
          <button
            onClick={clearNotification}
            className="p-1 rounded hover:bg-muted text-muted-foreground"
          >
            <X className="w-3 h-3" />
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ---------------------------------------------------------------------------
// Title Bar
// ---------------------------------------------------------------------------
function TitleBar() {
  return (
    <div className="drag-region h-10 flex items-center justify-between px-4 border-b border-border bg-card/80 backdrop-blur-xs shrink-0">
      <div className="flex items-center gap-2">
        <div className="w-6 h-6 rounded-md bg-primary flex items-center justify-center">
          <HardDrive className="w-3.5 h-3.5 text-primary-foreground" />
        </div>
        <span className="text-sm font-semibold text-foreground">Transfera</span>
      </div>
      {isElectron && (
        <div className="no-drag flex items-center gap-1">
          <button
            onClick={() => window.electronAPI?.minimizeWindow()}
            className="h-6 w-6 flex items-center justify-center rounded hover:bg-muted text-muted-foreground"
          >
            <svg width="10" height="1" viewBox="0 0 10 1" fill="currentColor"><rect width="10" height="1"/></svg>
          </button>
          <button
            onClick={() => window.electronAPI?.maximizeWindow()}
            className="h-6 w-6 flex items-center justify-center rounded hover:bg-muted text-muted-foreground"
          >
            <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1"><rect x="0.5" y="0.5" width="9" height="9"/></svg>
          </button>
          <button
            onClick={() => window.electronAPI?.closeWindow()}
            title="Close to System Tray"
            className="h-6 w-6 flex items-center justify-center rounded hover:bg-red-500 hover:text-white text-muted-foreground"
          >
            <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M1 1L9 9M9 1L1 9"/></svg>
          </button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page Router
// ---------------------------------------------------------------------------
function PageRouter() {
  const currentPage = useTransferStore((s) => s.ui.currentPage)

  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={currentPage}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -6 }}
        transition={{ duration: 0.15 }}
        className="flex-1 overflow-y-auto px-6 py-5"
      >
        {currentPage === 'dashboard' && (
          <PageErrorBoundary pageName="Dashboard">
            <DashboardPage />
          </PageErrorBoundary>
        )}
        {currentPage === 'setup' && (
          <PageErrorBoundary pageName="Setup">
            <DeviceSetupPage />
          </PageErrorBoundary>
        )}
        {currentPage === 'transfer' && (
          <PageErrorBoundary pageName="Transfer">
            <TransferPage />
          </PageErrorBoundary>
        )}
        {currentPage === 'library' && (
          <PageErrorBoundary pageName="Library">
            <LibraryPage />
          </PageErrorBoundary>
        )}
      </motion.div>
    </AnimatePresence>
  )
}

// ---------------------------------------------------------------------------
// Backend Down Screen
// ---------------------------------------------------------------------------
function BackendDownScreen() {
  const serverDown = useTransferStore((s) => s.ui.serverDown)
  const [retrying, setRetrying] = useState(false)

  if (!serverDown) return null

  const handleRetry = async () => {
    setRetrying(true)
    if (isElectron && window.electronAPI?.getBackendStatus) {
      try {
        const status = await window.electronAPI.getBackendStatus()
        if (status.running) {
          useTransferStore.getState().setServerDown(false)
          setRetrying(false)
          return
        }
      } catch {
        // ignore
      }
    }
    // In browser mode or if Electron check fails, try health endpoint directly
    try {
      const res = await fetch('/api/health')
      if (res.ok) {
        useTransferStore.getState().setServerDown(false)
        setRetrying(false)
        return
      }
    } catch {
      // ignore
    }
    setTimeout(() => setRetrying(false), 2000)
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="fixed inset-0 z-100 bg-background flex items-center justify-center"
    >
      <div className="text-center space-y-4 max-w-sm mx-auto px-6">
        <div className="w-16 h-16 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center mx-auto">
          <ServerCrash className="w-8 h-8 text-red-500" />
        </div>
        <h1 className="text-xl font-bold text-foreground">Backend Unavailable</h1>
        <p className="text-sm text-muted-foreground">
          The Transfera backend could not be reached. Please ensure the backend
          process is running and try again.
        </p>
        <button
          onClick={handleRetry}
          disabled={retrying}
          className="no-drag inline-flex items-center gap-2 px-5 py-2.5 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          <RefreshCw className={cn('w-4 h-4', retrying && 'animate-spin')} />
          {retrying ? 'Retrying...' : 'Retry Connection'}
        </button>
      </div>
    </motion.div>
  )
}

// ---------------------------------------------------------------------------
// Backend Recovery Watcher
// Monitors backend health. When the backend comes back after being down,
// invalidates all stale queries so pages auto-recover without a reload.
// ---------------------------------------------------------------------------
function BackendRecoveryWatcher() {
  const qc = useQueryClient()
  const { data: health, isError } = useHealth()
  const [wasDown, setWasDown] = useState(false)

  useEffect(() => {
    if (isError) {
      setWasDown(true)
    } else if (wasDown && health?.status === 'ok') {
      // Backend just came back — refetch everything
      qc.invalidateQueries()
      useTransferStore.getState().setServerDown(false)
      setWasDown(false)
    }
  }, [isError, health, wasDown, qc])

  return null
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
export default function App() {
  // Listen for backend:down IPC from Electron main process
  useEffect(() => {
    if (isElectron && window.electronAPI?.onBackendDown) {
      const unsub = window.electronAPI.onBackendDown(() => {
        useTransferStore.getState().setServerDown(true)
      })
      return unsub
    }
  }, [])

  // Listen for notification:click IPC — when user clicks a native notification,
  // navigate to that completed session's report (the artifact that represents
  // its outcome), with a Dashboard fallback if no report exists.
  useEffect(() => {
    if (isElectron && window.electronAPI?.onNotificationClick) {
      const unsub = window.electronAPI.onNotificationClick(async (sessionId: number) => {
        const store = useTransferStore.getState()

        // Fetch session info to determine the right destination
        try {
          const BASE_URL = window.location.origin || 'http://127.0.0.1:47821'
          const res = await fetch(`${BASE_URL}/api/sessions/${sessionId}`)
          if (!res.ok) throw new Error('Failed to fetch session')
          const session = await res.json()

          if (session.session_report_path) {
            // Open the HTML report directly — this is the artifact that
            // represents the completed session's actual outcome.
            if (window.electronAPI?.openPath) {
              window.electronAPI.openPath(session.session_report_path)
            } else {
              window.open(`/api/sessions/${sessionId}/report?fmt=html`, '_blank')
            }
            return
          }
        } catch {
          // Fetch failed or no report — fall through to Dashboard
        }

        // Fallback: navigate to Dashboard, where the session appears
        // in the Recent Sessions list with View/Report actions.
        store.setCurrentPage('dashboard')
      })
      return unsub
    }
  }, [])

  return (
    <QueryClientProvider client={queryClient}>
      <div className="h-screen flex flex-col bg-background">
        <TitleBar />
        <div className="flex-1 flex min-h-0">
          <Sidebar />
          <PageRouter />
        </div>
      </div>
      <PageErrorBoundary pageName="DuplicateModal">
        <DuplicateModal />
      </PageErrorBoundary>
      <NotificationToast />
      <BackendDownScreen />
      <BackendRecoveryWatcher />
    </QueryClientProvider>
  )
}
