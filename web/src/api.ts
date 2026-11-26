// Calls to Kite's web back end. Every write carries the session's CSRF token; a 401 sends the page back
// to the login form.

import type { DictKey } from './i18n'

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
  label?: string | null
  type: 'kaggle' | 'openai' | 'anthropic' | 'runpod' | string
  brand: string
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

export type TaskRow = {
  task_id: string
  state: string
  task_type: string
  goal: string
  created: string
  chat_session_id?: string | null
  environment_id?: string | null
  main_repo?: string | null
  last_apply_error?: ApplyError | null
}

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

// ---------------------------------------------------------------- Gói 2: environments + chat sessions (contract in
// kite-ui-build/HOP-DONG-GIAO-TIEP.md, section Gói 2). The back end for
// these endpoints is built by worker (a) in parallel; every field the UI
// shows is optional so the page still renders when the server returns a
// subset, and list calls accept either `{items: [...]}` or a bare array.
export type EnvironmentKind = 'default' | 'github' | 'local' | string

export type EnvironmentRow = {
  id: string
  name?: string | null
  display_name?: string | null
  kind?: EnvironmentKind
  owner?: string | null
  repo?: string | null
  url?: string | null
  path?: string | null
  branch?: string | null
  head?: string | null
  clean?: boolean | null
  last_sync?: number | string | null
  last_synced_at?: number | string | null
  sessions?: number | null
  session_count?: number | null
  tasks?: number | null
  task_count?: number | null
  lessons?: number | null
  lesson_count?: number | null
  spend_month_usd?: number | null
  description?: string | null
  has_pat?: boolean | null
  recent_commits?: string | string[] | null
}

export type EnvironmentList = { environments: EnvironmentRow[] }

export function envDisplayName(e: EnvironmentRow): string {
  return e.display_name ?? e.name ?? e.repo ?? e.id
}

