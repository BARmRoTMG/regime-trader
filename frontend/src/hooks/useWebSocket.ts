import { useEffect, useRef, useCallback } from 'react'

export type WsMessage = { type: 'signal' | 'equity' | 'ping' } & Record<string, unknown>

type Handler = (msg: WsMessage) => void

export function useWebSocket(onMessage: Handler) {
  const wsRef = useRef<WebSocket | null>(null)
  const handlerRef = useRef<Handler>(onMessage)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  handlerRef.current = onMessage

  const connect = useCallback(() => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/ws`)

    ws.onmessage = (e: MessageEvent<string>) => {
      try {
        const msg = JSON.parse(e.data) as WsMessage
        if (msg.type !== 'ping') handlerRef.current(msg)
      } catch {
        // ignore malformed frames
      }
    }

    ws.onclose = () => {
      retryRef.current = setTimeout(connect, 3000)
    }

    wsRef.current = ws
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (retryRef.current) clearTimeout(retryRef.current)
      wsRef.current?.close()
    }
  }, [connect])
}
