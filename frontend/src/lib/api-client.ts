// ---------------------------------------------------------------------------
// MediaVault v2 — Axios Client Configuration
// Base URL pulled dynamically from /api/config at runtime.
// ---------------------------------------------------------------------------

import axios from 'axios'

const apiClient = axios.create({
  baseURL: '/api',
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
