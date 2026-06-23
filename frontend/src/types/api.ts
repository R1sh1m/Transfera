// ---------------------------------------------------------------------------
// Transfera v2 — Shared TypeScript Types
// Mirrors backend Pydantic schemas exactly.
// ---------------------------------------------------------------------------

// --- Enums ----------------------------------------------------------------
export type SessionStatus = 'created' | 'running' | 'paused' | 'completed' | 'completed_with_errors' | 'failed' | 'cancelled'
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
  source_path?: string
  source_ref?: SourceRef
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
export type FolderLayout = 'year/month/day' | 'year/month' | 'flat'

export interface SessionCreate {
  session_name: string
  source_root?: string
  source_ref?: SourceRef
  dest_root: string
  transfer_mode: TransferMode
  only_new_since_last_import?: boolean
  selected_files?: string[] | null
  folder_layout?: FolderLayout
}

// --- Device Preview ----------------------------------------------------------
export interface DevicePreviewItem {
  id: string
  filename: string
  abs_path: string
  type: 'photo' | 'video'
  size_bytes: number
  mtime: number
  duration_s: number | null
  thumbnail_ready: boolean
}

export interface DevicePreviewResponse {
  total: number
  photos: number
  videos: number
  total_size_bytes: number
  page: number
  page_size: number
  pages: number
  items: DevicePreviewItem[]
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
  only_new_mode: boolean
  folder_layout: string
  created_at: string
  updated_at: string
  started_at?: string
  completed_at?: string
  total_bytes_volume?: number
  session_report_path?: string
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
  thumbnail_url?: string
  thumbnail_status: 'pending' | 'ready' | 'failed'
  date_taken?: string
  date_source?: 'exif' | 'file_modified'
  error_message?: string
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
  matched_item_id?: number
  matched_file_size?: number
  matched_date_taken?: string
  matched_thumbnail_url?: string
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

export interface PrescanCandidate {
  abs_path: string
  filename: string
  size_bytes: number
}

export interface PrescanResponse {
  checked: number
  likely_duplicate_count: number
  likely_duplicate_paths: string[]
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

// --- Disk Space (drive-level free space) -----------------------------------
export interface DiskSpaceRequest {
  path: string
}

export interface DiskSpaceResponse {
  path: string
  total_bytes: number
  used_bytes: number
  free_bytes: number
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
  | 'session_completed'
  | 'session_completed_with_errors'
  | 'error'
  | 'pong'

export interface WSEvent {
  event: WSEventType
  data: Record<string, unknown>
  timestamp: string
}

// --- Preflight Disk Validation --------------------------------------------
export interface PreflightValidateRequest {
  source_path?: string
  source_ref?: SourceRef
  dest_path: string
}

export interface PreflightValidateResponse {
  source_size_bytes: number
  dest_free_bytes: number
  is_sufficient: boolean
  file_count: number
}

// --- Path Validation (single path) ----------------------------------------
export interface PathValidateRequest {
  path: string
}

export interface PathValidateResponse {
  path: string
  exists: boolean
  is_dir: boolean
  readable: boolean
}

// --- Error ----------------------------------------------------------------
export interface ApiError {
  detail: string
  code?: string
}

// --- iOS Device -----------------------------------------------------------
export type IOSDeviceStatus = 'ready' | 'not_trusted' | 'locked' | 'no_driver' | 'not_found' | 'disconnected' | 'error'

export interface IOSDeviceInfo {
  serial: string
  name: string
  model: string
  ios_version: string
  connection_type: string
  status: IOSDeviceStatus
  error_detail?: string | null
  active_tier?: 'tier1' | 'tier2' | 'wpd' | null
}

export interface IOSDeviceListResponse {
  available: boolean
  driver_status: 'ready' | 'no_driver' | 'no_pymobiledevice3' | 'unknown'
  prefer_tier2: boolean
  devices: IOSDeviceInfo[]
}

export interface IOSBrowseRequest {
  serial: string
  path?: string
}

export interface IOSDeviceFileEntry {
  name: string
  path: string
  is_dir: boolean
  size: number
  mtime: number
}

export interface IOSBrowseResponse {
  serial: string
  path: string
  entries: IOSDeviceFileEntry[]
}

export const IOS_SOURCE_PREFIX = 'ios://' as const

export function isIOSDevicePath(path: string): boolean {
  return path.startsWith(IOS_SOURCE_PREFIX)
}

/**
 * Check if a device ID looks like a WPD PnP path instead of an iOS UDID.
 * WPD paths start with "\\\\?\\" and often contain "vid_" (USB VID).
 * Real iOS UDIDs are 40-char hex strings or "0000XXXX-XXXXXXXX" format.
 */
export function isWPDPath(deviceId: string): boolean {
  return deviceId.startsWith('\\\\?\\') || deviceId.includes('vid_')
}

export function parseIOSDevicePath(path: string): { serial: string; afcPath: string } | null {
  if (!isIOSDevicePath(path)) return null
  const withoutPrefix = path.slice(IOS_SOURCE_PREFIX.length)
  const slashIdx = withoutPrefix.indexOf('/')
  if (slashIdx === -1) {
    return { serial: withoutPrefix, afcPath: '/' }
  }
  return {
    serial: withoutPrefix.slice(0, slashIdx),
    afcPath: '/' + withoutPrefix.slice(slashIdx + 1),
  }
}

// --- Source References (discriminated union) --------------------------------
export type SourceRefLocal = {
  type: 'local_folder'
  path: string
}

export type SourceRefDevice = {
  type: 'device'
  device_id: string
  device_path: string
  device_name?: string
}

export type SourceRef = SourceRefLocal | SourceRefDevice

/**
 * Create a SourceRef from a legacy source path string.
 * Detects ios:// prefix and creates SourceRefDevice; otherwise SourceRefLocal.
 */
export function sourceRefFromString(sourceString: string): SourceRef {
  if (sourceString.startsWith(IOS_SOURCE_PREFIX)) {
    const withoutPrefix = sourceString.slice(IOS_SOURCE_PREFIX.length)
    const slashIdx = withoutPrefix.indexOf('/')
    if (slashIdx === -1) {
      return { type: 'device', device_id: withoutPrefix, device_path: '/' }
    }
    return {
      type: 'device',
      device_id: withoutPrefix.slice(0, slashIdx),
      device_path: '/' + withoutPrefix.slice(slashIdx + 1),
    }
  }
  return { type: 'local_folder', path: sourceString }
}

/**
 * Convert a SourceRef to a legacy string for display and DB storage.
 */
export function sourceRefToString(ref: SourceRef): string {
  if (ref.type === 'local_folder') return ref.path
  return `${IOS_SOURCE_PREFIX}${ref.device_id}${ref.device_path}`
}

/**
 * Check if a SourceRef represents a device source.
 */
export function isDeviceSourceRef(ref: SourceRef): ref is SourceRefDevice {
  return ref.type === 'device'
}

// --- Device Import State (incremental import tracking) ---------------------
export interface DeviceImportState {
  device_id: string
  device_name?: string
  last_successful_cutoff?: string
  last_import_session_id?: number
  updated_at: string
}

export interface DeviceImportStateListResponse {
  devices: DeviceImportState[]
}

// --- iOS Driver Installer -------------------------------------------------
export interface InstallerStatusResponse {
  winget_available: boolean
  winget_version?: string
  driver_status: string
}

export interface PackageVerificationResponse {
  success: boolean
  package_id?: string
  package_name?: string
  version?: string
  error?: string
}

export interface InstallDriverResponse {
  success: boolean
  exit_code: number | null
  error?: string
  message: string
}

// --- Tier 2 (WSL2 + usbipd-win) -------------------------------------------
export interface Tier2Status {
  wsl_installed: boolean
  distro_name?: string
  distro_ready: boolean
  usbipd_installed: boolean
  usbipd_version?: string
  bridge_running: boolean
  bridge_reachable: boolean
  virtualization_available: boolean
  restart_required: boolean
  active_tier: 'tier1' | 'tier2' | 'wpd' | 'none'
  devices_on_tier2: string[]
  error?: string
  bridge_error?: string
}

export interface Tier2StepPreview {
  step_id: string
  title: string
  description: string
  requires_restart: boolean
  requires_elevation: boolean
  elevation_description?: string
  restart_description?: string
  can_cancel: boolean
}

export interface Tier2SetupPreview {
  steps: Tier2StepPreview[]
  total_steps: number
  requires_restart: boolean
  requires_elevation: boolean
}

export interface Tier2StepResponse {
  step_id: string
  completed: boolean
  restart_required: boolean
  error?: string
  error_code?: string
  next_step?: string
  details: Record<string, unknown>
}

export interface Tier2BindPreview {
  busid: string
  device_name: string
  explanation: string
  requires_restart: boolean
  requires_elevation: boolean
  elevation_description: string
}

export interface Tier2BindExecuteResponse {
  busid: string
  bound: boolean
  attached: boolean
  confirmed_in_wsl: boolean
  error?: string
}

export interface Tier2USBDevice {
  busid: string
  vid_pid: string
  device_name: string
  state: string
  is_apple: boolean
}

export interface Tier2USBDeviceList {
  devices: Tier2USBDevice[]
}

export interface Tier2ResetResponse {
  reset: boolean
  message: string
  bridge_terminated: boolean
  prefer_tier2_reset: boolean
  persisted_state_cleared: boolean
  device_preferences_cleared: boolean
}

export interface Tier2ResumeNotification {
  steps_completed: string[]
  current_step: string
  message: string
}

export interface Tier2ElevatedCommand {
  executable: string
  args: string[]
  description: string
}

// --- Device Backend Preference -----------------------------------------------
export type DeviceAccessTier = 'tier1' | 'tier2' | 'wpd' | 'none'

export interface DevicePreferenceResponse {
  prefer_tier2: boolean
}

// --- Auto-Activation Status -------------------------------------------------
export interface DeviceBackendStatusResponse {
  apple_driver_installable: boolean
  apple_driver_package_name: string | null
  apple_driver_package_version: string | null
  pymobiledevice3_installable: boolean
  bridge_auto_started: boolean
  wsl_setup_suggested: boolean
  initializing: boolean
  active_tier: 'tier1' | 'wpd' | 'tier2' | 'none'
  tier2_available: boolean
  tier2_error: string | null
  ios_available: boolean
}

// --- iOS Device Recovery ----------------------------------------------------
export interface IOSDeviceRecoverResponse {
  overall: 'service_restored' | 'usb_passthrough_restored' | 'elevation_required' | 'needs_bind' | 'needs_elevation' | 'no_device_found' | 'no_recovery_needed'
  service: {
    state: string
    needs_elevation: boolean
    elevation_command: string[] | null
    exit_code: number | null
    message: string
    service_name: string
  }
  usb: {
    success: boolean
    apple_devices_found: number
    devices: Record<string, unknown>[]
    attach_errors: Record<string, unknown>[]
    needs_bind: string[]
    needs_elevation: boolean
  }
}

export interface Pymobiledevice3InstallResponse {
  success: boolean
  message: string
}

export interface DevicePreferenceRequest {
  prefer_tier2: boolean
}

// --- Session Progress (polling-based live data) ----------------------------
export interface SessionProgress {
  session_id: number
  status: SessionStatus
  total_items: number
  completed_items: number
  failed_items: number

  total_files: number
  cached_files: number
  imported_files: number
  failed_files: number
  current_batch: number
  total_batches: number
  progress_percent: number

  current_item_id: number | null
  current_file_name: string
  current_hop: string

  active_batch_id: number | null
  active_batch_number: number
  active_batch_status: string
  active_batch_total: number
  active_batch_completed: number
  active_batch_hop1_progress: number
  active_batch_hop2_progress: number

  recent_items: RecentItemProgress[]

  started_at: string | null
  completed_at: string | null

  elapsed_seconds: number
  eta_seconds: number | null
  speed_files_per_sec: number
}

export interface RecentItemProgress {
  item_id: number
  file_name: string
  hop1_status: string
  hop2_status: string
  thumbnail_url: string | null
  updated_at: string
}

// --- Clear / Purge --------------------------------------------------------
export interface ClearSessionsRequest {
  older_than_days?: number
}

export interface ClearResponse {
  message: string
  sessions_cleared: number
  batches_cleared: number
  media_items_cleared: number
  thumbnails_removed: number
}
