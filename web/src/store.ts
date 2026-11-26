// Shared state: who is signed in, and what Kite is doing now (the /api/ws events stream).
import { create } from 'zustand'
import { ApiError, get, setCsrf, wsUrl, type Activity, type Me } from './api'

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

let loadMeRun = 0

export async function loadMe(): Promise<void> {
  const run = ++loadMeRun
  const me = await loadMeWithRetry(
    () => get<Me>('/api/auth/me'),
    (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  )
  // A newer loadMe started while this one was retrying (e.g. the owner just
  // logged in): its result is fresher, so this stale one must not overwrite it.
  if (run !== loadMeRun) return
  useKite.getState().setMe(me)
}

// How a transient failure is retried before the owner is sent back to the
// login form: 3 retries, waiting 1s, then 2s, then 4s. A dropped tunnel or a
// hiccuping server used to show the login form at once; now it shows the
// loader a few seconds longer instead.
export const LOAD_ME_RETRIES = 3
export const LOAD_ME_BACKOFF_MS = [1000, 2000, 4000]

function isRetryableLoadMeError(e: unknown): boolean {
  // The fetch itself threw (TypeError on a dropped connection, tunnel down,
  // DNS failure, ...): the server never answered, so this says nothing about
  // the session. A 5xx is the server failing to answer. Anything else — 401
  // included — is an answer about the session, not the network.
  if (e instanceof ApiError) return e.status >= 500
  return true
}

/** Pure retry loop behind loadMe: fetchMe until it succeeds, return null on a
 * non-retryable error, or after `retries` retries on a retryable one. */
export async function loadMeWithRetry(
  fetchMe: () => Promise<Me>,
  sleep: (ms: number) => Promise<void>,
  retries: number = LOAD_ME_RETRIES,
): Promise<Me | null> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await fetchMe()
    } catch (e) {
      if (!isRetryableLoadMeError(e) || attempt >= retries) return null
      await sleep(LOAD_ME_BACKOFF_MS[attempt] ?? 4000)
    }
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
