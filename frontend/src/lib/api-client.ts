// ---------------------------------------------------------------------------
// Transfera v2 — Axios Client Configuration
// Dynamically resolves the API base URL from the current origin so the app
// works identically when served by FastAPI (port 47821) or Vite dev (5173).
// ---------------------------------------------------------------------------

import axios from 'axios'

// In packaged Electron (file:// protocol), window.location.origin is null.
// Fall back to direct backend connection on port 47821.
const API_BASE_URL = window.location.origin || 'http://127.0.0.1:47821'

// Fetch the local secret token from the backend config on startup.
// This token is required by destructive endpoints (clear, recover, etc.)
// to prevent unauthorized calls from other local processes.
let _localToken: string | null = null
fetch(`${API_BASE_URL}/api/config`)
  .then((r) => r.json())
  .then((cfg) => { _localToken = cfg.local_secret_token })
  .catch(() => { /* pre-flight is best-effort */ })

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

// Response interceptor: normalize errors
// Preserves the original AxiosError so isAxiosError() checks downstream
// still work — we only enrich the message with the backend detail if present.
apiClient.interceptors.response.use(
  (res) => res,
  (err) => {
    const detail = err.response?.data?.detail
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
