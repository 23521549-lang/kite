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
import { ago, attempt, fmtTime, notifyError, useLoad } from '../util'

// Chuỗi gom ở đây để Gói 0(b) di trú cơ học sang lớp i18n sau (key workspace.<mục>.<trường>).
const STR: Record<string, string> = {
  'workspace.title': 'Không gian làm việc',
  'workspace.queue.title': 'Hàng chờ duyệt',
  'workspace.queue.empty': 'Không có patch nào chờ duyệt.',
  'workspace.queue.review': 'Review',
  'workspace.queue.open-task': 'Mở task',
  'workspace.review.title': 'Trợ thủ review',
  'workspace.review.note': 'Trợ thủ chỉ tư vấn — không áp patch, không commit. Áp/Commit vẫn ở trang Task, bằng mã xác nhận của bạn.',
  'workspace.review.new-session': 'Phiên mới',
  'workspace.review.send': 'Gửi',
  'workspace.review.placeholder': 'Hỏi về patch này…',
  'workspace.review.no-session': 'Chưa có phiên review. Bấm “Phiên mới” để mở trò chuyện.',
  'workspace.review.empty': 'Chưa có tin nhắn.',
  'workspace.schedules.title': 'Kế hoạch',
  'workspace.schedules.empty': 'Chưa có kế hoạch nào.',
  'workspace.schedules.plan-path': 'File plan (.md)',
  'workspace.schedules.plan-path-ph': 'vd /home/han/ke-hoach/tuan-toi.md',
  'workspace.schedules.run-at': 'Giờ chạy (HH:MM, giờ Bangkok)',
  'workspace.schedules.weekdays': 'Các thứ trong tuần (1=Thứ 2 … 7=CN, cách nhau bằng dấu phẩy)',
  'workspace.schedules.create': 'Tạo kế hoạch',
  'workspace.schedules.once': 'Một lần',
  'workspace.schedules.daily': 'Hằng ngày',
  'workspace.schedules.weekly': 'Hằng tuần',
  'workspace.schedules.enabled': 'Đang bật',
  'workspace.schedules.disabled': 'Đang tắt',
  'workspace.schedules.expired': 'Đã hết hạn',
  'workspace.schedules.next-run': 'Chạy kế',
  'workspace.schedules.last-run': 'Chạy cuối',
  'workspace.schedules.enable': 'Bật',
  'workspace.schedules.disable': 'Tắt',
  'workspace.schedules.delete': 'Xóa kế hoạch này?',
  'workspace.secretary.title': 'Báo cáo thư ký',
  'workspace.secretary.open': 'Mở thư ký',
  'workspace.secretary.empty': 'Chưa có báo cáo nào.',
  'workspace.secretary.new': 'Báo cáo mới nhất',
  'workspace.needs.title': 'Cần bạn quyết',
  'workspace.needs.empty': 'Không có việc nào cần bạn quyết.',
  'workspace.needs.patch': 'Patch chờ duyệt',
  'workspace.needs.lesson': 'Bài học chờ duyệt',
  'workspace.needs.quota': 'Sắp chạm trần chi phí',
  'workspace.needs.failed': 'Task lỗi',
  'workspace.needs.open': 'Mở',
}

