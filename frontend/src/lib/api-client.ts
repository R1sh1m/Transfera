// ---------------------------------------------------------------------------
// Transfera v2 — Axios Client Configuration
// Dynamically resolves the API base URL from the current origin so the app
// works identically when served by FastAPI (port 47821) or Vite dev (5173).
// ---------------------------------------------------------------------------

import axios from 'axios'

// In packaged Electron (file:// protocol), window.location.origin is null.
// Fall back to direct backend connection on port 47821.
const API_BASE_URL = window.location.origin || 'http://127.0.0.1:47821'

// Augment Axios config to support our retry flag
declare module 'axios' {
  interface InternalAxiosRequestConfig {
    _retried?: boolean
  }
}

let _localToken: string | null = null
let _tokenFetchInProgress = false

/**
 * Fetch the local secret token from /api/config.
 * Retries with exponential backoff (500ms → 1s → 2s → 4s … up to 16s) until the
 * backend is ready and the token is obtained. Idempotent: if a fetch is already
 * in progress, the second call is a no-op.
 */
async function fetchLocalToken(): Promise<void> {
  if (_tokenFetchInProgress) return
  _tokenFetchInProgress = true
  let delay = 500
  while (!_localToken) {
    try {
      const r = await fetch(`${API_BASE_URL}/api/config`)
      if (r.ok) {
        const cfg = await r.json()
        if (cfg.local_secret_token) {
          _localToken = cfg.local_secret_token
          break
        }
      }
    } catch {
      // backend not yet ready — retry after delay
    }
    await new Promise((res) => setTimeout(res, delay))
    delay = Math.min(delay * 2, 16000)
  }
  _tokenFetchInProgress = false
}

// Kick off token fetch immediately on module load (non-blocking).
fetchLocalToken()

const apiClient = axios.create({
  baseURL: `${API_BASE_URL}/api`,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

// Inject the local secret token on every request so destructive endpoints
// are always authenticated.  The token is loaded lazily from /api/config
// at startup; if the fetch hasn't completed yet the first few requests
// may go without it, but /api/config itself is unprotected and any actual
// destructive call will happen after startup is complete.
apiClient.interceptors.request.use((config) => {
  if (_localToken) {
    config.headers.set('X-Local-Token', _localToken)
  }
  return config
})

// Response interceptor: normalize errors, retry on 403 token failures
// Preserves the original AxiosError so isAxiosError() checks downstream
// still work — we only enrich the message with the backend detail if present.
apiClient.interceptors.response.use(
  (res) => res,
  async (err) => {
    const detail = err.response?.data?.detail
    const isTokenError =
      err.response?.status === 403 &&
      typeof detail === 'string' &&
      detail.toLowerCase().includes('local token')

    // If the token was missing/stale, re-fetch it and retry the original request
    // exactly once. This recovers from the startup race without requiring a page reload.
    if (isTokenError && !err.config?._retried) {
      _localToken = null // clear stale value
      await fetchLocalToken()
      if (_localToken) {
        // Mark the retry so we don't loop on a genuine auth failure
        const retryConfig = { ...err.config, _retried: true }
        retryConfig.headers = { ...retryConfig.headers, 'X-Local-Token': _localToken }
        return apiClient(retryConfig)
      }
    }

    // Normal error enrichment
    if (typeof detail === 'string') {
      err.message = detail
    } else if (Array.isArray(detail)) {
      // Pydantic validation errors: [{ loc: [...], msg: "...", type: "..." }]
      err.message = detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join('; ')
    }
    return Promise.reject(err)
  },
)

export default apiClient
