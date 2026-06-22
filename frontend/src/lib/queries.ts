// ---------------------------------------------------------------------------
// Transfera v2 — React Query Hooks
// Server-state bindings for all backend endpoints.
// ---------------------------------------------------------------------------

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import apiClient from './api-client'
import { extractErrorMessage } from './utils'
import { useTransferStore } from '@/store/transfer'
import type {
  ClearResponse,
  ClearSessionsRequest,
  ConfigResponse,
  DeviceBackendStatusResponse,
  DeviceImportStateListResponse,
  DevicePreferenceRequest,
  DevicePreferenceResponse,
  DirSizeResponse,
  DiskSpaceResponse,
  FolderMetadataResponse,
  HealthResponse,
  InstallDriverResponse,
  InstallerStatusResponse,
  IOSBrowseResponse,
  IOSDeviceListResponse,
  PackageVerificationResponse,
  PathValidateResponse,
  PreflightValidateResponse,
  ScanRequest,
  ScanResponse,
  SessionCreate,
  SessionInfo,
  SessionList,
  SessionActionResponse,
  SessionProgress,
  BatchList,
  MediaList,
  DuplicateCheckRequest,
  DuplicateReport,
  DuplicateResolution,
  SourceRef,
  Tier2Status,
  Tier2SetupPreview,
  Tier2StepResponse,
  Tier2BindExecuteResponse,
  Tier2BindPreview,
  Tier2USBDeviceList,
  Tier2ElevatedCommand,
  Tier2ResetResponse,
} from '@/types/api'

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
export function useConfig() {
  return useQuery({
    queryKey: ['config'],
    queryFn: async () => {
      const { data } = await apiClient.get<ConfigResponse>('/config')
      return data
    },
    staleTime: Infinity,
    retry: 3,
  })
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------
export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: async () => {
      const { data } = await apiClient.get<HealthResponse>('/health')
      return data
    },
    refetchInterval: 10000,
    retry: 3,
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
    refetchOnReconnect: true,
  })
}

// ---------------------------------------------------------------------------
// Scan
// ---------------------------------------------------------------------------
export function useScan() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req: ScanRequest) => {
      const { data } = await apiClient.post<ScanResponse>('/scan', req)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------
export function useSessionList(page = 1, pageSize = 20) {
  return useQuery({
    queryKey: ['sessions', page, pageSize],
    queryFn: async () => {
      const { data } = await apiClient.get<SessionList>('/sessions', {
        params: { page, page_size: pageSize },
      })
      return data
    },
    refetchInterval: 5000,
  })
}

export function useSession(id: number | null) {
  return useQuery({
    queryKey: ['session', id],
    queryFn: async () => {
      const { data } = await apiClient.get<SessionInfo>(`/sessions/${id}`)
      return data
    },
    enabled: id !== null,
    refetchInterval: 3000,
  })
}

export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req: SessionCreate) => {
      const { data } = await apiClient.post<SessionInfo>('/sessions', req)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
      useTransferStore.getState().showNotification('success', 'Backup session created')
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useStartSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (sessionId: number) => {
      const { data } = await apiClient.post<SessionActionResponse>(
        `/sessions/${sessionId}/start`,
      )
      return data
    },
    onSuccess: (_data, sessionId) => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
      qc.invalidateQueries({ queryKey: ['session', sessionId] })
      useTransferStore.getState().showNotification('success', 'Backup started')
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function usePauseSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (sessionId: number) => {
      const { data } = await apiClient.post<SessionActionResponse>(
        `/sessions/${sessionId}/pause`,
      )
      return data
    },
    onSuccess: (_data, sessionId) => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
      qc.invalidateQueries({ queryKey: ['session', sessionId] })
      useTransferStore.getState().showNotification('success', 'Backup paused')
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useCancelSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (sessionId: number) => {
      const { data } = await apiClient.post<SessionActionResponse>(
        `/sessions/${sessionId}/cancel`,
      )
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
      useTransferStore.getState().showNotification('success', 'Backup cancelled')
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useResolveDuplicates() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      sessionId,
      batchId,
      resolutions,
    }: {
      sessionId: number
      batchId: number
      resolutions: DuplicateResolution[]
    }) => {
      const { data } = await apiClient.post<SessionActionResponse>(
        `/sessions/${sessionId}/duplicates/resolve`,
        { batch_id: batchId, resolutions },
      )
      return data
    },
    onSuccess: (_data, { sessionId }) => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
      qc.invalidateQueries({ queryKey: ['session', sessionId] })
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