export function envRepoLabel(e: EnvironmentRow): string | null {
  if (e.owner && e.repo) return `${e.owner}/${e.repo}`
  if (e.repo) return e.repo
  if (e.url) return e.url.replace(/^https?:\/\/[^/]+\//, '').replace(/\.git$/, '')
  return null
}

export function envSessionCount(e: EnvironmentRow): number | null {
  return e.sessions ?? e.session_count ?? null
}

export function envTaskCount(e: EnvironmentRow): number | null {
  return e.tasks ?? e.task_count ?? null
}

export function envLessonCount(e: EnvironmentRow): number | null {
  return e.lessons ?? e.lesson_count ?? null
}

export function envLastSync(e: EnvironmentRow): number | string | null {
  return e.last_sync ?? e.last_synced_at ?? null
}

export async function listEnvironments(): Promise<EnvironmentRow[]> {
  const data = await get<EnvironmentList | EnvironmentRow[]>('/api/environments')
  if (Array.isArray(data)) return data
  return data.environments ?? []
}

export async function createEnvironment(body: {
  name: string
  path: string
  url?: string
}): Promise<EnvironmentRow> {
  // Matches the real back end (worker (a)): EnvIn is {name, path, url}.
  // There is no branch/PAT field at creation; PAT is set afterwards on the
  // environment detail page (POST /api/environments/{id}/pat).
  return post<EnvironmentRow>('/api/environments', body)
}

export async function syncEnvironment(id: string): Promise<unknown> {
  return post(`/api/environments/${encodeURIComponent(id)}/sync`)
}

export async function deleteEnvironment(id: string): Promise<unknown> {
  return del(`/api/environments/${encodeURIComponent(id)}`)
}

export async function saveEnvironmentPat(id: string, pat: string): Promise<unknown> {
  return post(`/api/environments/${encodeURIComponent(id)}/pat`, { pat })
}

export async function deleteEnvironmentPat(id: string): Promise<unknown> {
  return del(`/api/environments/${encodeURIComponent(id)}/pat`)
}

// ---------------------------------------------------------------- F4: environment context (Điểm 9). The back end
// (GET /api/environments/{id}/context, PATCH description, POST codemap
// rebuild) returns exactly what the agent is loaded with: the owner-written
// description, the codemap built by graphify, the repo's AGENTS.md, and a
// preview assembled from existing data (no model call).
export type EnvCodemap = {
  status: 'none' | 'not_built' | 'building' | 'ready' | 'failed' | 'unavailable'
  graphify_available: boolean
  built_at?: string | number | null
  error?: string | null
  files: number
  symbols: { total: number; modules: number; classes: number; functions: number; methods: number }
  top_files: { path: string; symbols: number }[]
}

export type EnvAgentsMd = { present: boolean; chars: number; truncated: boolean; content: string | null }

export type EnvContextData = {
  env_id: string
  description?: string | null
  codemap: EnvCodemap
  agents_md: EnvAgentsMd
  preview: string
}

export async function getEnvContext(id: string): Promise<EnvContextData> {
  return get<EnvContextData>(`/api/environments/${encodeURIComponent(id)}/context`)
}

export async function saveEnvDescription(id: string, description: string): Promise<EnvironmentRow> {
  return patch<EnvironmentRow>(`/api/environments/${encodeURIComponent(id)}`, { description })
}

export async function rebuildCodemap(id: string): Promise<unknown> {
  return post(`/api/environments/${encodeURIComponent(id)}/codemap/rebuild`)
}

export type KiteChatSession = {
  kite_session_id: string
  opencode_session_id?: string | null
  title?: string | null
  environment_id?: string | null
  kind?: string | null
  status?: string | null
  created_at?: number | string | null
  updated_at?: number | string | null
}

export type ChatSessionList = { sessions: KiteChatSession[] }

export async function listChatSessions(): Promise<KiteChatSession[]> {
  const data = await get<ChatSessionList | KiteChatSession[]>('/api/chat/sessions')
  if (Array.isArray(data)) return data
  return data.sessions ?? []
}

export async function createChatSession(body: { environment_id?: string; title?: string; kind?: string } = {}): Promise<KiteChatSession> {
  return post<KiteChatSession>('/api/chat/sessions', body)
}

export async function moveChatSession(id: string, toEnv: string): Promise<unknown> {
  return post(`/api/chat/sessions/${encodeURIComponent(id)}/move`, { to_env: toEnv })
}

export async function archiveChatSession(id: string): Promise<unknown> {
  return post(`/api/chat/sessions/${encodeURIComponent(id)}/archive`)
}

export async function deleteChatSession(id: string): Promise<unknown> {
  return del(`/api/chat/sessions/${encodeURIComponent(id)}`)
}

export type SessionMove = {
  session_id?: string
  from_env?: string | null
  to_env?: string | null
  moved_at?: number | string | null
  trigger?: string | null
}

export async function listSessionMoves(id: string): Promise<SessionMove[]> {
  const data = await get<{ moves: SessionMove[] } | SessionMove[]>(`/api/chat/sessions/${encodeURIComponent(id)}/moves`)
  if (Array.isArray(data)) return data
  return data.moves ?? []
}

// Gói 10 experience API, consumed by the environment detail page (Gói 2 owns the UI).
export type ExperienceDoc = {
  repo?: string
  version?: number | null
  updated_at?: string | null
  content?: string | null
  has_doc?: boolean | null
  pending?: { id?: string; version?: number | null; created_at?: string | null } | null
}

export async function getExperience(repo: string): Promise<ExperienceDoc> {
  return get<ExperienceDoc>(`/api/repos/experience${qs({ repo })}`)
}

export async function getExperienceDiff(id: string): Promise<string> {
  const res = await fetch(`/api/repos/experience/${encodeURIComponent(id)}/diff`, { credentials: 'same-origin' })
  return res.ok ? await res.text() : ''
}

export type RepoMemoryPending = { id: string; kind: string; repo?: string; task_id?: string; text: string }

export type RepoMemory = {
  chat?: string[]
  repo?: string | null
  lessons?: string[]
  lessons_file?: string | null
  pending?: RepoMemoryPending[]
}

export async function getRepoMemory(repo: string): Promise<RepoMemory> {
  return get<RepoMemory>(`/api/repos/memory${qs({ repo })}`)
}

export async function approveExperience(id: string): Promise<unknown> {
  return post(`/api/repos/experience/${encodeURIComponent(id)}/approve`)
}

export async function rejectExperience(id: string): Promise<unknown> {
  return post(`/api/repos/experience/${encodeURIComponent(id)}/reject`)
}

// Existing repo-status endpoint, reused for git info on the environment
// detail page (GET /api/environments itself carries no branch/HEAD).
export type RepoStatus = {
  repo?: string
  branch?: string | null
  head?: string | null
  changes?: string | null
  diff_stat?: string | null
  recent_commits?: string | null
}

export async function getRepoStatus(path: string): Promise<RepoStatus> {
  return get<RepoStatus>(`/api/repos/status${qs({ repo: path })}`)
}

// Gói 2/Gói 3: re-run a task whose patch failed to apply (only for
// `apply_conflict`; see HOP-DONG-GIAO-TIEP.md Gói 2 + Gói 3).
export type ApplyError = {
  code?: string
  head?: string | null
  failed_files?: string[] | null
  at?: number | string | null
  task_id?: string | null
}

export async function rerunTask(taskId: string): Promise<{ task_id: string }> {
  return post<{ task_id: string }>(`/api/tasks/${encodeURIComponent(taskId)}/rerun`)
}

export async function dismissApplyError(taskId: string): Promise<unknown> {
  return post(`/api/tasks/${encodeURIComponent(taskId)}/apply-error/dismiss`)
}

// ---------------------------------------------------------------- Không gian làm việc (Gói 4)
export type NeedsDecisionItem = {
  kind: 'patch_awaiting' | 'lesson_pending' | 'quota_near' | 'task_failed' | string
  title: string
  detail: string
  task_id: string | null
  link: string
  created_at: number
  key: string
}

export type ScheduleRow = {
  id: string
  plan_path: string
  kind: 'once' | 'daily' | 'weekly' | string
  run_at: string
  weekdays?: number[] | null
  next_run?: number | null
  last_run?: number | null
  last_run_task_ids?: string[]
  status: 'enabled' | 'disabled' | 'expired' | string
  created_at?: number
}

export type SecretaryReport = { date: string; title: string }

export const getWorkspaceQueue = () => get<{ items: NeedsDecisionItem[] }>('/api/workspace/queue')
export const getNeedsDecision = () => get<{ items: NeedsDecisionItem[] }>('/api/workspace/needs-decision')

export const getSchedules = () => get<{ schedules: ScheduleRow[] }>('/api/schedules')
export const createSchedule = (body: Record<string, unknown>) => post<ScheduleRow>('/api/schedules', body)
export const updateSchedule = (id: string, body: Record<string, unknown>) =>
  patch<ScheduleRow>(`/api/schedules/${encodeURIComponent(id)}`, body)
export const deleteSchedule = (id: string) => del(`/api/schedules/${encodeURIComponent(id)}`)

export const getSecretaryReports = () => get<{ reports: SecretaryReport[] }>('/api/secretary/reports')
export const getSecretaryReport = (date: string) =>
  get<{ date: string; markdown: string }>(`/api/secretary/reports/${encodeURIComponent(date)}`)

// ---------------------------------------------------------------- Connect (Gói 5): MCP + skill + API platforms.
// Endpoints follow the cross-package contract; the back end (worker (b)) owns
// the server side. Statuses: enabled / disabled / pending_scan / pending_approval.
export type ConnectStatus = 'enabled' | 'disabled' | 'pending_scan' | 'pending_approval'
export type ConnectMcpKind = 'builtin' | 'local' | 'source' | 'package' | 'remote'
export type ScanVerdict = 'pass' | 'fail' | 'wait'

export type ScanFinding = { rule: string; severity: 'info' | 'low' | 'medium' | 'high' | 'critical'; message: string }

export type ScanReport = {
  verdict: ScanVerdict
  findings: ScanFinding[]
  scanner: string
  scanner_version: string
  scanned_at: string | number | null
  wait_reason?: string | null
}

export type ConnectLock = { version: string; integrity_sha512?: string | null; published_at?: string | number | null } | null

export type ConnectMcpItem = {
  id: string
  name: string
  kind: ConnectMcpKind
  status: ConnectStatus
  enabled: boolean
  source?: string | null
  command?: string | null
  args?: string[] | null
  url?: string | null
  package_name?: string | null
  version?: string | null
  tools?: string[] | null
  lock?: ConnectLock
  lock_mismatch?: boolean
  last_scan?: ScanReport | null
}

export type ConnectSkillItem = {
  id: string
  name: string
  path?: string | null
  status: ConnectStatus
  enabled: boolean
  lock?: ConnectLock
  lock_mismatch?: boolean
  last_scan?: ScanReport | null
}

export type ConnectPlatformItem = {
  id: string
  name: string
  kind?: string | null
  status: 'configured' | 'not_configured'
  detail?: string | null
}

export type ConnectInventory = { mcp: ConnectMcpItem[]; skills: ConnectSkillItem[]; platforms: ConnectPlatformItem[] }

export type McpCreateBody = {
  name: string
  kind: 'source' | 'package' | 'remote'
  command?: string
  args?: string[]
  package_name?: string
  version?: string
  url?: string
  auth_header?: string
}

/** Map the flat add-MCP form shape to the back end's nested-config payload
 * (F2 fix: `McpCreateIn` only reads `config` — flat fields are silently
 * ignored by pydantic, so an unmapped create always failed 400). The command
 * box holds a full command line (placeholder: "python /opt/mcp/server.py"),
 * so it is split on whitespace first; the registry is left out — the back
 * end defaults a missing one to the official registry. */
export function mcpCreatePayload(body: McpCreateBody): {
  name: string
  kind: 'source' | 'package' | 'remote'
  config: Record<string, unknown>
  auth_secret?: string
} {
  const config: Record<string, unknown> = {}
  if (body.kind === 'source') {
    config.command = [...(body.command ?? '').split(/\s+/).filter(Boolean), ...(body.args ?? [])]
  } else if (body.kind === 'package') {
    config.package = body.package_name
    config.version = body.version
    config.ecosystem = 'npm'
  } else {
    config.url = body.url
  }
  return {
    name: body.name,
    kind: body.kind,
    config,
    ...(body.auth_header ? { auth_secret: body.auth_header } : {}),
  }
}

export type SkillCreateBody = { name: string; path?: string }

export type McpTestResult = { ok: boolean; detail: string; tools?: string[]; latency_ms?: number | null }

export const getConnectInventory = () => get<ConnectInventory>('/api/connect/inventory')
export const getConnectPlatforms = () => get<{ platforms: ConnectPlatformItem[] }>('/api/connect/platforms')
export const createConnectMcp = (body: McpCreateBody) => post<ConnectMcpItem>('/api/connect/mcp', mcpCreatePayload(body))
export const scanConnectMcp = (id: string) => post<ScanReport>(`/api/connect/mcp/${encodeURIComponent(id)}/scan`)
export const getConnectMcpScan = (id: string) => get<ScanReport>(`/api/connect/mcp/${encodeURIComponent(id)}/scan`)
export const testConnectMcp = (id: string) => post<McpTestResult>(`/api/connect/mcp/${encodeURIComponent(id)}/test`)
export const enableConnectMcp = (id: string) => post<ConnectMcpItem>(`/api/connect/mcp/${encodeURIComponent(id)}/enable`)
export const disableConnectMcp = (id: string) => post<ConnectMcpItem>(`/api/connect/mcp/${encodeURIComponent(id)}/disable`)
export const deleteConnectMcp = (id: string) => del(`/api/connect/mcp/${encodeURIComponent(id)}`)
export const createConnectSkill = (body: SkillCreateBody) => post<ConnectSkillItem>('/api/connect/skills', body)
export const scanConnectSkill = (id: string) => post<ScanReport>(`/api/connect/skills/${encodeURIComponent(id)}/scan`)
export const getConnectSkillScan = (id: string) => get<ScanReport>(`/api/connect/skills/${encodeURIComponent(id)}/scan`)
export const enableConnectSkill = (id: string) => post<ConnectSkillItem>(`/api/connect/skills/${encodeURIComponent(id)}/enable`)
export const disableConnectSkill = (id: string) => post<ConnectSkillItem>(`/api/connect/skills/${encodeURIComponent(id)}/disable`)
export const deleteConnectSkill = (id: string) => del(`/api/connect/skills/${encodeURIComponent(id)}`)

export type KnowledgeDoc = {
  doc_id: string
  filename: string
  scope: 'global' | 'environment'
  environment_id: string | null
  tags: string[]
  status: 'uploaded' | 'converting' | 'ready' | 'failed'
  converter: 'markitdown' | 'docling' | 'passthrough' | null
  converter_version: string | null
  converted_at: string | null
  error: string | null
  size_bytes: number
}

export function listKnowledge(params: { scope?: string; environment_id?: string; tag?: string } = {}) {
  return get<{ documents: KnowledgeDoc[] }>(`/api/knowledge${qs(params)}`)
}

export function getKnowledge(docId: string) {
  return get<KnowledgeDoc>(`/api/knowledge/${encodeURIComponent(docId)}`)
}

export function getKnowledgeMarkdown(docId: string) {
  return get<string>(`/api/knowledge/${encodeURIComponent(docId)}/markdown`)
}

export async function uploadKnowledge(form: FormData): Promise<KnowledgeDoc> {
  const res = await fetch('/api/knowledge', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrf },
    body: form,
    credentials: 'same-origin',
  })
  if (res.status === 401) onUnauthorized()
  const type = res.headers.get('content-type') ?? ''
  const data: unknown = res.status === 204 ? null : type.includes('json') ? await res.json().catch(() => null) : await res.text()
  if (!res.ok) throw new ApiError(res.status, errorText(data, res.status), data)
  return data as KnowledgeDoc
}

