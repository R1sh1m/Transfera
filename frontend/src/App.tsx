// ---------------------------------------------------------------------------
// Transfera v2 — App Shell
// Zustand-driven page routing, providers, notification toast, duplicate modal.
// ---------------------------------------------------------------------------

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
} from 'lucide-react'
import { useTransferStore } from '@/store/transfer'
import { cn } from '@/lib/utils'
import type { UIState } from '@/store/transfer'

import DashboardPage from '@/pages/DashboardPage'
import DeviceSetupPage from '@/pages/DeviceSetupPage'
import TransferPage from '@/pages/TransferPage'
import LibraryPage from '@/pages/LibraryPage'
import DuplicateModal from '@/components/DuplicateModal'
import ThemeToggle from '@/components/ThemeToggle'

// ---------------------------------------------------------------------------
// React Query client
// ---------------------------------------------------------------------------
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
})

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
  const wsConnected = useTransferStore((s) => s.wsConnected)

  return (
    <div className="w-14 flex flex-col items-center py-3 gap-1 border-r border-border bg-card/50">
      {/* Logo */}
      <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center mb-3">
        <HardDrive className="w-4 h-4 text-primary-foreground" />
      </div>

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
        className={cn(
          'w-2.5 h-2.5 rounded-full',
          wsConnected ? 'bg-green-500' : 'bg-muted-foreground/30',
        )}
        title={wsConnected ? 'Connected' : 'Disconnected'}
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
    <div className="drag-region h-10 flex items-center justify-between px-4 border-b border-border bg-card/80 backdrop-blur-sm flex-shrink-0">
      <div className="flex items-center gap-2">
        <HardDrive className="w-4 h-4 text-primary" />
        <span className="text-sm font-semibold text-foreground">Transfera</span>
      </div>
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
          className="h-6 w-6 flex items-center justify-center rounded hover:bg-red-500 hover:text-white text-muted-foreground"
        >
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M1 1L9 9M9 1L1 9"/></svg>
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page Router
// ---------------------------------------------------------------------------
function PageRouter() {
  const currentPage = useTransferStore((s) => s.ui.currentPage)

  const pageMap: Record<UIState['currentPage'], React.ReactNode> = {
    dashboard: <DashboardPage />,
    setup: <DeviceSetupPage />,
    transfer: <TransferPage />,
    library: <LibraryPage />,
  }

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
        {pageMap[currentPage]}
      </motion.div>
    </AnimatePresence>
  )
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <div className="h-screen flex flex-col bg-background">
        <TitleBar />
        <div className="flex-1 flex min-h-0">
          <Sidebar />
          <PageRouter />
        </div>
      </div>
      <DuplicateModal />
      <NotificationToast />
    </QueryClientProvider>
  )
}
