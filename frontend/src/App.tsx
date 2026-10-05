// ---------------------------------------------------------------------------
// Transfera v2 — App Shell
// Zustand-driven page routing, providers, notification toast, duplicate modal.
// ---------------------------------------------------------------------------

import { Suspense, lazy, useEffect, useState } from "react";
import { QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { queryClient } from "@/lib/query-client";
import { AnimatePresence, motion } from "framer-motion";
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
  Loader2,
} from "lucide-react";
import { useTransferStore } from "@/store/transfer";
import { cn } from "@/lib/utils";
import {
  isTauri,
  minimizeWindow,
  maximizeWindow,
  closeWindow,
  onBackendDown,
  onBackendStarting,
  onBackendReady,
  getBackendStatus,
} from "@/lib/desktop";
import { API_BASE_URL } from "@/lib/api-client";
import { useHealth } from "@/lib/queries";
import type { UIState } from "@/store/transfer";
import AppUpdateNotification from "@/components/AppUpdateNotification";

const DashboardPage = lazy(() => import("@/pages/DashboardPage"));
const DeviceSetupPage = lazy(() => import("@/pages/DeviceSetupPage"));
const TransferPage = lazy(() => import("@/pages/TransferPage"));
const LibraryPage = lazy(() => import("@/pages/LibraryPage"));
import DuplicateModal from "@/components/DuplicateModal";
import CloseGuardModal from "@/components/CloseGuardModal";
import ThemeToggle from "@/components/ThemeToggle";
import PageErrorBoundary from "@/components/PageErrorBoundary";
import { useDeviceWatcher } from "@/hooks/use-device-watcher";