export function deleteKnowledge(docId: string) {
  return del(`/api/knowledge/${encodeURIComponent(docId)}`)
}

export function reconvertKnowledge(docId: string) {
  return post<{ doc_id: string; status: string }>(`/api/knowledge/${encodeURIComponent(docId)}/reconvert`)
}

/** Bench — mở rộng theo schema §9 của docs/bench-standard-v1.md. */
export type BenchLevel = {
  level: number
  name_en: string
  name_vi: string
  name_i18n_key: DictKey
  desc_i18n_key: DictKey
  range: [number, number]
}

export type BenchNetwork = { mode: 'offline' | 'allowlist'; hosts: string[] }

export type BenchRadarPoint = { axis: string; key: string; score: number | null }

export type BenchRunRow = Record<string, unknown> & {
  criteria_version?: string
  gate_set?: string | null
  overall?: number | null
  level?: BenchLevel | null
  level_unstable?: boolean | null
  network?: BenchNetwork
  axes?: Record<string, number | null>
  radar?: BenchRadarPoint[]
}

export type BenchOfficial = {
  criteria_version: string
  gate_set: string | null
  overall: number | null
  official: boolean
  runs: number
  excluded_runs: number
  level: BenchLevel | null
  level_unstable: boolean
}

export type BenchHistory = {
  runs: BenchRunRow[]
  table: string
  official?: BenchOfficial
  declared_network_hosts?: string[]
}

/** Bench import (F5): the result of POST /api/bench/import — the same
 * shape the CLI prints (ImportResult), so UI and CLI never disagree. */
export type BenchImportResult = {
  ok: boolean
  task_id: string | null
  dest: string | null
  source: string
  skipped: boolean
  reason: string | null
  warnings: string[]
}

export type BenchTaskRow = {
  id: string
  kind: string | null
  language: string | null
  axes: string[]
  difficulty: string | number | null
  source: string
  ok: boolean
  errors: { field: string; code: string; message: string }[]
}

export type BenchTasks = { tasks: BenchTaskRow[] }

export function importBenchTask(body: Record<string, unknown>) {
  return post<BenchImportResult>('/api/bench/import', body)
}

export function listBenchTasks() {
  return get<BenchTasks>('/api/bench/tasks')
}
