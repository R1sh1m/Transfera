// ---------------------------------------------------------------------------
// Transfera v2 — Zustand Transfer Store
// Global client-side state for real-time transfer tracking and WS events.
// ---------------------------------------------------------------------------

import { create } from 'zustand'
import type {
  WSEvent,
  WSEventType,
  SessionInfo,
  BatchInfo,
  DuplicateReport,
  MediaItemInfo,
  HopStatus,
  BatchStatus,
  SessionStatus,
  TransferMode,
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
  activeBatch: ActiveBatch | null
  batches: BatchInfo[]
  speed: number          // files/sec (rolling average)
  elapsed: number        // ms since start
  eta: number | null     // estimated ms remaining
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
  showNotification: (type: 'success' | 'error' | 'warning' | 'info', message: string) => void
  clearNotification: () => void

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
  activeBatch: null,
  batches: [],
  speed: 0,
  elapsed: 0,
  eta: null,
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
}

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
        scannedFiles: (d.scanned_files as number) ?? state.scan.scannedFiles,
        totalFound: (d.total_found as number) ?? state.scan.totalFound,
      }
      break
    }

    case 'scan_complete': {
      patch.scan = {
        isScanning: false,
        scannedFiles: (d.total_items as number) ?? state.scan.scannedFiles,
        totalFound: (d.total_items as number) ?? state.scan.totalFound,
      }
      patch.transfer = {
        ...state.transfer,
        totalItems: (d.total_items as number) ?? state.transfer.totalItems,
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
        total_items: (d.total_items as number) ?? 0,
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
        totalItems: (d.total_items as number) ?? 0,
        completedItems: 0,
        status: 'processing',
        hop1Progress: 0,
        hop1Status: 'pending',
        hop2Progress: 0,
        hop2Status: 'pending',
        startedAt: new Date().toISOString(),
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
        patch.transfer = {
          ...state.transfer,
          activeBatch: {
            ...active,
            hop1Progress: (d.progress as number) ?? active.hop1Progress,
            hop1Status: 'transferring',
            completedItems: (d.completed as number) ?? active.completedItems,
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
            completedItems: (d.cached as number) ?? active.completedItems,
          },
        }
      }
      break
    }

    // --- Hop 2 events (Cache -> Destination) ------------------------------
    case 'hop2_progress': {
      const active = state.transfer.activeBatch
      if (active) {
        patch.transfer = {
          ...state.transfer,
          activeBatch: {
            ...active,
            hop2Progress: (d.progress as number) ?? active.hop2Progress,
            hop2Status: 'transferring',
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
            (d.imported as number) ?? state.transfer.completedItems,
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

    case 'session_complete': {
      const td = d as Record<string, unknown>
      patch.transfer = {
        ...state.transfer,
        status: 'completed',
        completedItems: (td.completed_items as number) ?? state.transfer.completedItems,
        failedItems: (td.failed_items as number) ?? state.transfer.failedItems,
        activeBatch: null,
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
export const useTransferStore = create<TransferStore>((set) => ({
  // State
  transfer: { ...initialTransfer },
  duplicates: { ...initialDuplicates },
  scan: { ...initialScan },
  library: { ...initialLibrary },
  ui: { ...initialUI },
  wsConnected: false,

  // --- Transfer actions ---------------------------------------------------
  initTransfer: (session) =>
    set({
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
      },
    }),

  resetTransfer: () => set({ transfer: { ...initialTransfer } }),

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
    set((s) => ({
      library: {
        items: [...s.library.items, ...items],
        page: s.library.page + 1,
        totalPages: pages,
        total,
        isLoadingMore: false,
      },
    })),

  setLibraryPage: (page) =>
    set((s) => ({ library: { ...s.library, page } })),

  resetLibrary: () => set({ library: { ...initialLibrary } }),

  setLoadingMore: (loading) =>
    set((s) => ({ library: { ...s.library, isLoadingMore: loading } })),

  // --- UI actions ----------------------------------------------------------
  setCurrentPage: (page) =>
    set((s) => ({ ui: { ...s.ui, currentPage: page } })),

  showNotification: (type, message) =>
    set((s) => ({
      ui: { ...s.ui, notification: { type, message } },
    })),

  clearNotification: () =>
    set((s) => ({ ui: { ...s.ui, notification: null } })),

  // --- WS actions ---------------------------------------------------------
  setWsConnected: (connected) => set({ wsConnected: connected }),

  handleWsEvent: (event) =>
    set((state) => handleWsEventReducer(state, event) as TransferStore),
}))
