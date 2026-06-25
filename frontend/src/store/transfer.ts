// ---------------------------------------------------------------------------
// Transfera v2 — Zustand Transfer Store
// Global client-side state for real-time transfer tracking and WS events.
// ---------------------------------------------------------------------------

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { queryClient } from '@/lib/query-client'
import { clearThumbFailCache } from '@/lib/thumbnail-fetch'
import type {
  WSEvent,
  WSEventType,
  SessionInfo,
  SessionProgress,
  BatchInfo,
  DuplicateReport,
  MediaItemInfo,
  HopStatus,
  BatchStatus,
  SessionStatus,
  TransferMode,
  FolderLayout,
} from '@/types/api'

// ---------------------------------------------------------------------------
// Sub-state slices
// ---------------------------------------------------------------------------
export interface ActiveBatch {
  batchId: number
  batchNumber: number
  totalItems: number
  completedItems: number
  status: BatchStatus
  hop1Progress: number
  hop1Status: HopStatus
  hop2Progress: number
  hop2Status: HopStatus
  startedAt: string | null
  currentFileName: string
  currentItemId: number | null
}

export interface TransferSnapshot {
  sessionId: number | null
  sessionName: string
  sourceRoot: string
  destRoot: string
  transferMode: TransferMode
  status: SessionStatus
  totalItems: number
  completedItems: number
  failedItems: number
  totalFiles: number
  cachedFiles: number
  importedFiles: number
  failedFiles: number
  currentBatch: number
  totalBatches: number
  progressPercent: number
  activeBatch: ActiveBatch | null
  batches: BatchInfo[]
  speed: number          // files/sec (rolling average)
  elapsed: number        // ms since start
  eta: number | null     // estimated ms remaining
  startedAt: number | null  // epoch ms when session_started received
  completedAt: number | null  // epoch ms when session completed (null while running)
  currentFileName: string
  currentItemId: number | null  // stable DB id of current file for thumbnail fetching
}

export interface DuplicateState {
  isOpen: boolean
  report: DuplicateReport | null
  resolutions: Map<number, 'skip' | 'overwrite' | 'keep_both'>
  applyToAll: 'skip' | 'overwrite' | 'keep_both' | null
}

export interface ScanState {
  isScanning: boolean
  scannedFiles: number
  totalFound: number
}

export interface LibraryState {
  items: MediaItemInfo[]
  page: number
  totalPages: number
  total: number
  isLoadingMore: boolean
}

export interface UIState {
  currentPage: 'dashboard' | 'setup' | 'transfer' | 'library'
  notification: { type: 'success' | 'error' | 'warning' | 'info'; message: string } | null
  defaultTransferMode: TransferMode
  serverDown: boolean
  setupSourcePath: string
  setupDestPath: string
  setupSessionName: string
  setupTransferMode: TransferMode
  setupMoveConfirmed: boolean
  setupOnlyNewMode: boolean
  setupFolderLayout: FolderLayout
  wsError: string | null
  lastCompletedSessionId: number | null
  lastRegeneratedSessionId: number | null
  completedSnapshot: TransferSnapshot | null
}

// ---------------------------------------------------------------------------
// Store interface
// ---------------------------------------------------------------------------
export interface TransferStore {
  // --- State slices -------------------------------------------------------
  transfer: TransferSnapshot
  duplicates: DuplicateState
  scan: ScanState
  library: LibraryState
  ui: UIState
  wsConnected: boolean

  // --- Transfer actions ---------------------------------------------------
  initTransfer: (session: SessionInfo) => void
  resetTransfer: () => void
  updateFromPolling: (progress: SessionProgress) => void

  // --- Duplicate actions ---------------------------------------------------
  openDuplicates: (report: DuplicateReport) => void
  closeDuplicates: () => void
  setResolution: (itemId: number, action: 'skip' | 'overwrite' | 'keep_both') => void
  setApplyToAll: (action: 'skip' | 'overwrite' | 'keep_both') => void
  clearResolutions: () => void