// ---------------------------------------------------------------------------
// Clear / Purge
// ---------------------------------------------------------------------------
export function useClearSessions() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req?: ClearSessionsRequest) => {
      const { data } = await apiClient.post<ClearResponse>('/sessions/clear', req ?? {})
      return data
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
      useTransferStore.getState().showNotification('success', data.message)
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useClearLibrary() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.post<ClearResponse>('/media/clear')
      return data
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['media'] })
      useTransferStore.getState().resetLibrary()
      useTransferStore.getState().showNotification('success', data.message)
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

// ---------------------------------------------------------------------------
// Batches
// ---------------------------------------------------------------------------
export function useSessionBatches(sessionId: number | null) {
  return useQuery({
    queryKey: ['batches', sessionId],
    queryFn: async () => {
      const { data } = await apiClient.get<BatchList>(
        `/sessions/${sessionId}/batches`,
      )
      return data
    },
    enabled: sessionId !== null,
    refetchInterval: 3000,
  })
}

// ---------------------------------------------------------------------------
// Session Progress (polling-based authoritative live data)
// ---------------------------------------------------------------------------
export function useSessionProgress(sessionId: number | null) {
  const isTerminal = (s: string) =>
    ['completed', 'completed_with_errors', 'failed', 'cancelled'].includes(s)
  const status = useTransferStore((s) => s.transfer.status)
  const shouldPoll = sessionId !== null && !isTerminal(status)

  return useQuery({
    queryKey: ['session-progress', sessionId],
    queryFn: async () => {
      const { data } = await apiClient.get<SessionProgress>(
        `/sessions/${sessionId}/progress`,
      )
      return data
    },
    enabled: shouldPoll,
    refetchInterval: shouldPoll ? 750 : false,
    staleTime: 500,
  })
}

// ---------------------------------------------------------------------------
// Media Library
// ---------------------------------------------------------------------------

/** Convert camelCase keys to snake_case for API compatibility. */
function toSnakeParams(
  obj: Record<string, unknown>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(obj)) {
    const snake = key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)
    result[snake] = value
  }
  return result
}

export function useMediaList(params: {
  page?: number
  pageSize?: number
  sessionId?: number
  hop1Status?: string
  hop2Status?: string
  finalStatus?: string
  extension?: string
  search?: string
}) {
  const { page = 1, pageSize = 50, ...rest } = params
  return useQuery({
    queryKey: ['media', page, pageSize, rest],
    queryFn: async () => {
      const { data } = await apiClient.get<MediaList>('/media', {
        params: { page, page_size: pageSize, ...toSnakeParams(rest) },
      })
      return data
    },
  })
}

// ---------------------------------------------------------------------------
// Directory Size Metrics
// ---------------------------------------------------------------------------
export function useDirSize(path: string | null, enabled = true) {
  return useQuery({
    queryKey: ['dir-size', path],
    queryFn: async () => {
      const { data } = await apiClient.post<DirSizeResponse>('/utils/dir-size', { path })
      return data
    },
    enabled: enabled && !!path && path.trim().length > 0,
    refetchInterval: 30000,
    retry: 1,
    staleTime: 15000,
  })
}

// ---------------------------------------------------------------------------
// Disk Space (drive-level free space)
// ---------------------------------------------------------------------------
export function useDiskSpace(path: string | null) {
  return useQuery({
    queryKey: ['disk-space', path],
    queryFn: async () => {
      const { data } = await apiClient.post<DiskSpaceResponse>('/utils/disk-space', { path })
      return data
    },
    enabled: !!path && path.trim().length > 0,
    refetchInterval: 60000,
    retry: 1,
    staleTime: 30000,
  })
}

// ---------------------------------------------------------------------------
// Folder Metadata (lightweight size + count for dashboard)
// ---------------------------------------------------------------------------
export function useFolderMetadata(path: string | null) {
  return useQuery({
    queryKey: ['folder-metadata', path],
    queryFn: async () => {
      const { data } = await apiClient.post<FolderMetadataResponse>('/utils/folder-metadata', { path })
      return data
    },
    enabled: !!path && path.trim().length > 0,
    refetchInterval: 30000,
    retry: 1,
    staleTime: 15000,
  })
}