function DeviceWatcher() {
  useDeviceWatcher();
  return null;
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
const navItems: {
  id: UIState["currentPage"];
  label: string;
  icon: React.ReactNode;
}[] = [
  {
    id: "dashboard",
    label: "Dashboard",
    icon: <LayoutDashboard className="w-4 h-4" />,
  },
  { id: "setup", label: "Setup", icon: <Settings className="w-4 h-4" /> },
  {
    id: "transfer",
    label: "Transfer",
    icon: <ArrowRightLeft className="w-4 h-4" />,
  },
  { id: "library", label: "Library", icon: <Library className="w-4 h-4" /> },
];

function Sidebar() {
  const currentPage = useTransferStore((s) => s.ui.currentPage);
  const setCurrentPage = useTransferStore((s) => s.setCurrentPage);
  const { data: health, isLoading, isError } = useHealth();

  const backendColor = isLoading
    ? "bg-muted-foreground/30 animate-pulse"
    : isError || !health
      ? "bg-red-500"
      : "bg-green-500";
  const backendTitle = isLoading
    ? "Backend: Connecting..."
    : isError || !health
      ? "Backend: Disconnected"
      : "Backend: Connected";

  return (
    <div className="w-14 flex flex-col items-center py-3 gap-1 border-r border-border bg-card/50">
      {/* Nav items */}
      {navItems.map((item) => (
        <button
          key={item.id}
          onClick={() => setCurrentPage(item.id)}
          className={cn(
            "no-drag w-10 h-10 rounded-lg flex items-center justify-center transition-colors relative",
            currentPage === item.id
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-muted hover:text-foreground",
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
        className={cn("w-2.5 h-2.5 rounded-full", backendColor)}
        title={backendTitle}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Notification Toast
// ---------------------------------------------------------------------------
const notifIcons: Record<
  "success" | "error" | "warning" | "info",
  React.ReactNode
> = {
  success: <CheckCircle2 className="w-4 h-4 text-green-500" />,
  error: <AlertCircle className="w-4 h-4 text-red-500" />,
  warning: <AlertTriangle className="w-4 h-4 text-amber-500" />,
  info: <Info className="w-4 h-4 text-blue-500" />,
};

function NotificationToast() {
  const notification = useTransferStore((s) => s.ui.notification);
  const clearNotification = useTransferStore((s) => s.clearNotification);

  return (
    <AnimatePresence>
      {notification && (
        <motion.div
          initial={{ opacity: 0, y: 20, x: 20 }}
          animate={{ opacity: 1, y: 0, x: 0 }}
          exit={{ opacity: 0, y: 20 }}
          className="fixed bottom-4 right-4 z-50 glass rounded-lg p-3 flex items-center gap-3 max-w-sm"
        >
          {notifIcons[notification.type]}
          <p className="text-sm text-foreground flex-1">
            {notification.message}
          </p>
          <button
            onClick={clearNotification}
            className="p-1 rounded hover:bg-muted text-muted-foreground"
          >
            <X className="w-3 h-3" />
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// Window Controls (min / max / close) — Tauri frameless window only.
// Rendered solely in the packaged shell; under `vite dev` there is no
// frameless window to control, so nothing renders.
// ---------------------------------------------------------------------------
function WindowControls() {
  if (!isTauri) return null;
  return (
    <div className="no-drag flex items-center gap-1">
      <button
        onClick={() => minimizeWindow()}
        title="Minimize to Tray"
        className="h-6 w-6 flex items-center justify-center rounded hover:bg-muted text-muted-foreground"
      >
        <svg width="10" height="1" viewBox="0 0 10 1" fill="currentColor">
          <rect width="10" height="1" />
        </svg>
      </button>
      <button
        onClick={() => maximizeWindow()}
        title="Maximize"
        className="h-6 w-6 flex items-center justify-center rounded hover:bg-muted text-muted-foreground"
      >
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          stroke="currentColor"
          strokeWidth="1"
        >
          <rect x="0.5" y="0.5" width="9" height="9" />
        </svg>
      </button>
      <button
        onClick={() => closeWindow()}
        title="Close"
        className="h-6 w-6 flex items-center justify-center rounded hover:bg-red-500 hover:text-white text-muted-foreground"
      >
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <path d="M1 1L9 9M9 1L1 9" />
        </svg>
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Title Bar
// ---------------------------------------------------------------------------
function TitleBar() {
  return (
    <div
      data-tauri-drag-region
      className="drag-region glass-bar h-10 flex items-center justify-between px-4 border-b border-border bg-card/80 shrink-0"
    >
      <div className="flex items-center gap-2">
        <div className="w-6 h-6 rounded-md bg-primary flex items-center justify-center">
          <HardDrive className="w-3.5 h-3.5 text-primary-foreground" />
        </div>
        <span className="text-sm font-semibold text-foreground">Transfera</span>
      </div>
      <WindowControls />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page Router
// ---------------------------------------------------------------------------
function PageRouter() {
  const currentPage = useTransferStore((s) => s.ui.currentPage);

  return (
    <div className="flex-1 flex flex-col min-h-0 relative overflow-hidden">
      <Suspense
        fallback={
          <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
            Loading…
          </div>
        }
      >
        {/* NOTE: intentionally no AnimatePresence exit here. mode="sync"
          exits left fully-opaque ghost pages stacked over the new page
          (transparent page backgrounds made every exit failure visible).
          Enter-only animation: old page unmounts instantly, new page fades
          in. Zero overlap possible by construction. */}
        {currentPage === "dashboard" && (
          <motion.div
            key="dashboard"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 overflow-y-auto bg-background px-6 py-5"
          >
            <PageErrorBoundary key="dashboard" pageName="Dashboard">
              <DashboardPage />
            </PageErrorBoundary>
          </motion.div>
        )}
        {currentPage === "setup" && (
          <motion.div
            key="setup"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 overflow-y-auto bg-background px-6 py-5"
          >
            <PageErrorBoundary key="setup" pageName="Setup">
              <DeviceSetupPage />
            </PageErrorBoundary>
          </motion.div>
        )}
        {currentPage === "transfer" && (
          <motion.div
            key="transfer"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 overflow-y-auto bg-background px-6 py-5"
          >
            <PageErrorBoundary key="transfer" pageName="Transfer">
              <TransferPage />
            </PageErrorBoundary>
          </motion.div>
        )}
        {currentPage === "library" && (
          <motion.div
            key="library"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 overflow-y-auto bg-background px-6 py-5"
          >
            <PageErrorBoundary key="library" pageName="Library">
              <LibraryPage />
            </PageErrorBoundary>
          </motion.div>
        )}
      </Suspense>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Backend Down Screen
// The Tauri shell ships the frozen sidecar — there is no first-run Python
// install. backend:down therefore always means "engine failed", answered
// with Retry (health probe) rather than a setup wizard.
// ---------------------------------------------------------------------------
function BackendDownScreen() {
  const serverDown = useTransferStore((s) => s.ui.serverDown);
  const [retrying, setRetrying] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const { data: health } = useHealth();

  // Initial probe on mount — if backend is already healthy, never show starting/down screen
  useEffect(() => {
    fetch(`${API_BASE_URL}/api/health`)
      .then((res) => {
        if (res.ok) {
          setIsStarting(false);
          if (useTransferStore.getState().ui.serverDown) {
            useTransferStore.getState().setServerDown(false);
          }
        }
      })
      .catch(() => {});
  }, []);

  // Listen for backend:starting — only show loading if backend isn't already responding
  useEffect(() => {
    const unsub = onBackendStarting(async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/api/health`);
        if (res.ok) {
          setIsStarting(false);
          useTransferStore.getState().setServerDown(false);
          return;
        }
      } catch {
        // Backend not yet ready
      }
      setIsStarting(true);
    });
    return unsub;
  }, []);

  // Listen for backend:ready — clear the starting/error state
  useEffect(() => {
    const unsub = onBackendReady(() => {
      setIsStarting(false);
      useTransferStore.getState().setServerDown(false);
    });
    return unsub;
  }, []);

  // Rapid health polling while starting or down (every 300ms) so the loading screen
  // dismisses the instant the backend is ready — never waits for slow TanStack intervals
  useEffect(() => {
    if (!isStarting && !serverDown) return;
    let cancelled = false;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/api/health`);
        if (res.ok && !cancelled) {
          setIsStarting(false);
          if (useTransferStore.getState().ui.serverDown) {
            useTransferStore.getState().setServerDown(false);
          }
        }
      } catch {
        // Still booting
      }
    }, 300);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [isStarting, serverDown]);

  // Clear starting/down as soon as TanStack useHealth sees "ok"
  useEffect(() => {
    if (health?.status === "ok") {
      setIsStarting(false);
      if (useTransferStore.getState().ui.serverDown) {
        useTransferStore.getState().setServerDown(false);
      }
    }
  }, [health]);

  if (!serverDown && !isStarting) return null;

  // Show a clean loading screen while the backend is in the process of starting up
  if (isStarting && !serverDown) {
    return (
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="fixed inset-0 z-100 bg-background flex items-center justify-center drag-region"
      >
        <div className="absolute top-3 right-3">
          <WindowControls />
        </div>
        <div className="text-center space-y-4 max-w-sm mx-auto px-6">
          <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center mx-auto">
            <Loader2 className="w-8 h-8 text-primary animate-spin" />
          </div>
          <h1 className="text-xl font-bold text-foreground">
            Starting Transfera
          </h1>
          <p className="text-sm text-muted-foreground">
            The backend engine is starting up. This takes a few seconds.
          </p>
        </div>
      </motion.div>
    );
  }

  const handleRetry = async () => {
    setRetrying(true);
    try {
      const status = await getBackendStatus();
      if (status.running) {
        useTransferStore.getState().setServerDown(false);
        setRetrying(false);
        return;
      }
      // Backend is still launching — show starting state
      if (status.starting) {
        setIsStarting(true);
        setRetrying(false);
        return;
      }
    } catch {
      // ignore
    }
    // Direct health-endpoint probe as a second chance. Absolute backend
    // URL: a relative fetch would hit the Tauri page origin, which serves
    // no API (404) in the packaged app.
    try {
      const res = await fetch(`${API_BASE_URL}/api/health`);
      if (res.ok) {
        useTransferStore.getState().setServerDown(false);
        setRetrying(false);
        return;
      }
    } catch {
      // ignore
    }
    setTimeout(() => setRetrying(false), 2000);
  };

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="fixed inset-0 z-100 bg-background flex items-center justify-center drag-region"
    >
      <div className="absolute top-3 right-3">
        <WindowControls />
      </div>
      <div className="text-center space-y-4 max-w-sm mx-auto px-6">
        <div className="w-16 h-16 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center mx-auto">
          <ServerCrash className="w-8 h-8 text-red-500" />
        </div>
        <h1 className="text-xl font-bold text-foreground">
          Engine Unavailable
        </h1>
        <p className="text-sm text-muted-foreground">
          Transfera couldn&apos;t start its engine. Try clicking Retry, or close
          and reopen the app.
        </p>
        <button
          onClick={handleRetry}
          disabled={retrying}
          className="no-drag inline-flex items-center gap-2 px-5 py-2.5 bg-primary text-primary-foreground rounded-pill text-sm font-normal hover:bg-primary/90 active:scale-[0.95] transition-colors disabled:opacity-50"
        >
          <RefreshCw className={cn("w-4 h-4", retrying && "animate-spin")} />
          {retrying ? "Retrying..." : "Retry"}
        </button>
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Backend Recovery Watcher
// Monitors backend health. When the backend comes back after being down,
// invalidates all stale queries so pages auto-recover without a reload.
// ---------------------------------------------------------------------------
function BackendRecoveryWatcher() {
  const qc = useQueryClient();
  const { data: health, isError } = useHealth();
  const [wasDown, setWasDown] = useState(false);
  // Only escalate to "Engine Unavailable" if the backend was previously
  // confirmed healthy this session. Cold-start probe failures (while the
  // backend is still booting) should not trigger the error screen.
  const [wasEverUp, setWasEverUp] = useState(false);

  useEffect(() => {
    if (isError) {
      setWasDown(true);
      // Mid-run engine death (kill -9, crash, port stolen): the shell only
      // emits backend:down for startup failures, so flip into the Engine
      // Unavailable screen from here. useHealth retries 3x with backoff
      // before isError, so this is a genuine outage — but only if we've
      // confirmed the backend was healthy at least once this session.
      if (wasEverUp) {
        useTransferStore.getState().setServerDown(true);
      }
    } else if (health?.status === "ok") {
      // Backend healthy: always clear a stale down screen, whatever set it
      // (missed backend:ready race, or a backend:down that fired before a
      // slow cold boot finished). Guarded reads keep a healthy poll a
      // complete no-op — no render churn, no query invalidation.
      setWasEverUp(true);
      if (wasDown) {
        // Backend just came back — refetch everything
        qc.invalidateQueries();
        setWasDown(false);
      }
      if (useTransferStore.getState().ui.serverDown) {
        useTransferStore.getState().setServerDown(false);
      }
    }
  }, [isError, health, wasDown, wasEverUp, qc]);

  return null;
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
export default function App() {
  // Listen for backend:down from the Tauri shell — flip into the
  // Engine Unavailable screen (BackendRecoveryWatcher clears it on return).
  useEffect(() => {
    const unsub = onBackendDown(() => {
      useTransferStore.getState().setServerDown(true);
    });
    return unsub;
  }, []);

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
      <CloseGuardModal />
      <NotificationToast />
      <AppUpdateNotification />
      <BackendDownScreen />
      <BackendRecoveryWatcher />
      <DeviceWatcher />
    </QueryClientProvider>
  );
}