  // --- Scan actions --------------------------------------------------------
  setScanning: (scanning: boolean) => void

  // --- Library actions -----------------------------------------------------
  appendLibraryItems: (items: MediaItemInfo[], total: number, pages: number) => void
  setLibraryPage: (page: number) => void
  resetLibrary: () => void
  setLoadingMore: (loading: boolean) => void

  // --- UI actions ----------------------------------------------------------
  setCurrentPage: (page: UIState['currentPage']) => void
  setDefaultTransferMode: (mode: TransferMode) => void
  showNotification: (type: 'success' | 'error' | 'warning' | 'info', message: string) => void
  clearNotification: () => void
  setServerDown: (down: boolean) => void
  setSetupSourcePath: (path: string) => void
  setSetupDestPath: (path: string) => void
  setSetupSessionName: (name: string) => void
  setSetupTransferMode: (mode: TransferMode) => void
  setSetupMoveConfirmed: (confirmed: boolean) => void
  setSetupOnlyNewMode: (onlyNew: boolean) => void
  setSetupFolderLayout: (layout: FolderLayout) => void
  resetSetup: () => void
  setLastCompletedSessionId: (id: number | null) => void
  setLastRegeneratedSessionId: (id: number | null) => void
  setCompletedSnapshot: (snapshot: TransferSnapshot | null) => void
  setWsError: (error: string | null) => void

  // --- Clear all (used after "Clear Library") ----------------------------
  clearAll: () => void
  clearAllExceptPage: () => void

  // --- WS actions ---------------------------------------------------------
  setWsConnected: (connected: boolean) => void
  handleWsEvent: (event: WSEvent) => void
}

// ---------------------------------------------------------------------------
// Initial values
// ---------------------------------------------------------------------------
const initialTransfer: TransferSnapshot = {
  sessionId: null,
  sessionName: '',
  sourceRoot: '',
  destRoot: '',
  transferMode: 'copy',
  status: 'created',
  totalItems: 0,
  completedItems: 0,
  failedItems: 0,
  totalFiles: 0,
  cachedFiles: 0,
  importedFiles: 0,
  failedFiles: 0,
  currentBatch: 0,
  totalBatches: 0,
  progressPercent: 0,
  activeBatch: null,
  batches: [],
  speed: 0,
  elapsed: 0,
  eta: null,
  startedAt: null,
  completedAt: null,
  currentFileName: '',
  currentItemId: null,
}

const initialDuplicates: DuplicateState = {
  isOpen: false,
  report: null,
  resolutions: new Map(),
  applyToAll: null,
}

const initialScan: ScanState = {
  isScanning: false,
  scannedFiles: 0,
  totalFound: 0,
}

const initialLibrary: LibraryState = {
  items: [],
  page: 1,
  totalPages: 1,
  total: 0,
  isLoadingMore: false,
}

const initialUI: UIState = {
  currentPage: 'dashboard',
  notification: null,
  defaultTransferMode: 'copy',
  serverDown: false,
  setupSourcePath: '',
  setupDestPath: '',
  setupSessionName: '',
  setupTransferMode: 'copy',
  setupMoveConfirmed: false,
  setupOnlyNewMode: false,
  setupFolderLayout: 'year/month',
  wsError: null,
  lastCompletedSessionId: null,
  lastRegeneratedSessionId: null,
  completedSnapshot: null,
}

// Track which sessions have already fired a completion notification to
// prevent duplicate toasts if the WS event is dispatched more than once.
const _notifiedSessions = new Set<number>()

