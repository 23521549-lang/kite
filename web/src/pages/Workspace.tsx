import { Alert, Badge, Button, Card, Group, SegmentedControl, Stack, Text, TextInput, Textarea } from '@mantine/core'
import { IconMessageChatbot, IconSend } from '@tabler/icons-react'
import { useCallback, useEffect, useState } from 'react'
import {
  createSchedule,
  deleteSchedule,
  get,
  getNeedsDecision,
  getSchedules,
  getSecretaryReport,
  getSecretaryReports,
  getWorkspaceQueue,
  patch,
  post,
  type NeedsDecisionItem,
  type ScheduleRow,
  type SecretaryReport,
} from '../api'
import { ConfirmButton, Empty, Loading, Markdown, Page } from '../components/common'
import { go } from '../router'
import { useT, type DictKey } from '../i18n'
import { ago, attempt, fmtTime, notifyError, useLoad } from '../util'

const KIND_LABEL_KEYS: Record<string, DictKey> = {
  patch_awaiting: 'workspace.needs.patch',
  lesson_pending: 'workspace.needs.lesson',
  quota_near: 'workspace.needs.quota',
  task_failed: 'workspace.needs.failed',
}

// ---------------------------------------------------------------- OpenCode shapes (the parts used here)
type OCSession = { id: string; title?: string; time?: { updated: number } }
type OCPart = { type: string; text?: string }
type OCMessage = { id: string; role?: string; info?: { role?: string }; parts?: OCPart[]; content?: OCPart[] }

function messageText(m: OCMessage): string {
  const parts = m.parts ?? m.content ?? []
  return parts
    .filter((p) => p?.type === 'text')
    .map((p) => p.text ?? '')
    .join('\n')
    .trim()
}

/** A small chat against a narrow OpenCode proxy: create session + prompt + list messages. */
function ProxyChat({ base, title }: { base: string; title: string }) {
  const t = useT()
  const [sessions, setSessions] = useState<OCSession[] | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  // History loads as soon as a session is picked (and polls while the panel is open); with
  // no session yet there is nothing to read, so no request is sent.
  const messages = useLoad(
    () =>
      sessionId
        ? get<{ data: OCMessage[] }>(`${base}/api/session/${encodeURIComponent(sessionId)}/message`)
        : Promise.resolve({ data: [] as OCMessage[] }),
    [sessionId],
    sessionId ? 5000 : undefined,
  )

  const loadSessions = useCallback(async () => {
    try {
      const res = await get<{ data: OCSession[] }>(`${base}/api/session`)
      const list = (res.data ?? []).sort((a, b) => (b.time?.updated ?? 0) - (a.time?.updated ?? 0))
      setSessions(list)
      if (list.length > 0) setSessionId(list[0].id)
    } catch (e) {
      notifyError(e, t('workspace.review.load-sessions-error'))
      setSessions([])
    }
  }, [base])

  useEffect(() => {
    loadSessions()
  }, [loadSessions])

  async function newSession() {
    const res = await attempt(() => post<{ data: OCSession }>(base, {}))
    if (res?.data?.id) {
      await loadSessions()
      setSessionId(res.data.id)
    }
  }

  async function send() {
    const text = input.trim()
    if (!text || !sessionId || sending) return
    setSending(true)
    setInput('')
    try {
      await post(`${base}/api/session/${encodeURIComponent(sessionId)}/prompt`, { text })
      await messages.reload()
    } catch (e) {
      notifyError(e, t('workspace.review.send-error'))
    } finally {
      setSending(false)
    }
  }

  const list = messages.data?.data ?? []
  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Text fw={600}>{title}</Text>
        <Button size="xs" variant="light" onClick={newSession}>
          {t('workspace.review.new-session')}
        </Button>
      </Group>
      {sessions === null ? (
        <Loading />
      ) : !sessionId ? (
        <Text size="sm" c="dimmed">
          {t('workspace.review.no-session')}
        </Text>
      ) : (
        <>
          <Stack gap="xs">
            {list.length === 0 ? (
              <Text size="sm" c="dimmed">
                {t('workspace.review.empty')}
              </Text>
            ) : (
              list.map((m) => {
                const text = messageText(m)
                if (!text) return null
                return (
                  <Card key={m.id} withBorder p="sm">
                    <Text size="xs" c="dimmed" mb={4}>
                      {m.role ?? m.info?.role ?? 'agent'}
                    </Text>
                    <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                      {text}
                    </Text>
                  </Card>
                )
              })
            )}
          </Stack>
          <Group gap="xs" align="flex-end">
            <Textarea
              flex={1}
              minRows={1}
              autosize
              placeholder={t('workspace.review.placeholder')}
              value={input}
              onChange={(e) => setInput(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  send()
                }
              }}
            />
            <Button leftSection={<IconSend size={16} />} onClick={send} loading={sending}>
              {t('workspace.review.send')}
            </Button>
          </Group>
        </>
      )}
    </Stack>
  )
}

