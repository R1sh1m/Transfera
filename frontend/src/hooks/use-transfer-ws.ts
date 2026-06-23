// ---------------------------------------------------------------------------
// Transfera v2 — WebSocket Hook
// Manages connection to /ws/transfer/{sessionId} with auto-reconnect.
// Surfaces connection errors to the store so the UI can display them.
// ---------------------------------------------------------------------------

import { useEffect, useRef, useCallback } from 'react'
import { useTransferStore } from '@/store/transfer'
import type { WSEvent } from '@/types/api'

const WS_RECONNECT_DELAY = 3000
const WS_MAX_RECONNECT = 10

export function useTransferWs(sessionId: number | null) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectCount = useRef(0)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const handleWsEvent = useTransferStore((s) => s.handleWsEvent)
  const setWsConnected = useTransferStore((s) => s.setWsConnected)
  const setWsError = useTransferStore((s) => s.setWsError)

  const cleanup = useCallback(() => {
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = null
    }
    if (wsRef.current) {
      wsRef.current.onopen = null
      wsRef.current.onmessage = null
      wsRef.current.onclose = null
      wsRef.current.onerror = null
      wsRef.current.close()
      wsRef.current = null
    }
    setWsConnected(false)
  }, [setWsConnected])

  const connect = useCallback(() => {
    if (!sessionId) return
    cleanup()

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    // In packaged Electron (file:// protocol), window.location.host is empty.
    // Fall back to direct backend connection on port 47821.
    const host = window.location.host || '127.0.0.1:47821'
    const url = `${protocol}//${host}/ws/transfer/${sessionId}`

    let ws: WebSocket
    try {
      ws = new WebSocket(url)
    } catch (err) {
      console.warn('[ws] Failed to create WebSocket connection:', err)
      return
    }
    wsRef.current = ws

    ws.onopen = () => {
      setWsConnected(true)
      setWsError(null)
      reconnectCount.current = 0
      console.log('[ws] Connected to session', sessionId)
    }

    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data as string) as {
          event: string
          data: Record<string, unknown>
          timestamp: string
        }

        if (event.event === 'ping') {
          ws.send(JSON.stringify({ event: 'pong', data: {}, timestamp: new Date().toISOString() }))
          return
        }

        handleWsEvent(event as WSEvent)
      } catch {
        // malformed message — ignore
      }
    }

    ws.onclose = (event) => {
      setWsConnected(false)
      console.warn('[ws] Disconnected:', event.reason || 'connection closed', '(code:', event.code, ')')

      if (reconnectCount.current < WS_MAX_RECONNECT) {
        reconnectTimer.current = setTimeout(() => {
          reconnectCount.current += 1
          connect()
        }, WS_RECONNECT_DELAY)
      }
    }

    ws.onerror = (event) => {
      console.warn('[ws] Error:', event)
      ws.close()
    }
  }, [sessionId, handleWsEvent, setWsConnected, setWsError, cleanup])

  useEffect(() => {
    if (sessionId) {
      connect()
    }
    return cleanup
  }, [sessionId, connect, cleanup])
}