// ---------------------------------------------------------------------------
// Duplicates
// ---------------------------------------------------------------------------
export function useCheckDuplicates() {
  return useMutation({
    mutationFn: async (req: DuplicateCheckRequest) => {
      const { data } = await apiClient.post<DuplicateReport>(
        '/duplicates/check',
        req,
      )
      return data
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

// ---------------------------------------------------------------------------
// Preflight Disk Validation
// ---------------------------------------------------------------------------
export function usePreflightValidate(sourcePath: string | null, destPath: string | null, sourceRef?: SourceRef | null) {
  return useQuery({
    queryKey: ['preflight', sourcePath, destPath, sourceRef],
    queryFn: async () => {
      const { data } = await apiClient.post<PreflightValidateResponse>(
        '/utils/preflight-validate',
        sourceRef
          ? { source_ref: sourceRef, dest_path: destPath! }
          : { source_path: sourcePath || '', dest_path: destPath! },
      )
      return data
    },
    enabled:
      (!!sourcePath || !!sourceRef) &&
      !!destPath &&
      destPath.trim().length > 0,
    retry: 1,
    staleTime: 10000,
  })
}

// ---------------------------------------------------------------------------
// Recovery
// ---------------------------------------------------------------------------
export function useRecovery() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.post<SessionActionResponse>('/recovery')
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['sessions'] })
      useTransferStore.getState().showNotification('success', 'Recovery completed')
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

// ---------------------------------------------------------------------------
// Path Validation (single path existence check)
// ---------------------------------------------------------------------------
export function useValidatePath(path: string | null) {
  return useQuery({
    queryKey: ['validate-path', path],
    queryFn: async () => {
      const { data } = await apiClient.post<PathValidateResponse>('/utils/validate-path', { path })
      return data
    },
    enabled: !!path && path.trim().length > 0,
    retry: false,
    staleTime: 5000,
  })
}

// ---------------------------------------------------------------------------
// iOS Device Support
// ---------------------------------------------------------------------------
export function useIOSDevices(enabled = true) {
  return useQuery({
    queryKey: ['ios-devices'],
    queryFn: async () => {
      const { data } = await apiClient.get<IOSDeviceListResponse>('/ios-devices')
      return data
    },
    refetchInterval: 5000,
    staleTime: 3000,
    enabled,
  })
}

// ---------------------------------------------------------------------------
// Device Backend Preference (Tier 1 vs Tier 2)
// ---------------------------------------------------------------------------
export function useDevicePreference() {
  return useQuery({
    queryKey: ['device-preference'],
    queryFn: async () => {
      const { data } = await apiClient.get<DevicePreferenceResponse>('/device-preference')
      return data
    },
    staleTime: Infinity,
  })
}

export function useSetDevicePreference() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req: DevicePreferenceRequest) => {
      const { data } = await apiClient.post<DevicePreferenceResponse>('/device-preference', req)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['device-preference'] })
      qc.invalidateQueries({ queryKey: ['ios-devices'] })
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useIOSBrowse(serial: string | null, path: string = '/') {
  return useQuery({
    queryKey: ['ios-browse', serial, path],
    queryFn: async () => {
      const { data } = await apiClient.post<IOSBrowseResponse>('/ios-devices/browse', {
        serial,
        path,
      })
      return data
    },
    enabled: !!serial,
    retry: false,
    staleTime: 5000,
  })
}

// ---------------------------------------------------------------------------
// Device Import State (incremental import tracking)
// ---------------------------------------------------------------------------
export function useDeviceImportStateList() {
  return useQuery({
    queryKey: ['device-import-states'],
    queryFn: async () => {
      const { data } = await apiClient.get<DeviceImportStateListResponse>('/device-import-state')
      return data
    },
    staleTime: 30000,
  })
}

export function useDeviceImportState(deviceId: string | null) {
  return useQuery({
    queryKey: ['device-import-state', deviceId],
    queryFn: async () => {
      const { data } = await apiClient.get(`/device-import-state/${encodeURIComponent(deviceId!)}`)
      return data
    },
    enabled: !!deviceId,
    retry: false,
    staleTime: 10000,
  })
}

