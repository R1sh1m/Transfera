import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

const PROCESS_START_TIME = Date.now()
const STARTUP_GRACE_MS = 25000 // 25s covers worst-case device manager init

function suppressStartupErrors(proxy: any) {
  proxy.on('error', (err: any, _req: any, res: any) => {
    const isStartupError =
      (err.code === 'ECONNREFUSED' || err.code === 'ECONNRESET') &&
      Date.now() - PROCESS_START_TIME < STARTUP_GRACE_MS

    if (isStartupError) {
      // Return a clean 503 to the frontend instead of crashing the proxy
      // The frontend's health polling and React Query retry logic handles this
      try {
        if (res && typeof res.writeHead === 'function' && !res.headersSent) {
          res.writeHead(503, { 'Content-Type': 'application/json' })
          res.end(JSON.stringify({ status: 'starting' }))
        }
      } catch {
        // res may already be closed — ignore
      }
      return // Do NOT propagate to Vite's error logger
    }

    // After grace period: log real errors (backend crashed, wrong port, etc.)
    console.error(`[proxy] ${err.code}: ${err.message}`)
  })
}

export default defineConfig({
  base: './',
  plugins: [tailwindcss(), react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:47821',
        changeOrigin: true,
        configure: suppressStartupErrors,
      },
      '/ws': {
        target: 'http://127.0.0.1:47821',
        ws: true,
        changeOrigin: true,
        configure: suppressStartupErrors,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
