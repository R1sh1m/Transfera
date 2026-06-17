// ---------------------------------------------------------------------------
// MediaVault v2 — WebSocket Hook
// Manages connection to /ws/transfer/{sessionId} with auto-reconnect.
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

  const cleanup = useCallback(() => {
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = null
    }
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setWsConnected(false)
  }, [setWsConnected])

  const connect = useCallback(() => {
    if (!sessionId) return
    cleanup()

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host || '127.0.0.1:47821'
    const url = `${protocol}//${host}/ws/transfer/${sessionId}`

    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      setWsConnected(true)
      reconnectCount.current = 0
    }

    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data as string) as {
          event: string
          data: Record<string, unknown>
          timestamp: string
        }
        handleWsEvent(event as WSEvent)
      } catch {
        // malformed message — ignore
      }
    }

    ws.onclose = () => {
      setWsConnected(false)
      if (reconnectCount.current < WS_MAX_RECONNECT) {
        reconnectTimer.current = setTimeout(() => {
          reconnectCount.current += 1
          connect()
        }, WS_RECONNECT_DELAY)
      }
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [sessionId, handleWsEvent, setWsConnected, cleanup])

  useEffect(() => {
    if (sessionId) {
      connect()
    }
    return cleanup
  }, [sessionId, connect, cleanup])
}