// ---------------------------------------------------------------------------
// Reducer: handleWsEvent
// Central dispatcher for all 15 server-side WS event types.
// ---------------------------------------------------------------------------
function handleWsEventReducer(
  state: TransferStore,
  event: WSEvent,
): Partial<TransferStore> {
  const d = event.data
  const patch: Partial<TransferStore> = {}

  switch (event.event as WSEventType) {
    // --- Scan events ------------------------------------------------------
    case 'scan_progress': {
      patch.scan = {
        ...state.scan,
        scannedFiles: (d.processed as number) ?? state.scan.scannedFiles,
        totalFound: (d.total as number) ?? state.scan.totalFound,
      }
      break
    }

    case 'scan_complete': {
      patch.scan = {
        isScanning: false,
        scannedFiles: (d.item_count as number) ?? state.scan.scannedFiles,
        totalFound: (d.item_count as number) ?? state.scan.totalFound,
      }
      patch.transfer = {
        ...state.transfer,
        totalItems: (d.item_count as number) ?? state.transfer.totalItems,
      }
      break
    }

    // --- Batch events -----------------------------------------------------
    case 'batch_created': {
      const batch: BatchInfo = {
        id: (d.batch_id as number) ?? 0,
        session_id: state.transfer.sessionId ?? 0,
        batch_number: (d.batch_number as number) ?? 0,
        status: 'pending',
        total_items: (d.item_count as number) ?? 0,
        completed_items: 0,
        failed_items: 0,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }
      patch.transfer = {
        ...state.transfer,
        batches: [...state.transfer.batches, batch],
      }
      break
    }

    case 'batch_processing': {
      const batchId = d.batch_id as number
      const active: ActiveBatch = {
        batchId,
        batchNumber: (d.batch_number as number) ?? 0,
        totalItems: (d.item_count as number) ?? 0,
        completedItems: 0,
        status: 'processing',
        hop1Progress: 0,
        hop1Status: 'pending',
        hop2Progress: 0,
        hop2Status: 'pending',
        startedAt: new Date().toISOString(),
        currentFileName: '',
        currentItemId: null,
      }
      patch.transfer = { ...state.transfer, activeBatch: active }
      break
    }

    case 'batch_complete': {
      const batchId = d.batch_id as number
      const updatedBatches = state.transfer.batches.map((b) =>
        b.id === batchId
          ? { ...b, status: (d.status as BatchStatus) ?? 'completed' }
          : b,
      )
      patch.transfer = {
        ...state.transfer,
        batches: updatedBatches,
        activeBatch:
          state.transfer.activeBatch?.batchId === batchId
            ? null
            : state.transfer.activeBatch,
      }
      break
    }

    // --- Hop 1 events (Source -> Cache) -----------------------------------
    case 'hop1_progress': {
      const active = state.transfer.activeBatch
      if (active) {
        const processed = (d.processed as number) ?? 0
        const total = (d.total as number) ?? active.totalItems
        const itemId = (d.item_id as number) ?? null
        patch.transfer = {
          ...state.transfer,
          currentFileName: (d.file_name as string) ?? state.transfer.currentFileName,
          currentItemId: itemId ?? state.transfer.currentItemId,
          activeBatch: {
            ...active,
            hop1Progress: total > 0 ? Math.round((processed / total) * 100) : 0,
            hop1Status: 'transferring',
            completedItems: processed,
            currentFileName: (d.file_name as string) ?? active.currentFileName,
            currentItemId: itemId ?? active.currentItemId,
          },
        }
      }
      break
    }

    case 'hop1_complete': {
      const active = state.transfer.activeBatch
      if (active) {
        patch.transfer = {
          ...state.transfer,
          activeBatch: {
            ...active,
            hop1Progress: 100,
            hop1Status: 'completed',
            completedItems: (d.cached_count as number) ?? active.completedItems,
          },
        }
      }
      break
    }

    // --- Hop 2 events (Cache -> Destination) ------------------------------
    case 'hop2_progress': {
      const active = state.transfer.activeBatch
      if (active) {
        const processed = (d.processed as number) ?? 0
        const total = (d.total as number) ?? active.totalItems
        const itemId = (d.item_id as number) ?? null
        patch.transfer = {
          ...state.transfer,
          currentFileName: (d.file_name as string) ?? state.transfer.currentFileName,
          currentItemId: itemId ?? state.transfer.currentItemId,
          activeBatch: {
            ...active,
            hop2Progress: total > 0 ? Math.round((processed / total) * 100) : 0,
            hop2Status: 'transferring',
            currentFileName: (d.file_name as string) ?? active.currentFileName,
            currentItemId: itemId ?? active.currentItemId,
          },
        }
      }
      break
    }

    case 'hop2_complete': {
      const active = state.transfer.activeBatch
      if (active) {
        patch.transfer = {
          ...state.transfer,
          activeBatch: {
            ...active,
            hop2Progress: 100,
            hop2Status: 'completed',
          },
          completedItems:
            (d.imported_count as number) ?? state.transfer.completedItems,
        }
      }
      break
    }

    // --- Duplicate events -------------------------------------------------
    case 'duplicates_detected': {
      // The DuplicateModal will pick this up from store
      patch.duplicates = {
        ...state.duplicates,
        isOpen: true,
        report: d as unknown as DuplicateReport,
      }
      break
    }

    case 'duplicates_resolved': {
      patch.duplicates = initialDuplicates
      break
    }

    // --- Session events ---------------------------------------------------
    case 'session_started': {
      patch.transfer = {
        ...state.transfer,
        status: 'running',
        startedAt: state.transfer.startedAt ?? Date.now(),
      }
      break
    }

    case 'session_paused': {
      patch.transfer = {
        ...state.transfer,
        status: 'paused',
      }
      break
    }

    case 'session_completed':
    case 'session_completed_with_errors': {
      const isError = event.event === 'session_completed_with_errors'
      const imported = (d.imported_files as number) ?? state.transfer.importedFiles
      const failed = (d.failed_files as number) ?? state.transfer.failedFiles
      const sessionId = (d.session_id as number) ?? state.transfer.sessionId
      patch.transfer = {
        ...state.transfer,
        status: isError ? ('completed_with_errors' as SessionStatus) : ('completed' as SessionStatus),
        importedFiles: imported,
        failedFiles: failed,
        completedAt: Date.now(),
        activeBatch: null,
      }
      if (isError) {
        patch.ui = {
          ...state.ui,
          lastCompletedSessionId: sessionId,
          notification: {
            type: 'warning',
            message: `${imported} transferred, ${failed} failed. See library for details.`,
          },
        }
      } else {
        patch.ui = {
          ...state.ui,
          lastCompletedSessionId: sessionId,
          notification: {
            type: 'success',
            message: `${imported} file${imported !== 1 ? 's' : ''} transferred successfully.`,
          },
        }
      }
      break
    }

    // --- System events ----------------------------------------------------
    case 'error': {
      patch.ui = {
        ...state.ui,
        notification: {
          type: 'error',
          message: (d.message as string) ?? 'Unknown error',
        },
      }
      break
    }

    case 'pong': {
      // Keepalive ack — no state change
      break
    }
  }

  return patch
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useTransferStore = create<TransferStore>()(
  persist(
    (set) => ({
  // State
  transfer: { ...initialTransfer },
  duplicates: { ...initialDuplicates },
  scan: { ...initialScan },
  library: { ...initialLibrary },
  ui: { ...initialUI },
wsConnected: false,

  // --- Transfer actions ---------------------------------------------------
  initTransfer: (session) => {
    clearThumbFailCache()
    return set((state) => ({
      transfer: {
        ...initialTransfer,
        sessionId: session.id,
        sessionName: session.session_name,
        sourceRoot: session.source_root,
        destRoot: session.dest_root,
        transferMode: session.transfer_mode ?? 'copy',
        status: session.status,
        totalItems: session.total_items,
        completedItems: session.completed_items,
        failedItems: session.failed_items,
        startedAt: state.transfer.startedAt,
      },
      ui: {
        ...state.ui,
        completedSnapshot: null,
      },
    }))
  },

  updateFromPolling: (progress) =>
    set((state) => {
      const TERMINAL = new Set(['completed', 'completed_with_errors', 'failed', 'cancelled'])
      const startedAt = progress.started_at
        ? new Date(progress.started_at).getTime()
        : null
      const completedAt = progress.completed_at
        ? new Date(progress.completed_at).getTime()
        : null

      const activeBatch = progress.active_batch_id
        ? {
            batchId: progress.active_batch_id,
            batchNumber: progress.active_batch_number,
            totalItems: progress.active_batch_total,
            completedItems: progress.active_batch_completed,
            status: progress.active_batch_status as BatchStatus,
            hop1Progress: progress.active_batch_hop1_progress,
            hop1Status: (progress.active_batch_hop1_progress >= 100
              ? 'completed'
              : progress.active_batch_hop1_progress > 0
                ? 'transferring'
                : 'pending') as HopStatus,
            hop2Progress: progress.active_batch_hop2_progress,
            hop2Status: (progress.active_batch_hop2_progress >= 100
              ? 'completed'
              : progress.active_batch_hop2_progress > 0
                ? 'transferring'
                : 'pending') as HopStatus,
            startedAt: startedAt ? new Date(startedAt).toISOString() : null,
            currentFileName: progress.current_file_name,
            currentItemId: progress.current_item_id,
          }
        : null

      return {
        transfer: {
          ...state.transfer,
          sessionId: progress.session_id,
          status: TERMINAL.has(state.transfer.status)
            ? state.transfer.status
            : (progress.status as SessionStatus),
          totalItems: progress.total_items,
          completedItems: progress.completed_items,
          failedItems: progress.failed_items,
          totalFiles: progress.total_files ?? progress.total_items,
          cachedFiles: progress.cached_files ?? 0,
          importedFiles: progress.imported_files ?? 0,
          failedFiles: progress.failed_files ?? 0,
          currentBatch: progress.current_batch ?? 0,
          totalBatches: progress.total_batches ?? 0,
          progressPercent: progress.progress_percent ?? 0,
          activeBatch,
          startedAt,
          completedAt,
          currentFileName: progress.current_file_name,
          currentItemId: progress.current_item_id,
          speed: progress.speed_files_per_sec ?? 0,
          elapsed: (progress.elapsed_seconds ?? 0) * 1000,
          eta: progress.eta_seconds != null ? progress.eta_seconds * 1000 : null,
        },
      }
    }),

  resetTransfer: () => set((s) => ({
    transfer: { ...initialTransfer },
    ui: { ...s.ui, completedSnapshot: null },
  })),

  // --- Duplicate actions ---------------------------------------------------
  openDuplicates: (report) =>
    set({
      duplicates: {
        isOpen: true,
        report,
        resolutions: new Map(),
        applyToAll: null,
      },
    }),

  closeDuplicates: () =>
    set({ duplicates: { ...initialDuplicates } }),

  setResolution: (itemId, action) =>
    set((s) => {
      const next = new Map(s.duplicates.resolutions)
      next.set(itemId, action)
      return { duplicates: { ...s.duplicates, resolutions: next } }
    }),

  setApplyToAll: (action) =>
    set((s) => ({
      duplicates: { ...s.duplicates, applyToAll: action },
    })),

  clearResolutions: () =>
    set((s) => ({
      duplicates: {
        ...s.duplicates,
        resolutions: new Map(),
        applyToAll: null,
      },
    })),

  // --- Scan actions --------------------------------------------------------
  setScanning: (scanning) =>
    set((s) => ({
      scan: { ...s.scan, isScanning: scanning },
    })),

  // --- Library actions -----------------------------------------------------
  appendLibraryItems: (items, total, pages) =>
    set((s) => {
      const existingIds = new Set(s.library.items.map(i => i.id))
      const newItems = items.filter(i => !existingIds.has(i.id))
      return {
        library: {
          items: [...s.library.items, ...newItems],
          page: s.library.page + 1,
          totalPages: pages,
          total,
          isLoadingMore: false,
        },
      }
    }),

  setLibraryPage: (page) =>
    set((s) => ({ library: { ...s.library, page } })),

  resetLibrary: () => set({ library: { ...initialLibrary } }),

  setLoadingMore: (loading) =>
    set((s) => ({ library: { ...s.library, isLoadingMore: loading } })),

  // --- UI actions ----------------------------------------------------------
  setCurrentPage: (page) =>
    set((s) => ({ ui: { ...s.ui, currentPage: page } })),

  setDefaultTransferMode: (mode) =>
    set((s) => ({ ui: { ...s.ui, defaultTransferMode: mode } })),

  showNotification: (type, message) =>
    set((s) => ({
      ui: { ...s.ui, notification: { type, message } },
    })),

  clearNotification: () =>
    set((s) => ({ ui: { ...s.ui, notification: null } })),

  setServerDown: (down) =>
    set((s) => ({ ui: { ...s.ui, serverDown: down } })),

  setSetupSourcePath: (path) =>
    set((s) => ({ ui: { ...s.ui, setupSourcePath: path } })),

  setSetupDestPath: (path) =>
    set((s) => ({ ui: { ...s.ui, setupDestPath: path } })),

  setSetupSessionName: (name) =>
    set((s) => ({ ui: { ...s.ui, setupSessionName: name } })),

  setSetupTransferMode: (mode) =>
    set((s) => ({ ui: { ...s.ui, setupTransferMode: mode } })),

  setSetupMoveConfirmed: (confirmed) =>
    set((s) => ({ ui: { ...s.ui, setupMoveConfirmed: confirmed } })),

  setSetupOnlyNewMode: (onlyNew) =>
    set((s) => ({ ui: { ...s.ui, setupOnlyNewMode: onlyNew } })),

  setSetupFolderLayout: (layout) =>
    set((s) => ({ ui: { ...s.ui, setupFolderLayout: layout } })),

  resetSetup: () =>
    set((s) => ({
      ui: {
        ...s.ui,
        setupSourcePath: '',
        setupDestPath: '',
        setupSessionName: '',
        setupTransferMode: s.ui.defaultTransferMode,
        setupMoveConfirmed: false,
        setupOnlyNewMode: false,
        setupFolderLayout: 'year/month',
      },
    })),

  setLastCompletedSessionId: (id) =>
    set((s) => ({ ui: { ...s.ui, lastCompletedSessionId: id } })),

  setLastRegeneratedSessionId: (id) =>
    set((s) => ({ ui: { ...s.ui, lastRegeneratedSessionId: id } })),

  setCompletedSnapshot: (snapshot) =>
    set((s) => ({ ui: { ...s.ui, completedSnapshot: snapshot } })),

  setWsError: (error) =>
    set((s) => ({ ui: { ...s.ui, wsError: error } })),

  // --- Clear all except currentPage (used before deferred navigation) ---
  clearAllExceptPage: () => {
    clearThumbFailCache()
    return set((s) => ({
      transfer: { ...initialTransfer, status: 'cancelled' as SessionStatus },
      library: { ...initialLibrary },
      scan: { ...initialScan },
      duplicates: { ...initialDuplicates },
      ui: {
        ...s.ui,
        notification: null,
        wsError: null,
        completedSnapshot: null,
        lastCompletedSessionId: null,
        lastRegeneratedSessionId: null,
      },
    }))
  },

  // --- Clear all (used after "Clear Library") ----------------------------
  clearAll: () => {
    clearThumbFailCache()
    return set((s) => ({
      // Use 'cancelled' (a terminal status) instead of initialTransfer's 'created'
      // so that useSessionBatches / useSessionProgress immediately stop polling.
      // Leaving status as 'created' with sessionId=null causes both hooks to
      // fire requests to /api/sessions/null/... which 404 and blank non-Library pages.
      transfer: { ...initialTransfer, status: 'cancelled' as SessionStatus },
      library: { ...initialLibrary },
      scan: { ...initialScan },
      duplicates: { ...initialDuplicates },
      ui: {
        ...s.ui,
        currentPage: 'dashboard' as const,
        notification: null,
        wsError: null,
        completedSnapshot: null,
        lastCompletedSessionId: null,
        lastRegeneratedSessionId: null,
      },
    }))
  },

  // --- WS actions ---------------------------------------------------------
  setWsConnected: (connected) => set({ wsConnected: connected }),

  handleWsEvent: (event) => {
    // Apply the reducer to get the state patch
    set((state) => handleWsEventReducer(state, event) as TransferStore)
    const sid = (event.data?.session_id as number) ?? useTransferStore.getState().transfer.sessionId

    // When a session reaches a terminal state, cancel any in-flight polling
    // queries so they don't continue after the WebSocket event.
    if (event.event === 'session_completed' || event.event === 'session_completed_with_errors') {
      if (sid != null) {
        queryClient.cancelQueries({ queryKey: ['session-progress', sid] })
        queryClient.cancelQueries({ queryKey: ['session', sid] })
        queryClient.cancelQueries({ queryKey: ['batches', sid] })
      }
    }

    // Side effect: fire a native notification on session completion if the
    // window is not focused. Only fires once per session (guarded by
    // _notifiedSessions set).
    if (event.event === 'session_completed' || event.event === 'session_completed_with_errors') {
      const d = event.data
      const sessionId = (d.session_id as number) ?? 0
      const isError = event.event === 'session_completed_with_errors'
      const importedFiles = (d.imported_files as number) ?? 0
      const failedFiles = (d.failed_files as number) ?? 0
      const totalFiles = importedFiles + failedFiles

      // Skip if already notified for this session
      if (_notifiedSessions.has(sessionId)) return
      _notifiedSessions.add(sessionId)

      // Only fire if running inside Electron
      if (typeof window !== 'undefined' && window.electronAPI) {
        window.electronAPI.isWindowFocused().then((focused) => {
          if (focused) return // Don't bother the user if they're looking at the app

          let title: string
          let body: string

          if (isError) {
            title = 'Transfer completed with errors'
            body = `${importedFiles} files copied — ${failedFiles} failed`
          } else {
            title = 'Transfer complete'
            body = `${importedFiles} of ${totalFiles} files copied successfully`
          }

          window.electronAPI!.showNotification({ title, body, sessionId })
        }).catch(() => {
          // Ignore errors — notification is best-effort
        })
      }
    }
  },
}),
    {
      name: 'transfera-preferences',
      partialize: (state) => ({
        ui: {
          defaultTransferMode: state.ui.defaultTransferMode,
          setupSourcePath: state.ui.setupSourcePath,
          setupDestPath: state.ui.setupDestPath,
          setupSessionName: state.ui.setupSessionName,
          setupTransferMode: state.ui.setupTransferMode,
          setupOnlyNewMode: state.ui.setupOnlyNewMode,
          setupFolderLayout: state.ui.setupFolderLayout,
        },
      }),
      // Zustand's default merge does a shallow top-level spread, which means
      // the persisted `ui` object (containing only 5 fields) REPLACES the
      // entire initial `ui` slice, wiping `currentPage`, `notification`,
      // `serverDown`, `wsError`, and `setupMoveConfirmed` to undefined.
      // A deep merge of the `ui` slice preserves all initial fields while
      // restoring only the persisted ones.
      merge: (persistedState: unknown, currentState: TransferStore) => {
        const persisted = (persistedState ?? {}) as Record<string, unknown>
        return {
          ...currentState,
          ui: {
            ...currentState.ui,
            ...((persisted.ui ?? {}) as Record<string, unknown>),
          },
        }
      },
    },
  ),
)
