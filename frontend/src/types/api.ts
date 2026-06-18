// ---------------------------------------------------------------------------
// Transfera v2 — Shared TypeScript Types
// Mirrors backend Pydantic schemas exactly.
// ---------------------------------------------------------------------------

// --- Enums ----------------------------------------------------------------
export type SessionStatus = 'created' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled'
export type HopStatus = 'pending' | 'scanning' | 'scanned' | 'hashing' | 'hashed' | 'transferring' | 'completed' | 'failed' | 'skipped'
export type BatchStatus = 'pending' | 'processing' | 'loading' | 'archived' | 'completed' | 'failed' | 'partial'

// --- Health ---------------------------------------------------------------
export interface HealthResponse {
  status: string
  version: string
  port: number
  database: string
}

// --- Config ---------------------------------------------------------------
export interface ConfigResponse {
  port: number
  host: string
  batch_size: number
  max_retry: number
  cache_dir: string
  db_dir: string
  image_extensions: string[]
  video_extensions: string[]
  audio_extensions: string[]
  document_extensions: string[]
}

// --- Scan -----------------------------------------------------------------
export interface ScanRequest {
  source_path: string
  session_name?: string
  dest_path?: string
}

export interface ScanResponse {
  session_id: number
  status: string
  message: string
}

// --- Session --------------------------------------------------------------
export type TransferMode = 'copy' | 'move'

export interface SessionCreate {
  session_name: string
  source_root: string
  dest_root: string
  transfer_mode: TransferMode
}

export interface SessionInfo {
  id: number
  session_name: string
  source_root: string
  dest_root: string
  transfer_mode: TransferMode
  status: SessionStatus
  total_items: number
  completed_items: number
  failed_items: number
  created_at: string
  updated_at: string
  started_at?: string
  completed_at?: string
}

export interface SessionList {
  sessions: SessionInfo[]
  total: number
}

export interface SessionActionResponse {
  session_id: number
  status: string
  message: string
}

// --- Batch ----------------------------------------------------------------
export interface BatchInfo {
  id: number
  session_id: number
  batch_number: number
  status: BatchStatus
  total_items: number
  completed_items: number
  failed_items: number
  created_at: string
  updated_at: string
}

export interface BatchList {
  batches: BatchInfo[]
  total: number
}

// --- Media Item -----------------------------------------------------------
export interface MediaItemInfo {
  id: number
  source_path: string
  file_name: string
  file_size: number
  extension?: string
  mime_type?: string
  hop1_status: HopStatus
  hop2_status: HopStatus
  final_status: HopStatus
  live_photo_group?: string
  created_at: string
  updated_at: string
}

export interface MediaList {
  items: MediaItemInfo[]
  total: number
  page: number
  page_size: number
  pages: number
}

// --- Duplicate ------------------------------------------------------------
export interface DuplicateEntry {
  item_id: number
  file_name: string
  source_path: string
  source_hash?: string
  file_size: number
  match_type: 'exact' | 'potential'
  matched_path?: string
}

export interface DuplicateReport {
  batch_id: number
  session_id: number
  checked_at: string
  exact_duplicates: DuplicateEntry[]
  potential_duplicates: DuplicateEntry[]
  total_items_checked: number
  processing_paused: boolean
  summary: string
}

export interface DuplicateCheckRequest {
  batch_id: number
}

// --- Duplicate Resolution -------------------------------------------------
export type DuplicateAction = 'skip' | 'overwrite' | 'keep_both'

export interface DuplicateResolution {
  item_id: number
  action: DuplicateAction
}

// --- Directory Size --------------------------------------------------------
export interface DirSizeRequest {
  path: string
}

export interface DirSizeResponse {
  path: string
  total_bytes: number
  file_count: number
  folder_count: number
  readable: string
}

// --- Folder Metadata -------------------------------------------------------
export interface FolderMetadataRequest {
  path: string
}

export interface FolderMetadataResponse {
  path: string
  size_gb: number
  file_count: number
}

// --- WebSocket Events (15 system-wide) ------------------------------------
export type WSEventType =
  | 'scan_progress'
  | 'scan_complete'
  | 'batch_created'
  | 'batch_processing'
  | 'batch_complete'
  | 'hop1_progress'
  | 'hop1_complete'
  | 'hop2_progress'
  | 'hop2_complete'
  | 'duplicates_detected'
  | 'duplicates_resolved'
  | 'session_started'
  | 'session_paused'
  | 'session_complete'
  | 'error'
  | 'pong'

export interface WSEvent {
  event: WSEventType
  data: Record<string, unknown>
  timestamp: string
}

// --- Error ----------------------------------------------------------------
export interface ApiError {
  detail: string
  code?: string
}
