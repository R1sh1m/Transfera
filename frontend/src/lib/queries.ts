// ---------------------------------------------------------------------------
// Transfera v2 — React Query Hooks
// Server-state bindings for all backend endpoints.
// ---------------------------------------------------------------------------

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import apiClient from './api-client'
import type {
  ConfigResponse,
  DirSizeResponse,
  FolderMetadataResponse,
  HealthResponse,
  PreflightValidateResponse,
  ScanRequest,
  ScanResponse,
  SessionCreate,
  SessionInfo,
  SessionList,
  SessionActionResponse,
  BatchList,
  MediaList,
  DuplicateCheckRequest,
  DuplicateReport,
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
// Media Library
// ---------------------------------------------------------------------------
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
        params: { page, page_size: pageSize, ...rest },
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
  })
}

// ---------------------------------------------------------------------------
// Preflight Disk Validation
// ---------------------------------------------------------------------------
export function usePreflightValidate(sourcePath: string | null, destPath: string | null) {
  return useQuery({
    queryKey: ['preflight', sourcePath, destPath],
    queryFn: async () => {
      const { data } = await apiClient.post<PreflightValidateResponse>(
        '/utils/preflight-validate',
        { source_path: sourcePath, dest_path: destPath },
      )
      return data
    },
    enabled:
      !!sourcePath &&
      sourcePath.trim().length > 0 &&
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
    },
  })
}