// ---------------------------------------------------------------- the page
export default function Workspace() {
  const t = useT()
  return (
    <Page title={t('workspace.title')}>
      <Stack>
        <Queue />
        <NeedsDecision />
        <Schedules />
        <Secretary />
      </Stack>
    </Page>
  )
}

function Queue() {
  const t = useT()
  const queue = useLoad(() => getWorkspaceQueue(), [], 30000)
  const [reviewTask, setReviewTask] = useState<NeedsDecisionItem | null>(null)
  const items = queue.data?.items ?? []
  return (
    <Card withBorder>
      <Text fw={600} mb="sm">
        {t('workspace.queue.title')}
      </Text>
      {queue.data ? (
        items.length === 0 ? (
          <Empty>{t('workspace.queue.empty')}</Empty>
        ) : (
          <Stack gap="xs">
            {items.map((item) => (
              <Group key={item.key} justify="space-between" wrap="wrap">
                <Stack gap={0}>
                  <Text size="sm">{item.title || item.task_id}</Text>
                  <Text size="xs" c="dimmed">
                    {ago(item.created_at)}
                  </Text>
                </Stack>
                <Group gap="xs">
                  <Button size="xs" variant="light" onClick={() => setReviewTask(item)}>
                    {t('workspace.queue.review')}
                  </Button>
                  <Button size="xs" variant="subtle" onClick={() => go(item.link)}>
                    {t('workspace.queue.open-task')}
                  </Button>
                </Group>
              </Group>
            ))}
          </Stack>
        )
      ) : (
        <Loading error={queue.error} />
      )}
      {reviewTask?.task_id ? (
        <Stack gap="sm" mt="md">
          <Alert color="yellow" title={t('workspace.review.title')}>
            {t('workspace.review.note')}
          </Alert>
          <ProxyChat
            base={`/api/workspace/review/${encodeURIComponent(reviewTask.task_id)}/chat`}
            title={`${t('workspace.review.title')} — ${reviewTask.task_id}`}
          />
        </Stack>
      ) : null}
    </Card>
  )
}

function NeedsDecision() {
  const t = useT()
  const needs = useLoad(() => getNeedsDecision(), [], 30000)
  const items = needs.data?.items ?? []
  return (
    <Card withBorder>
      <Text fw={600} mb="sm">
        {t('workspace.needs.title')}
      </Text>
      {needs.data ? (
        items.length === 0 ? (
          <Empty>{t('workspace.needs.empty')}</Empty>
        ) : (
          <Stack gap="xs">
            {items.map((item) => (
              <Group key={item.key} justify="space-between" wrap="wrap">
                <Stack gap={2}>
                  <Group gap="xs">
                    <Badge color={item.kind === 'task_failed' ? 'red' : item.kind === 'quota_near' ? 'yellow' : 'blue'}>
                      {KIND_LABEL_KEYS[item.kind] ? t(KIND_LABEL_KEYS[item.kind]) : item.kind}
                    </Badge>
                    <Text size="sm">{item.title}</Text>
                  </Group>
                  <Text size="xs" c="dimmed">
                    {item.detail} · {ago(item.created_at)}
                  </Text>
                </Stack>
                <Button size="xs" variant="light" onClick={() => go(item.link)}>
                  {t('workspace.needs.open')}
                </Button>
              </Group>
            ))}
          </Stack>
        )
      ) : (
        <Loading error={needs.error} />
      )}
    </Card>
  )
}

