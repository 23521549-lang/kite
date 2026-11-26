// Chat with Kite's lead agent through the OpenCode 2 server the TUI also uses (sessions are shared).
// History comes from /api/session/{id}/message; while the agent works, its text and tool calls stream in
// from the server's event stream and the history is read again when a step ends.
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Card,
  Checkbox,
  Code,
  Collapse,
  Group,
  Loader,
  Menu,
  Paper,
  Radio,
  ScrollArea,
  Select,
  Stack,
  Text,
  TextInput,
  Textarea,
  Tooltip,
  UnstyledButton,
} from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import {
  IconAlertTriangle,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconDots,
  IconPlayerStopFilled,
  IconPlus,
  IconSend,
  IconTool,
  IconTrash,
  IconPencil,
} from '@tabler/icons-react'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { del, get, patch, post, qs } from '../api'
import { ConfirmButton, JsonView, Loading, Markdown } from '../components/common'
import { go } from '../router'
import { useT, type DictKey, type TFunction } from '../i18n'
import { ago, attempt, notifyError, short, useLoad } from '../util'

// ---------------------------------------------------------------- OpenCode 2 shapes (the parts used here)
type ChatInfo = {
  chat_dir: string | null
  ready: boolean
  tested_version: string
  opencode: { reachable: boolean; version: string | null; error: string | null }
}
type Session = {
  id: string
  parentID?: string
  title?: string
  agent?: string
  tokens?: { input: number; output: number; reasoning?: number }
  time: { created: number; updated: number; busy?: number; idle?: number; archived?: number }
  outcome?: string
}
type ToolState = {
  status: 'streaming' | 'running' | 'completed' | 'error'
  input?: any
  content?: { type: string; text?: string; [k: string]: any }[]
  error?: { type?: string; message: string }
  metadata?: Record<string, any>
}
type Part =
  | { type: 'text'; text: string }
  | { type: 'reasoning'; text: string }
  | { type: 'tool'; id: string; name: string; state: ToolState }
type Message = {
  id: string
  type: string
  time: { created: number; completed?: number }
  text?: string
  description?: string
  content?: Part[]
  error?: { type?: string; message: string }
  outcome?: string
  command?: string
  status?: string
  output?: { output: string; truncated: boolean }
  agent?: string
  model?: { id: string; providerID: string }
}
type Permission = { id: string; sessionID: string; action: string; resources: string[]; message?: string }
type FormField = {
  key: string
  title?: string
  description?: string
  type?: string
  options?: { value: string; label?: string; description?: string }[]
  custom?: boolean
  multiple?: boolean
}
type Form = { id: string; sessionID: string; title: string; fields: FormField[] }
type Live = { id: string; order: string[]; parts: Record<string, Part> }

const OC = '/api/opencode/api'

const KITE_TOOLS: Record<string, DictKey> = {
  start_task: 'chat.tool.start-task',
  task_status: 'chat.tool.task-status',
  wait_task: 'chat.tool.wait-task',
  task_log: 'chat.tool.task-log',
  task_result: 'chat.tool.task-result',
  apply_result: 'chat.tool.apply-result',
  commit_result: 'chat.tool.commit-result',
  repo_status: 'chat.tool.repo-status',
  run_command: 'chat.tool.run-command',
  send_instruction: 'chat.tool.send-instruction',
  pause_task: 'chat.tool.pause-task',
  resume_task: 'chat.tool.resume-task',
  cancel_task: 'chat.tool.cancel-task',
  list_tasks: 'chat.tool.list-tasks',
  worker_status: 'chat.tool.worker-status',
  note_task: 'chat.tool.note-task',
  remember: 'chat.tool.remember',
  memory_show: 'chat.tool.memory-show',
  memory_forget: 'chat.tool.memory-forget',
  repo_config: 'chat.tool.repo-config',
  doctor: 'chat.tool.doctor',
  cleanup: 'chat.tool.cleanup',
  worker_restart: 'chat.tool.worker-restart',
  update_kite: 'chat.tool.update-kite',
  bench_start: 'chat.tool.bench-start',
  bench_history: 'chat.tool.bench-history',
}
const OTHER_TOOLS: Record<string, DictKey> = { shell: 'chat.tool.shell', question: 'chat.tool.question', read: 'chat.tool.read', glob: 'chat.tool.glob', grep: 'chat.tool.grep' }

