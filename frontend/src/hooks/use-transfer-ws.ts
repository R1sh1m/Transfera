// ---------------------------------------------------------------------------
// Transfera v2 — WebSocket Hook
// Manages connection to /ws/transfer/{sessionId} with auto-reconnect.
// Surfaces connection errors to the store so the UI can display them.
// Stops reconnecting when the session reaches a terminal status or the
// backend rejects the connection (403 / session not found).
// ---------------------------------------------------------------------------

import { useEffect, useRef, useCallback } from 'react'
import { useTransferStore } from '@/store/transfer'
import type { WSEvent } from '@/types/api'

const WS_RECONNECT_DELAY = 3000
const WS_MAX_RECONNECT = 10

const TERMINAL_STATUSES = new Set([
  'completed',
  'completed_with_errors',
  'failed',
  'cancelled',
])

export function useTransferWs(sessionId: number | null) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectCount = useRef(0)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const connectingRef = useRef(false)
  const shouldReconnectRef = useRef(true)
  const handleWsEvent = useTransferStore((s) => s.handleWsEvent)
  const setWsConnected = useTransferStore((s) => s.setWsConnected)
  const setWsError = useTransferStore((s) => s.setWsError)
  const sessionStatus = useTransferStore((s) => s.transfer.status)
  const sessionStatusRef = useRef(sessionStatus)
  useEffect(() => { sessionStatusRef.current = sessionStatus }, [sessionStatus])

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
    if (!sessionId || connectingRef.current) return

    // Do not reconnect if session is in a terminal state
    if (TERMINAL_STATUSES.has(sessionStatusRef.current) || !shouldReconnectRef.current) {
      cleanup()
      return
    }

    connectingRef.current = true
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
      connectingRef.current = false
      return
    }
    wsRef.current = ws

    ws.onopen = () => {
      connectingRef.current = false
      setWsConnected(true)
      setWsError(null)
      reconnectCount.current = 0
      shouldReconnectRef.current = true
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
      connectingRef.current = false
      setWsConnected(false)
      console.warn('[ws] Disconnected:', event.reason || 'connection closed', '(code:', event.code, ')')

      // Code 1008 (Policy Violation) or 4003 → server rejected the connection
      // (e.g. session not found or in terminal state) — stop reconnecting.
      if (event.code === 1008 || event.code === 4003) {
        shouldReconnectRef.current = false
        return
      }

      // If session status has become terminal since we connected, stop reconnecting
      if (TERMINAL_STATUSES.has(sessionStatusRef.current)) {
        shouldReconnectRef.current = false
        return
      }

      if (reconnectCount.current < WS_MAX_RECONNECT && shouldReconnectRef.current) {
        reconnectTimer.current = setTimeout(() => {
          reconnectCount.current += 1
          connect()
        }, WS_RECONNECT_DELAY)
      }
    }

    ws.onerror = (event) => {
      connectingRef.current = false
      console.warn('[ws] Error:', event)
      ws.close()
    }
  }, [sessionId, handleWsEvent, setWsConnected, setWsError, cleanup])

  // Separate effect: when session reaches terminal state, stop reconnecting
  // without triggering the connect() effect (avoids reconnect churn).
  useEffect(() => {
    if (TERMINAL_STATUSES.has(sessionStatus)) {
      shouldReconnectRef.current = false
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
        reconnectTimer.current = null
      }
    }
  }, [sessionStatus])

  useEffect(() => {
    if (sessionId) {
      // Reset reconnect state on new session
      shouldReconnectRef.current = true
      reconnectCount.current = 0
      connect()
    }
    return cleanup
  }, [sessionId, connect, cleanup])
}