function Schedules() {
  const t = useT()
  const schedules = useLoad(() => getSchedules(), [], 30000)
  const [planPath, setPlanPath] = useState('')
  const [kind, setKind] = useState('once')
  const [runAt, setRunAt] = useState('09:00')
  const [weekdays, setWeekdays] = useState('')
  const rows = schedules.data?.schedules ?? []

  async function create() {
    const body: Record<string, unknown> = { plan_path: planPath.trim(), kind, run_at: runAt.trim() }
    if (kind === 'weekly') {
      body.weekdays = weekdays
        .split(',')
        .map((v) => Number(v.trim()))
        .filter((v) => v >= 1 && v <= 7)
    }
    const created = await attempt(() => createSchedule(body))
    if (created) {
      setPlanPath('')
      await schedules.reload()
    }
  }

  async function toggle(row: ScheduleRow) {
    const next = row.status === 'enabled' ? 'disabled' : 'enabled'
    const done = await attempt(() => patch(`/api/schedules/${encodeURIComponent(row.id)}`, { status: next }))
    if (done) await schedules.reload()
  }

  async function remove(row: ScheduleRow) {
    const done = await attempt(() => deleteSchedule(row.id))
    if (done) await schedules.reload()
  }

  return (
    <Card withBorder>
      <Text fw={600} mb="sm">
        {t('workspace.schedules.title')}
      </Text>
      {schedules.data ? (
        rows.length === 0 ? (
          <Empty>{t('workspace.schedules.empty')}</Empty>
        ) : (
          <Stack gap="xs">
            {rows.map((row) => (
              <Group key={row.id} justify="space-between" wrap="wrap">
                <Stack gap={0}>
                  <Text size="sm">{row.plan_path}</Text>
                  <Text size="xs" c="dimmed">
                    {row.kind} · {row.run_at}
                    {row.next_run ? ` · ${t('workspace.schedules.next-run')}: ${fmtTime(row.next_run)}` : ''}
                    {row.last_run ? ` · ${t('workspace.schedules.last-run')}: ${fmtTime(row.last_run)}` : ''}
                  </Text>
                </Stack>
                <Group gap="xs">
                  <Badge color={row.status === 'enabled' ? 'teal' : row.status === 'expired' ? 'gray' : 'yellow'}>
                    {row.status === 'enabled'
                      ? t('workspace.schedules.enabled')
                      : row.status === 'expired'
                        ? t('workspace.schedules.expired')
                        : t('workspace.schedules.disabled')}
                  </Badge>
                  {row.status !== 'expired' ? (
                    <Button size="xs" variant="light" onClick={() => toggle(row)}>
                      {row.status === 'enabled' ? t('workspace.schedules.disable') : t('workspace.schedules.enable')}
                    </Button>
                  ) : null}
                  <ConfirmButton
                    size="xs"
                    variant="subtle"
                    color="red"
                    label={t('workspace.schedules.delete-label')}
                    title={t('workspace.schedules.delete-title')}
                    message={t('workspace.schedules.delete')}
                    onConfirm={() => remove(row)}
                  />
                </Group>
              </Group>
            ))}
          </Stack>
        )
      ) : (
        <Loading error={schedules.error} />
      )}
      <Stack gap="xs" mt="md">
        <TextInput
          label={t('workspace.schedules.plan-path')}
          placeholder={t('workspace.schedules.plan-path-ph')}
          value={planPath}
          onChange={(e) => setPlanPath(e.currentTarget.value)}
        />
        <Group gap="xs" align="flex-end">
          <SegmentedControl
            data={[
              { value: 'once', label: t('workspace.schedules.once') },
              { value: 'daily', label: t('workspace.schedules.daily') },
              { value: 'weekly', label: t('workspace.schedules.weekly') },
            ]}
            value={kind}
            onChange={setKind}
          />
          <TextInput
            label={t('workspace.schedules.run-at')}
            value={runAt}
            onChange={(e) => setRunAt(e.currentTarget.value)}
            w={140}
          />
          {kind === 'weekly' ? (
            <TextInput
              label={t('workspace.schedules.weekdays')}
              value={weekdays}
              onChange={(e) => setWeekdays(e.currentTarget.value)}
              flex={1}
            />
          ) : null}
          <Button onClick={create} disabled={!planPath.trim()}>
            {t('workspace.schedules.create')}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

function Secretary() {
  const t = useT()
  const reports = useLoad(() => getSecretaryReports(), [], 60000)
  const [open, setOpen] = useState(false)
  const [current, setCurrent] = useState<{ date: string; markdown: string } | null>(null)
  const list = reports.data?.reports ?? []

  async function openReport(report: SecretaryReport) {
    const data = await attempt(() => getSecretaryReport(report.date))
    if (data) setCurrent(data)
  }

  return (
    <Card withBorder>
      <Group justify="space-between" mb="sm">
        <Text fw={600}>{t('workspace.secretary.title')}</Text>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconMessageChatbot size={16} />}
          onClick={() => setOpen((v) => !v)}
        >
          {t('workspace.secretary.open')}
        </Button>
      </Group>
      {open ? <ProxyChat base="/api/secretary/chat" title={t('workspace.secretary.title')} /> : null}
      {reports.data ? (
        list.length === 0 ? (
          <Empty>{t('workspace.secretary.empty')}</Empty>
        ) : (
          <Stack gap="xs" mt="sm">
            {list.map((report) => (
              <Group key={report.date} justify="space-between">
                <Text size="sm">{report.title || report.date}</Text>
                <Button size="xs" variant="light" onClick={() => openReport(report)}>
                  {t('workspace.needs.open')}
                </Button>
              </Group>
            ))}
          </Stack>
        )
      ) : (
        <Loading error={reports.error} />
      )}
      {current ? (
        <Stack gap="xs" mt="md">
          <Text fw={600} size="sm">
            {current.date}
          </Text>
          <Markdown text={current.markdown} />
        </Stack>
      ) : null}
    </Card>
  )
}