const KIND_LABEL: Record<string, string> = {
  patch_awaiting: STR['workspace.needs.patch'],
  lesson_pending: STR['workspace.needs.lesson'],
  quota_near: STR['workspace.needs.quota'],
  task_failed: STR['workspace.needs.failed'],
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
      notifyError(e, 'Không đọc được các phiên chat')
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
      notifyError(e, 'Không gửi được')
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
          {STR['workspace.review.new-session']}
        </Button>
      </Group>
      {sessions === null ? (
        <Loading />
      ) : !sessionId ? (
        <Text size="sm" c="dimmed">
          {STR['workspace.review.no-session']}
        </Text>
      ) : (
        <>
          <Stack gap="xs">
            {list.length === 0 ? (
              <Text size="sm" c="dimmed">
                {STR['workspace.review.empty']}
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
              placeholder={STR['workspace.review.placeholder']}
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
              {STR['workspace.review.send']}
            </Button>
          </Group>
        </>
      )}
    </Stack>
  )
}

// ---------------------------------------------------------------- the page
export default function Workspace() {
  return (
    <Page title={STR['workspace.title']}>
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
  const queue = useLoad(() => getWorkspaceQueue(), [], 30000)
  const [reviewTask, setReviewTask] = useState<NeedsDecisionItem | null>(null)
  const items = queue.data?.items ?? []
  return (
    <Card withBorder>
      <Text fw={600} mb="sm">
        {STR['workspace.queue.title']}
      </Text>
      {queue.data ? (
        items.length === 0 ? (
          <Empty>{STR['workspace.queue.empty']}</Empty>
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
                    {STR['workspace.queue.review']}
                  </Button>
                  <Button size="xs" variant="subtle" onClick={() => go(item.link)}>
                    {STR['workspace.queue.open-task']}
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
          <Alert color="yellow" title={STR['workspace.review.title']}>
            {STR['workspace.review.note']}
          </Alert>
          <ProxyChat
            base={`/api/workspace/review/${encodeURIComponent(reviewTask.task_id)}/chat`}
            title={`${STR['workspace.review.title']} — ${reviewTask.task_id}`}
          />
        </Stack>
      ) : null}
    </Card>
  )
}

function NeedsDecision() {
  const needs = useLoad(() => getNeedsDecision(), [], 30000)
  const items = needs.data?.items ?? []
  return (
    <Card withBorder>
      <Text fw={600} mb="sm">
        {STR['workspace.needs.title']}
      </Text>
      {needs.data ? (
        items.length === 0 ? (
          <Empty>{STR['workspace.needs.empty']}</Empty>
        ) : (
          <Stack gap="xs">
            {items.map((item) => (
              <Group key={item.key} justify="space-between" wrap="wrap">
                <Stack gap={2}>
                  <Group gap="xs">
                    <Badge color={item.kind === 'task_failed' ? 'red' : item.kind === 'quota_near' ? 'yellow' : 'blue'}>
                      {KIND_LABEL[item.kind] ?? item.kind}
                    </Badge>
                    <Text size="sm">{item.title}</Text>
                  </Group>
                  <Text size="xs" c="dimmed">
                    {item.detail} · {ago(item.created_at)}
                  </Text>
                </Stack>
                <Button size="xs" variant="light" onClick={() => go(item.link)}>
                  {STR['workspace.needs.open']}
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
        {STR['workspace.schedules.title']}
      </Text>
      {schedules.data ? (
        rows.length === 0 ? (
          <Empty>{STR['workspace.schedules.empty']}</Empty>
        ) : (
          <Stack gap="xs">
            {rows.map((row) => (
              <Group key={row.id} justify="space-between" wrap="wrap">
                <Stack gap={0}>
                  <Text size="sm">{row.plan_path}</Text>
                  <Text size="xs" c="dimmed">
                    {row.kind} · {row.run_at}
                    {row.next_run ? ` · ${STR['workspace.schedules.next-run']}: ${fmtTime(row.next_run)}` : ''}
                    {row.last_run ? ` · ${STR['workspace.schedules.last-run']}: ${fmtTime(row.last_run)}` : ''}
                  </Text>
                </Stack>
                <Group gap="xs">
                  <Badge color={row.status === 'enabled' ? 'teal' : row.status === 'expired' ? 'gray' : 'yellow'}>
                    {row.status === 'enabled'
                      ? STR['workspace.schedules.enabled']
                      : row.status === 'expired'
                        ? STR['workspace.schedules.expired']
                        : STR['workspace.schedules.disabled']}
                  </Badge>
                  {row.status !== 'expired' ? (
                    <Button size="xs" variant="light" onClick={() => toggle(row)}>
                      {row.status === 'enabled' ? STR['workspace.schedules.disable'] : STR['workspace.schedules.enable']}
                    </Button>
                  ) : null}
                  <ConfirmButton
                    size="xs"
                    variant="subtle"
                    color="red"
                    label="Xóa"
                    title="Xóa kế hoạch"
                    message={STR['workspace.schedules.delete']}
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
          label={STR['workspace.schedules.plan-path']}
          placeholder={STR['workspace.schedules.plan-path-ph']}
          value={planPath}
          onChange={(e) => setPlanPath(e.currentTarget.value)}
        />
        <Group gap="xs" align="flex-end">
          <SegmentedControl
            data={[
              { value: 'once', label: STR['workspace.schedules.once'] },
              { value: 'daily', label: STR['workspace.schedules.daily'] },
              { value: 'weekly', label: STR['workspace.schedules.weekly'] },
            ]}
            value={kind}
            onChange={setKind}
          />
          <TextInput
            label={STR['workspace.schedules.run-at']}
            value={runAt}
            onChange={(e) => setRunAt(e.currentTarget.value)}
            w={140}
          />
          {kind === 'weekly' ? (
            <TextInput
              label={STR['workspace.schedules.weekdays']}
              value={weekdays}
              onChange={(e) => setWeekdays(e.currentTarget.value)}
              flex={1}
            />
          ) : null}
          <Button onClick={create} disabled={!planPath.trim()}>
            {STR['workspace.schedules.create']}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

function Secretary() {
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
        <Text fw={600}>{STR['workspace.secretary.title']}</Text>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconMessageChatbot size={16} />}
          onClick={() => setOpen((v) => !v)}
        >
          {STR['workspace.secretary.open']}
        </Button>
      </Group>
      {open ? <ProxyChat base="/api/secretary/chat" title={STR['workspace.secretary.title']} /> : null}
      {reports.data ? (
        list.length === 0 ? (
          <Empty>{STR['workspace.secretary.empty']}</Empty>
        ) : (
          <Stack gap="xs" mt="sm">
            {list.map((report) => (
              <Group key={report.date} justify="space-between">
                <Text size="sm">{report.title || report.date}</Text>
                <Button size="xs" variant="light" onClick={() => openReport(report)}>
                  {STR['workspace.needs.open']}
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