function toolLabel(name: string, t: TFunction): string {
  const kite = name.match(/^kite[_.:-](.+)$/)
  if (kite) {
    const key = KITE_TOOLS[kite[1]]
    return key ? t(key) : kite[1]
  }
  const other = OTHER_TOOLS[name]
  return other ? t(other) : name
}

function toolOutput(state: ToolState): string {
  return (state.content ?? [])
    .map((c) => (c.type === 'text' ? c.text ?? '' : JSON.stringify(c)))
    .join('\n')
    .trim()
}

function parseJson(text: string): any {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

// ---------------------------------------------------------------- the page
export default function Chat({ parts }: { parts: string[] }) {
  const t = useT()
  const sessionId = parts[0] ?? null
  const wide = useMediaQuery('(min-width: 62em)')
  const { data: info, error: infoError, reload: reloadInfo } = useLoad(() => get<ChatInfo>('/api/chat/info'), [], 30000)
  const [sessions, setSessions] = useState<Session[] | null>(null)

  const loadSessions = useCallback(async () => {
    if (!info?.chat_dir) return
    try {
      const res = await get<{ data: Session[] }>(`${OC}/session${qs({ directory: info.chat_dir, limit: 60 })}`)
      setSessions(res.data.filter((s) => !s.parentID && !s.time.archived).sort((a, b) => b.time.updated - a.time.updated))
    } catch (e) {
      notifyError(e, t('chat.sessions-load-error'))
      setSessions([])
    }
  }, [info?.chat_dir, t])

  useEffect(() => {
    if (info?.opencode.reachable && info.ready) loadSessions()
  }, [info?.opencode.reachable, info?.ready, loadSessions])

  async function newSession() {
    const res = await attempt(() => post<{ data: Session }>(`${OC}/session`, {}))
    if (res) {
      await loadSessions()
      go(`/chat/${res.data.id}`)
    }
  }

  if (!info) return <Box p="md">{<Loading error={infoError} />}</Box>
  const problem = chatProblem(info, t)
  if (problem)
    return (
      <Box p="md">
        <Alert color="yellow" title={t('chat.not-ready')} icon={<IconAlertTriangle />}>
          {problem}
          <Group mt="sm">
            <Button size="xs" variant="light" onClick={reloadInfo}>
              {t('chat.check-again')}
            </Button>
          </Group>
        </Alert>
      </Box>
    )

  const versionNote =
    info.opencode.version && info.opencode.version.split('.')[0] !== info.tested_version.split('.')[0]
      ? t('chat.version-mismatch', { version: info.opencode.version, tested: info.tested_version })
      : null

  const list = (
    <SessionList
      sessions={sessions}
      current={sessionId}
      onNew={newSession}
      onChanged={loadSessions}
    />
  )
  return (
    <Box style={{ display: 'flex', height: 'calc(100dvh - 52px)' }}>
      {wide ? (
        <Box w={270} style={{ borderRight: '1px solid var(--mantine-color-default-border)', display: 'flex', flexDirection: 'column' }}>
          {list}
        </Box>
      ) : null}
      <Box style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        {!wide ? (
          <Group p="xs" gap="xs" wrap="nowrap" style={{ borderBottom: '1px solid var(--mantine-color-default-border)' }}>
            <Select
              style={{ flex: 1 }}
              placeholder={t('chat.select-session')}
              data={(sessions ?? []).map((s) => ({ value: s.id, label: s.title || t('chat.untitled-session') }))}
              value={sessionId}
              onChange={(v) => v && go(`/chat/${v}`)}
              searchable
            />
            <ActionIcon variant="light" size="lg" onClick={newSession} title={t('chat.new-session')}>
              <IconPlus size={18} />
            </ActionIcon>
          </Group>
        ) : null}
        {versionNote ? (
          <Alert color="yellow" py={6} radius={0}>
            {versionNote}
          </Alert>
        ) : null}
        {sessionId ? (
          <Conversation key={sessionId} sessionId={sessionId} chatDir={info.chat_dir!} onSessionsChanged={loadSessions} />
        ) : (
          <Stack align="center" justify="center" style={{ flex: 1 }} p="xl">
            <Text c="dimmed" ta="center">
              {t('chat.intro-1')}
              <br />
              {t('chat.intro-2')}
            </Text>
            <Button leftSection={<IconPlus size={16} />} onClick={newSession}>
              {t('chat.new-session-title')}
            </Button>
          </Stack>
        )}
      </Box>
    </Box>
  )
}

function chatProblem(info: ChatInfo, t: TFunction): ReactNode {
  if (!info.ready)
    return (
      <Text size="sm">
        {t('chat.no-chat-dir-prefix')}
        {info.chat_dir ?? '~/kite-chat'}). {t('chat.no-chat-dir-suffix')} <Code>kite chat setup</Code>.
      </Text>
    )
  if (!info.opencode.reachable)
    return (
      <Text size="sm">
        {t('chat.opencode-unreachable-prefix')}
        {info.opencode.error ?? t('chat.no-answer')}). {t('chat.opencode-unreachable-middle')} <Code>sudo systemctl enable --now kite-opencode</Code>, {t('chat.opencode-unreachable-after')} <Code>opencode</Code> {t('chat.opencode-unreachable-end')}
      </Text>
    )
  return null
}

