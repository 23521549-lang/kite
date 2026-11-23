// Calls to Kite's web back end. Every write carries the session's CSRF token; a 401 sends the page back
// to the login form.

export class ApiError extends Error {
  status: number
  data: unknown
  constructor(status: number, message: string, data?: unknown) {
    super(message)
    this.status = status
    this.data = data
  }
}

let csrf = ''
let onUnauthorized: () => void = () => {}

export function setCsrf(token: string) {
  csrf = token
}

export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

type Options = { method?: string; body?: unknown; signal?: AbortSignal }

function errorText(data: unknown, status: number): string {
  if (data && typeof data === 'object') {
    const d = data as Record<string, unknown>
    const msg = d.error ?? d.message ?? d.detail
    if (typeof msg === 'string') return msg
    if (msg) return JSON.stringify(msg)
  }
  if (typeof data === 'string' && data.trim()) return data.slice(0, 300)
  return `HTTP ${status}`
}

export async function api<T = any>(path: string, opts: Options = {}): Promise<T> {
  const method = opts.method ?? (opts.body === undefined ? 'GET' : 'POST')
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'
  const res = await fetch(path, {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    credentials: 'same-origin',
    signal: opts.signal,
  })
  if (res.status === 401 && path !== '/api/auth/login') onUnauthorized()
  const type = res.headers.get('content-type') ?? ''
  let data: unknown = null
  if (res.status !== 204) {
    data = type.includes('json') ? await res.json().catch(() => null) : await res.text()
  }
  if (!res.ok) throw new ApiError(res.status, errorText(data, res.status), data)
  return data as T
}

export const get = <T = any>(path: string, signal?: AbortSignal) => api<T>(path, { signal })
export const post = <T = any>(path: string, body: unknown = {}) => api<T>(path, { method: 'POST', body })
export const put = <T = any>(path: string, body: unknown) => api<T>(path, { method: 'PUT', body })
export const patch = <T = any>(path: string, body: unknown) => api<T>(path, { method: 'PATCH', body })
export const del = <T = any>(path: string) => api<T>(path, { method: 'DELETE' })

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

export function wsUrl(path: string): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${location.host}${path}`
}

// ---------------------------------------------------------------- shapes the back end returns
export type Me = { username: string; csrf: string; sessions: SessionRow[] }
export type SessionRow = { id?: number; ip?: string; user_agent?: string; created_at?: number; last_seen?: number; current?: boolean }

export type WorkerRow = {
  id: string
  type: 'kaggle' | 'openai' | 'anthropic' | 'runpod' | string
  enabled: boolean
  owner: string
  models: string[]
  concurrency: number
  alive: boolean
  busy: boolean
  running: number
  slots?: number | null
  loaded_model?: string | null
  version?: string | null
  draining: boolean
  seconds_since_heartbeat?: number | null
  switch: { on: boolean; auto: boolean }
  start?: Record<string, any> | null
  spend: { today_usd: number; month_usd: number }
  in_gateway: boolean
  kaggle?: { kernel: string; via: string; notebook: string; idle_minutes: number; auto_click: boolean; url: string }
  api?: {
    kind: string
    base_url?: string | null
    stream: boolean
    price: { input: number; output: number }
    caps: { daily_usd?: number | null; monthly_usd?: number | null }
    upstream: Record<string, string>
  }
  pod?: { provider: string; id: string; port: number; price_hour?: number | null; idle_minutes: number }
}

export type TaskRow = { task_id: string; state: string; task_type: string; goal: string; created: string }

export type Activity = {
  task_id: string
  state: string
  goal: string
  step: string | null
  action: string | null
  phase: string | null
  since: number | null
  worker: string | null
}

export type Overview = {
  gateway_up: boolean
  workers: WorkerRow[]
  queued_jobs: number
  running_jobs: number
  spend: { today_usd: number; month_usd: number }
  tasks: { counts: Record<string, number>; recent: TaskRow[] }
  gpu_quota: Record<string, { status: string; detail: string }> | null
}

export type ProviderType = {
  type: string
  label: string
  cost: string
  fields: string[]
  secrets: string[]
  note: string
}