export function useClearDeviceImportState() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (deviceId: string) => {
      const { data } = await apiClient.delete(`/device-import-state/${encodeURIComponent(deviceId)}`)
      return data
    },
    onSuccess: (_data, deviceId) => {
      qc.invalidateQueries({ queryKey: ['device-import-states'] })
      qc.invalidateQueries({ queryKey: ['device-import-state', deviceId] })
      useTransferStore.getState().showNotification('success', 'Device import state cleared — next import will be a full scan')
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

// ---------------------------------------------------------------------------
// Device Backend Auto-Activation Status
// ---------------------------------------------------------------------------
export function useDeviceBackendStatus() {
  return useQuery({
    queryKey: ['device-backend-status'],
    queryFn: async () => {
      const { data } = await apiClient.get<DeviceBackendStatusResponse>('/device-backend/status')
      return data
    },
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
}

// ---------------------------------------------------------------------------
// iOS Driver Installer
// ---------------------------------------------------------------------------
export function useInstallerStatus() {
  return useQuery({
    queryKey: ['installer-status'],
    queryFn: async () => {
      const { data } = await apiClient.get<InstallerStatusResponse>('/ios-driver/installer-status')
      return data
    },
    staleTime: 30_000,
  })
}

export function useVerifyPackage() {
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.post<PackageVerificationResponse>('/ios-driver/verify-package')
      return data
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useInstallDriver() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.post<InstallDriverResponse>('/ios-driver/install')
      return data
    },
    onSuccess: () => {
      // Invalidate iOS device queries so they re-check driver status
      qc.invalidateQueries({ queryKey: ['ios-devices'] })
      qc.invalidateQueries({ queryKey: ['installer-status'] })
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

// ---------------------------------------------------------------------------
// Tier 2 (WSL2 + usbipd-win) Device Support
// ---------------------------------------------------------------------------
export function useTier2Status() {
  return useQuery({
    queryKey: ['tier2-status'],
    queryFn: async () => {
      const { data } = await apiClient.get<Tier2Status>('/tier2/status')
      return data
    },
    refetchInterval: 10000,
    staleTime: 5000,
  })
}

export function useTier2SetupPreview() {
  return useQuery({
    queryKey: ['tier2-preview'],
    queryFn: async () => {
      const { data } = await apiClient.get<Tier2SetupPreview>('/tier2/preview')
      return data
    },
    staleTime: 30000,
  })
}

export function useTier2ExecuteStep() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req: { step_id: string; confirmed?: boolean }) => {
      // Setup steps (provision_linux, start_bridge, etc.) can take
      // well over 30s on first run — override the default axios timeout
      // with a generous 5-minute ceiling.
      const { data } = await apiClient.post<Tier2StepResponse>('/tier2/setup', req, { timeout: 300000 })
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tier2-status'] })
      qc.invalidateQueries({ queryKey: ['ios-devices'] })
    },
    // No onError toast here — callers (Tier2SetupPanel) handle errors inline
    // in their own try/catch blocks with contextual error banners.  Adding a
    // separate toast here would show the same failure twice in two different
    // UI surfaces, which is confusing rather than helpful.
  })
}

export function useTier2USBDevices() {
  return useQuery({
    queryKey: ['tier2-usb-devices'],
    queryFn: async () => {
      const { data } = await apiClient.get<Tier2USBDeviceList>('/tier2/usb-devices')
      return data
    },
    refetchInterval: 5000,
    staleTime: 3000,
  })
}

export function useTier2BindPreview() {
  return useMutation({
    mutationFn: async (req: { busid: string; serial?: string }) => {
      const { data } = await apiClient.post<Tier2BindPreview>('/tier2/devices/bind-preview', req)
      return data
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useTier2BindExecute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req: { busid: string; confirmed: boolean }) => {
      const { data } = await apiClient.post<Tier2BindExecuteResponse>('/tier2/devices/bind-execute', req)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ios-devices'] })
      qc.invalidateQueries({ queryKey: ['tier2-usb-devices'] })
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useTier2BindElevated() {
  return useMutation({
    mutationFn: async (req: { busid: string; serial?: string }) => {
      const { data } = await apiClient.post<Tier2ElevatedCommand>('/tier2/devices/bind-elevated', req)
      return data
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useTier2Cancel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.post('/tier2/cancel')
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tier2-status'] })
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useTier2Reset() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.post<Tier2ResetResponse>('/tier2/reset', {}, { timeout: 30000 })
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tier2-status'] })
      qc.invalidateQueries({ queryKey: ['tier2-preview'] })
      qc.invalidateQueries({ queryKey: ['device-preference'] })
    },
    onError: (error) => {
      useTransferStore.getState().showNotification('error', extractErrorMessage(error))
    },
  })
}

export function useTier2Resume() {
  return useQuery({
    queryKey: ['tier2-resume'],
    queryFn: async () => {
      const { data } = await apiClient.get('/tier2/resume')
      return data
    },
    staleTime: 60000,
  })
}