function SessionList({
  sessions,
  current,
  onNew,
  onChanged,
}: {
  sessions: Session[] | null
  current: string | null
  onNew: () => void
  onChanged: () => void
}) {
  const t = useT()
  return (
    <>
      <Box p="xs">
        <Button fullWidth leftSection={<IconPlus size={16} />} variant="light" onClick={onNew}>
          {t('chat.new-session')}
        </Button>
      </Box>
      <ScrollArea style={{ flex: 1 }} px="xs">
        {sessions === null ? (
          <Loading />
        ) : sessions.length === 0 ? (
          <Text c="dimmed" size="sm" p="xs">
            {t('chat.no-sessions')}
          </Text>
        ) : (
          <Stack gap={2} pb="md">
            {sessions.map((s) => (
              <Group
                key={s.id}
                gap={4}
                wrap="nowrap"
                px={8}
                py={6}
                className="clickable"
                style={{ borderRadius: 8, background: s.id === current ? 'var(--mantine-color-default-hover)' : undefined }}
                onClick={() => go(`/chat/${s.id}`)}
              >
                <Box style={{ flex: 1, minWidth: 0 }}>
                  <Text size="sm" truncate fw={s.id === current ? 600 : 400}>
                    {s.title || t('chat.untitled-session')}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {ago(s.time.updated)}
                  </Text>
                </Box>
                <SessionMenu session={s} onChanged={onChanged} />
              </Group>
            ))}
          </Stack>
        )}
      </ScrollArea>
    </>
  )
}

function SessionMenu({ session, onChanged }: { session: Session; onChanged: () => void }) {
  const t = useT()
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(session.title ?? '')
  if (renaming)
    return (
      <TextInput
        size="xs"
        autoFocus
        value={title}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setTitle(e.currentTarget.value)}
        onBlur={() => setRenaming(false)}
        onKeyDown={async (e) => {
          if (e.key === 'Enter') {
            await attempt(() => patch(`${OC}/session/${session.id}`, { title }))
            setRenaming(false)
            onChanged()
          } else if (e.key === 'Escape') setRenaming(false)
        }}
      />
    )
  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon variant="subtle" size="sm" onClick={(e) => e.stopPropagation()}>
          <IconDots size={14} />
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown onClick={(e) => e.stopPropagation()}>
        <Menu.Item leftSection={<IconPencil size={14} />} onClick={() => setRenaming(true)}>
          {t('chat.rename')}
        </Menu.Item>
        <Menu.Item
          leftSection={<IconTrash size={14} />}
          color="red"
          onClick={async () => {
            if (!confirm(t('chat.delete-session-confirm'))) return
            await attempt(() => del(`${OC}/session/${session.id}`))
            onChanged()
            go('/chat')
          }}
        >
          {t('chat.delete')}
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  )
}

