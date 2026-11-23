// Shared state: who is signed in, and what Kite is doing now (the /api/ws events stream).
import { create } from 'zustand'
import { get, setCsrf, wsUrl, type Activity, type Me } from './api'

export type LiveEvent = { file: string; record: Record<string, any>; at: number }

type State = {
  me: Me | null | undefined // undefined: not known yet
  setMe: (me: Me | null) => void
  connected: boolean
  gatewayUp: boolean | null
  status: Record<string, any> | null
  activity: Activity[]
  events: LiveEvent[]
}

export const useKite = create<State>((set) => ({
  me: undefined,
  setMe: (me) => {
    setCsrf(me?.csrf ?? '')
    set({ me })
  },
  connected: false,
  gatewayUp: null,
  status: null,
  activity: [],
  events: [],
}))

export async function loadMe(): Promise<void> {
  try {
    const me = await get<Me>('/api/auth/me')
    useKite.getState().setMe(me)
  } catch {
    useKite.getState().setMe(null)
  }
}

let socket: WebSocket | null = null
let retry = 0
let stopped = true

export function startLive() {
  if (!stopped) return
  stopped = false
  open()
}

export function stopLive() {
  stopped = true
  socket?.close()
  socket = null
  useKite.setState({ connected: false })
}

function open() {
  if (stopped) return
  const ws = new WebSocket(wsUrl('/api/ws'))
  socket = ws
  ws.onopen = () => {
    retry = 0
    useKite.setState({ connected: true })
  }
  ws.onmessage = (e) => {
    let msg: any
    try {
      msg = JSON.parse(e.data)
    } catch {
      return
    }
    if (msg.t === 'status') useKite.setState({ gatewayUp: !!msg.gateway_up, status: msg.status ?? null })
    else if (msg.t === 'tasks') useKite.setState({ activity: msg.tasks ?? [] })
    else if (msg.t === 'event') {
      const item: LiveEvent = { file: msg.file, record: msg.record ?? {}, at: Date.now() }
      useKite.setState((s) => ({ events: [item, ...s.events].slice(0, 300) }))
    }
  }
  ws.onclose = (e) => {
    useKite.setState({ connected: false })
    if (socket === ws) socket = null
    if (stopped) return
    if (e.code === 4401) {
      loadMe()
      return
    }
    retry = Math.min(retry + 1, 6)
    setTimeout(open, 1000 * 2 ** (retry - 1))
  }
}
