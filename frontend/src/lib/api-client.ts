// ---------------------------------------------------------------------------
// MediaVault v2 — Axios Client Configuration
// Dynamically resolves the API base URL from the current origin so the app
// works identically when served by FastAPI (port 47821) or Vite dev (5173).
// ---------------------------------------------------------------------------

import axios from 'axios'

const API_BASE_URL = window.location.origin

const apiClient = axios.create({
  baseURL: `${API_BASE_URL}/api`,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

// Response interceptor: normalize errors
apiClient.interceptors.response.use(
  (res) => res,
  (err) => {
    const message =
      err.response?.data?.detail ?? err.message ?? 'Unknown error'
    return Promise.reject(new Error(message))
  },
)

export default apiClient