// ---------------------------------------------------------------- one conversation
function Conversation({ sessionId, chatDir, onSessionsChanged }: { sessionId: string; chatDir: string; onSessionsChanged: () => void }) {
  const t = useT()
  const [session, setSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[] | null>(null)
  const [live, setLive] = useState<Live | null>(null)
  const [busy, setBusy] = useState(false)
  const [permissions, setPermissions] = useState<Permission[]>([])
  const [forms, setForms] = useState<Form[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [agents, setAgents] = useState<{ id: string; name: string; description?: string }[]>([])
  const viewport = useRef<HTMLDivElement>(null)
  const nearBottom = useRef(true)

  const loadMessages = useCallback(async () => {
    try {
      const res = await get<{ data: Message[] }>(`${OC}/session/${sessionId}/message${qs({ limit: 200 })}`)
      setMessages([...res.data].reverse())
      setLive(null)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [sessionId])

  const loadPending = useCallback(async () => {
    try {
      const [p, f] = await Promise.all([
        get<{ data: Permission[] }>(`${OC}/session/${sessionId}/permission`),
        get<{ data: Form[] }>(`${OC}/session/${sessionId}/form`),
      ])
      setPermissions(p.data)
      setForms(f.data)
    } catch {
      /* the history still shows; the stream brings new requests */
    }
  }, [sessionId])

  const loadSession = useCallback(async () => {
    try {
      const res = await get<{ data: Session }>(`${OC}/session/${sessionId}`)
      setSession(res.data)
      setBusy(!!res.data.time.busy && (!res.data.time.idle || res.data.time.busy > res.data.time.idle))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [sessionId])

  useEffect(() => {
    loadSession()
    loadMessages()
    loadPending()
    get<{ data: { id: string; name: string; description?: string; mode: string; hidden: boolean }[] }>(
      `${OC}/agent?${encodeURIComponent('location[directory]')}=${encodeURIComponent(chatDir)}`,
    )
      .then((res) => setAgents(res.data.filter((a) => a.mode !== 'subagent' && !a.hidden)))
      .catch(() => setAgents([]))
  }, [loadSession, loadMessages, loadPending, chatDir])

  // The server's event stream: every session's events; keep this one's.
  useEffect(() => {
    const source = new EventSource(`${OC}/event`)
    let reconnecting = false
    source.onerror = () => {
      reconnecting = true
    }
    source.onopen = () => {
      if (reconnecting) {
        reconnecting = false
        loadSession()
        loadMessages()
        loadPending()
      }
    }
    source.onmessage = (e) => {
      let event: { type: string; data: any }
      try {
        event = JSON.parse(e.data)
      } catch {
        return
      }
      const d = event.data ?? {}
      const sid = d.sessionID ?? d.form?.sessionID
      if (['session.created', 'session.renamed', 'session.deleted', 'session.archived'].includes(event.type)) {
        onSessionsChanged()
        if (sid === sessionId && event.type === 'session.renamed') setSession((s) => (s ? { ...s, title: d.title } : s))
      }
      if (sid !== sessionId) return
      switch (event.type) {
        case 'session.execution.started':
          setBusy(true)
          setNotice(null)
          break
        case 'session.execution.succeeded':
        case 'session.execution.failed':
        case 'session.execution.interrupted':
          setBusy(false)
          setNotice(null)
          loadMessages()
          loadPending()
          break
        case 'session.step.started':
          setLive({ id: d.assistantMessageID, order: [], parts: {} })
          break
        case 'session.step.ended':
        case 'session.step.failed':
          loadMessages()
          break
        case 'session.text.delta':
        case 'session.reasoning.delta': {
          const kind = event.type === 'session.text.delta' ? 'text' : 'reasoning'
          setLive((l) => appendText(l, d.assistantMessageID, `${kind}:${d.ordinal ?? 0}`, kind, d.delta ?? ''))
          break
        }
        case 'session.tool.input.started':
          setLive((l) => putTool(l, d.assistantMessageID, d.id, d.name, { status: 'streaming' }))
          break
        case 'session.tool.called':
          setLive((l) => putTool(l, d.assistantMessageID, d.id, undefined, { status: 'running', input: d.input }))
          break
        case 'session.tool.success':
          setLive((l) => putTool(l, d.assistantMessageID, d.id, undefined, { status: 'completed', content: d.content, metadata: d.metadata }))
          break
        case 'session.tool.failed':
          setLive((l) => putTool(l, d.assistantMessageID, d.id, undefined, { status: 'error', error: d.error ?? { message: t('chat.tool-error-fallback') } }))
          break
        case 'session.retry.scheduled':
          setNotice(t('chat.retry-notice'))
          break
        case 'session.usage.updated':
          setSession((s) => (s ? { ...s, tokens: d.tokens } : s))
          break
        case 'permission.asked':
          setPermissions((ps) => (ps.some((p) => p.id === d.id) ? ps : [...ps, d as Permission]))
          break
        case 'permission.replied':
          setPermissions((ps) => ps.filter((p) => p.id !== (d.id ?? d.requestID)))
          break
        case 'form.created':
          setForms((fs) => (fs.some((f) => f.id === d.form.id) ? fs : [...fs, d.form as Form]))
          break
        case 'form.replied':
        case 'form.cancelled':
          setForms((fs) => fs.filter((f) => f.id !== d.id))
          break
      }
    }
    return () => source.close()
  }, [sessionId, loadMessages, loadPending, loadSession, onSessionsChanged])

  // Follow the end of the conversation unless the owner scrolled up to read.
  useEffect(() => {
    const el = viewport.current
    if (el && nearBottom.current) el.scrollTop = el.scrollHeight
  }, [messages, live, permissions, forms])

  async function send(text: string): Promise<boolean> {
    try {
      await post(`${OC}/session/${sessionId}/prompt`, { text })
      nearBottom.current = true
      setBusy(true)
      setMessages((m) => [...(m ?? []), { id: `local-${Date.now()}`, type: 'user', text, time: { created: Date.now() } }])
      return true
    } catch (e) {
      notifyError(e, t('chat.send-error'))
      return false
    }
  }

  async function answerPermission(p: Permission, decision: 'once' | 'reject') {
    const ok = await attempt(() => post(`${OC}/session/${sessionId}/permission/${p.id}/reply`, { decision }))
    if (ok !== undefined) setPermissions((ps) => ps.filter((x) => x.id !== p.id))
  }

  return (
    <>
      <Group px="md" py={8} justify="space-between" wrap="nowrap" style={{ borderBottom: '1px solid var(--mantine-color-default-border)' }}>
        <Box style={{ minWidth: 0 }}>
          <Text fw={600} truncate>
            {session?.title || t('chat.untitled-session')}
          </Text>
          {session?.tokens ? (
            <Text size="xs" c="dimmed">
              {t('chat.tokens', { input: session.tokens.input.toLocaleString('vi-VN'), output: session.tokens.output.toLocaleString('vi-VN') })}
            </Text>
          ) : null}
        </Box>
        <Group gap="xs" wrap="nowrap">
          {agents.length > 1 ? (
            <Select
              size="xs"
              w={190}
              data={agents.map((a) => ({ value: a.id, label: a.name || a.id }))}
              value={session?.agent ?? agents[0]?.id}
              onChange={async (v) => {
                if (!v) return
                const ok = await attempt(() => post(`${OC}/session/${sessionId}/agent`, { agent: v }))
                if (ok !== undefined) setSession((s) => (s ? { ...s, agent: v } : s))
              }}
              allowDeselect={false}
            />
          ) : null}
          {busy ? (
            <Badge color="blue" variant="light" leftSection={<Loader size={10} />}>
              {t('chat.working')}
            </Badge>
          ) : null}
        </Group>
      </Group>
      <ScrollArea
        className="chat-scroll"
        viewportRef={viewport}
        onScrollPositionChange={({ y }) => {
          const el = viewport.current
          if (el) nearBottom.current = el.scrollHeight - y - el.clientHeight < 80
        }}
      >
        <Stack gap="md" p="md" maw={900} mx="auto">
          {error ? <Alert color="red">{error}</Alert> : null}
          {messages === null ? <Loading /> : messages.length === 0 && !live ? <Text c="dimmed">{t('chat.empty-start')}</Text> : null}
          {(messages ?? []).map((m) => (
            <MessageView key={m.id} message={m} />
          ))}
          {live && !(messages ?? []).some((m) => m.id === live.id) ? (
            <AssistantParts parts={live.order.map((k) => live.parts[k])} streaming />
          ) : null}
          {busy && !live ? (
            <Group gap="xs" c="dimmed">
              <Loader size="xs" />
              <Text size="sm">{t('chat.agent-working')}</Text>
            </Group>
          ) : null}
          {notice ? (
            <Text size="sm" c="yellow.9">
              {notice}
            </Text>
          ) : null}
          {permissions.map((p) => (
            <PermissionCard key={p.id} permission={p} onAnswer={answerPermission} />
          ))}
          {forms.map((f) => (
            <FormCard key={f.id} form={f} sessionId={sessionId} onDone={() => setForms((fs) => fs.filter((x) => x.id !== f.id))} />
          ))}
        </Stack>
      </ScrollArea>
      <Composer busy={busy} onSend={send} onStop={() => attempt(() => post(`${OC}/session/${sessionId}/interrupt`, {}))} />
    </>
  )
}

function appendText(live: Live | null, messageId: string, key: string, kind: 'text' | 'reasoning', delta: string): Live {
  const l = live && live.id === messageId ? live : { id: messageId, order: [], parts: {} }
  const existing = l.parts[key] as { type: 'text' | 'reasoning'; text: string } | undefined
  return {
    ...l,
    order: existing ? l.order : [...l.order, key],
    parts: { ...l.parts, [key]: { type: kind, text: (existing?.text ?? '') + delta } },
  }
}

function putTool(live: Live | null, messageId: string, id: string, name: string | undefined, state: Partial<ToolState>): Live {
  const l = live && live.id === messageId ? live : { id: messageId, order: [], parts: {} }
  const key = `tool:${id}`
  const existing = l.parts[key] as Extract<Part, { type: 'tool' }> | undefined
  const next: Part = {
    type: 'tool',
    id,
    name: name ?? existing?.name ?? '?',
    state: { ...(existing?.state ?? { status: 'streaming' }), ...state } as ToolState,
  }
  return { ...l, order: existing ? l.order : [...l.order, key], parts: { ...l.parts, [key]: next } }
}

// ---------------------------------------------------------------- messages
function MessageView({ message: m }: { message: Message }) {
  const t = useT()
  switch (m.type) {
    case 'user':
      return (
        <Group justify="flex-end">
          <div className="bubble-user">{m.text}</div>
        </Group>
      )
    case 'assistant':
      return (
        <Stack gap={6}>
          <AssistantParts parts={m.content ?? []} />
          {m.error ? (
            <Alert color="red" py={6} title={t('chat.model-error')}>
              {m.error.message}
            </Alert>
          ) : null}
        </Stack>
      )
    case 'shell':
      return (
        <Card withBorder p="xs">
          <Text size="xs" c="dimmed">
            shell ({m.status})
          </Text>
          <Code block>{`$ ${m.command}\n${m.output?.output ?? ''}`}</Code>
        </Card>
      )
    case 'idle':
      if (m.outcome === 'failed' && m.error)
        return (
          <Alert color="red" py={6} title={t('chat.turn-error')}>
            {m.error.message}
          </Alert>
        )
      if (m.outcome === 'interrupted')
        return (
          <Text size="xs" c="dimmed" ta="center">
            {t('chat.stopped')}
          </Text>
        )
      return null
    case 'synthetic':
    case 'system':
    case 'skill':
      return <Fold label={m.description || (m.type === 'skill' ? 'skill' : t('chat.system-note'))} text={m.text ?? ''} />
    case 'agent-switched':
      return (
        <Text size="xs" c="dimmed" ta="center">
          {t('chat.agent-switched', { agent: m.agent ?? '' })}
        </Text>
      )
    case 'model-switched':
      return (
        <Text size="xs" c="dimmed" ta="center">
          {t('chat.model-switched', { model: m.model?.id ?? '' })}
        </Text>
      )
    default:
      if (m.type.startsWith('compaction'))
        return (
          <Text size="xs" c="dimmed" ta="center">
            {t('chat.compaction')}
          </Text>
        )
      return null
  }
}

function Fold({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <Box>
      <UnstyledButton onClick={() => setOpen((o) => !o)}>
        <Group gap={4} c="dimmed">
          {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          <Text size="xs">{label}</Text>
        </Group>
      </UnstyledButton>
      <Collapse expanded={open}>
        <Text size="sm" c="dimmed" style={{ whiteSpace: 'pre-wrap' }} pl="md">
          {text}
        </Text>
      </Collapse>
    </Box>
  )
}

function AssistantParts({ parts, streaming }: { parts: Part[]; streaming?: boolean }) {
  const t = useT()
  return (
    <Stack gap={6}>
      {parts.map((p, i) => {
        if (p.type === 'text') return p.text.trim() ? <Markdown key={i} text={p.text} /> : null
        if (p.type === 'reasoning') return p.text.trim() ? <Fold key={i} label={t('chat.thinking')} text={p.text} /> : null
        return <ToolCard key={p.id} name={p.name} state={p.state} />
      })}
      {streaming && parts.length === 0 ? (
        <Group gap="xs" c="dimmed">
          <Loader size="xs" />
          <Text size="sm">{t('chat.writing')}</Text>
        </Group>
      ) : null}
    </Stack>
  )
}

function ToolCard({ name, state }: { name: string; state: ToolState }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const output = toolOutput(state)
  const result = useMemo(() => parseJson(output), [output])
  const isKite = /^kite[_.:-]/.test(name)
  const tool = name.replace(/^kite[_.:-]/, '')
  const summary = toolSummary(tool, state.input)
  const color = state.status === 'error' ? 'red' : state.status === 'completed' ? 'teal' : 'blue'
  return (
    <Paper withBorder p={8} radius="md">
      <Group gap={8} wrap="nowrap" className="clickable" onClick={() => setOpen((o) => !o)} style={{ borderRadius: 6 }}>
        {state.status === 'completed' ? (
          <IconCheck size={16} color={`var(--mantine-color-${color}-5)`} />
        ) : state.status === 'error' ? (
          <IconAlertTriangle size={16} color="var(--mantine-color-red-5)" />
        ) : (
          <Loader size={14} />
        )}
        <IconTool size={14} color="var(--mantine-color-dimmed)" />
        <Text size="sm" fw={600}>
          {toolLabel(name, t)}
        </Text>
        <Text size="sm" c="dimmed" truncate style={{ flex: 1 }}>
          {summary}
        </Text>
        {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
      </Group>
      {state.status === 'error' && state.error ? (
        <Text size="sm" c="red" mt={4}>
          {state.error.message}
        </Text>
      ) : null}
      {isKite && result ? <KiteActions tool={tool} result={result} /> : null}
      <Collapse expanded={open}>
        <Stack gap={6} mt={6}>
          {state.input !== undefined ? (
            <>
              <Text size="xs" c="dimmed">
                {t('chat.tool-input')}
              </Text>
              <JsonView data={state.input} maxHeight={220} />
            </>
          ) : null}
          {output ? (
            <>
              <Text size="xs" c="dimmed">
                {t('chat.tool-output')}
              </Text>
              <JsonView data={result ?? output} maxHeight={320} />
            </>
          ) : null}
        </Stack>
      </Collapse>
    </Paper>
  )
}

function toolSummary(tool: string, input: any): string {
  if (!input || typeof input !== 'object') return typeof input === 'string' ? short(input, 90) : ''
  if (tool === 'start_task') return short(`${input.goal ?? ''}${input.repo ? ` · ${input.repo}` : ''}`, 110)
  if (input.command) return short(input.command, 110)
  if (input.task_id) return input.task_id
  if (input.repo) return input.repo
  if (input.questions?.[0]?.question) return short(input.questions[0].question, 110)
  return short(JSON.stringify(input), 110)
}

/** Buttons under Kite's tool results: open the task; apply and commit are the owner's clicks. */
function KiteActions({ tool, result }: { tool: string; result: any }) {
  const t = useT()
  const taskId: string | undefined = result?.task_id
  if (!taskId) return null
  const ready = tool === 'task_result' && result.ready && result.patch
  const applied = tool === 'apply_result' && result.applied
  return (
    <Group gap="xs" mt={6}>
      <Button size="compact-xs" variant="light" onClick={() => go(`/tasks/${encodeURIComponent(taskId)}`)}>
        {t('chat.open-task')}
      </Button>
      {ready ? (
        <ConfirmButton
          size="compact-xs"
          color="teal"
          label={t('chat.apply-patch')}
          title={t('chat.apply-patch-title')}
          message={<Text size="sm">{t('chat.apply-patch-message', { task: taskId })}</Text>}
          confirmLabel={t('chat.apply')}
          onConfirm={() => attempt(() => post(`/api/tasks/${encodeURIComponent(taskId)}/apply`), t('chat.applied'))}
        />
      ) : null}
      {ready || applied ? (
        <ConfirmButton
          size="compact-xs"
          variant="light"
          label={t('chat.commit')}
          title={t('chat.commit-title')}
          askText
          textLabel={t('chat.commit-message-label')}
          message={<Text size="sm">{t('chat.commit-explain')}</Text>}
          confirmLabel={t('chat.tool.commit-result')}
          onConfirm={(message) => attempt(() => post(`/api/tasks/${encodeURIComponent(taskId)}/commit`, { message }), t('chat.committed'))}
        />
      ) : null}
    </Group>
  )
}

function PermissionCard({ permission: p, onAnswer }: { permission: Permission; onAnswer: (p: Permission, d: 'once' | 'reject') => void }) {
  const t = useT()
  return (
    <Alert color="yellow" title={p.action === 'shell' ? t('chat.permission-shell') : t('chat.permission-other', { action: p.action })}>
      <Stack gap="xs">
        {p.message ? <Text size="sm">{p.message}</Text> : null}
        <Code block>{p.resources.join('\n')}</Code>
        <Group gap="xs">
          <Button size="xs" color="yellow" onClick={() => onAnswer(p, 'once')}>
            {t('chat.allow-once')}
          </Button>
          <Button size="xs" variant="default" onClick={() => onAnswer(p, 'reject')}>
            {t('chat.reject')}
          </Button>
        </Group>
      </Stack>
    </Alert>
  )
}

function FormCard({ form, sessionId, onDone }: { form: Form; sessionId: string; onDone: () => void }) {
  const t = useT()
  const [answer, setAnswer] = useState<Record<string, string | string[]>>({})
  const [custom, setCustom] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const complete = form.fields.every((f) => {
    const v = custom[f.key]?.trim() || answer[f.key]
    return Array.isArray(v) ? v.length > 0 : !!v
  })
  return (
    <Card withBorder>
      <Text fw={600} mb="xs">
        {t('chat.agent-asks')}
      </Text>
      <Stack gap="md">
        {form.fields.map((f) => (
          <Stack key={f.key} gap={6}>
            <Text size="sm" fw={500}>
              {f.description || f.title}
            </Text>
            {f.options?.length ? (
              f.multiple || f.type === 'array' ? (
                <Checkbox.Group value={(answer[f.key] as string[]) ?? []} onChange={(v) => setAnswer((a) => ({ ...a, [f.key]: v }))}>
                  <Stack gap={4}>
                    {f.options.map((o) => (
                      <Checkbox key={o.value} value={o.value} label={o.label ?? o.value} description={o.description} />
                    ))}
                  </Stack>
                </Checkbox.Group>
              ) : (
                <Radio.Group value={(answer[f.key] as string) ?? ''} onChange={(v) => setAnswer((a) => ({ ...a, [f.key]: v }))}>
                  <Stack gap={4}>
                    {f.options.map((o) => (
                      <Radio key={o.value} value={o.value} label={o.label ?? o.value} description={o.description} />
                    ))}
                  </Stack>
                </Radio.Group>
              )
            ) : null}
            {f.custom || !f.options?.length ? (
              <TextInput
                size="sm"
                placeholder={f.options?.length ? t('chat.answer-custom') : t('chat.answer-placeholder')}
                value={custom[f.key] ?? ''}
                onChange={(e) => {
                  const value = e.currentTarget.value
                  setCustom((c) => ({ ...c, [f.key]: value }))
                }}
              />
            ) : null}
          </Stack>
        ))}
        <Group>
          <Button
            size="xs"
            loading={busy}
            disabled={!complete}
            onClick={async () => {
              setBusy(true)
              const body: Record<string, string | string[]> = {}
              for (const f of form.fields) body[f.key] = custom[f.key]?.trim() || answer[f.key]
              const ok = await attempt(() => post(`${OC}/session/${sessionId}/form/${form.id}/reply`, { answer: body }))
              setBusy(false)
              if (ok !== undefined) onDone()
            }}
          >
            {t('chat.answer')}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

function Composer({ busy, onSend, onStop }: { busy: boolean; onSend: (text: string) => Promise<boolean>; onStop: () => void }) {
  const t = useT()
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  async function submit() {
    const value = text.trim()
    if (!value || sending) return
    setSending(true)
    if (await onSend(value)) setText('')
    setSending(false)
  }
  return (
    <Box p="sm" style={{ borderTop: '1px solid var(--mantine-color-default-border)' }}>
      <Group gap="xs" align="flex-end" wrap="nowrap" maw={900} mx="auto">
        <Textarea
          style={{ flex: 1 }}
          autosize
          minRows={1}
          maxRows={8}
          placeholder={busy ? t('chat.composer-busy') : t('chat.composer-placeholder')}
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              submit()
            }
          }}
        />
        {busy ? (
          <Tooltip label={t('chat.stop-turn')}>
            <ActionIcon size={36} color="red" variant="light" onClick={onStop}>
              <IconPlayerStopFilled size={18} />
            </ActionIcon>
          </Tooltip>
        ) : null}
        <ActionIcon size={36} onClick={submit} loading={sending} disabled={!text.trim()} title={t('chat.send')}>
          <IconSend size={18} />
        </ActionIcon>
      </Group>
    </Box>
  )
}
