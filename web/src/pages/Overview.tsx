import { Badge, Card, Group, SegmentedControl, SimpleGrid, Stack, Table, Text, Title, useComputedColorScheme } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { Suspense, lazy, useMemo, useState } from 'react'
import { get, type Activity, type Overview as OverviewData, type WorkerRow } from '../api'
import { Dot, Empty, Loading, Page, StateBadge } from '../components/common'
import type { OfficeState, Pick, WorkerState } from '../office/scene'
import { go } from '../router'
import { useKite } from '../store'
import { ago, short, usd, useLoad, workerState } from '../util'

// three.js is only downloaded when the 3D office is shown.
const Office3D = lazy(() => import('../office/Office3D'))
const MODE_KEY = 'kite.overview.mode'
const PHASES: Record<string, string> = { fast_path: 'đường nhanh', review: 'rà soát' }

function hasWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    return !!(canvas.getContext('webgl2') || canvas.getContext('webgl'))
  } catch {
    return false
  }
}

function savedMode(): '3d' | '2d' {
  try {
    const value = localStorage.getItem(MODE_KEY)
    if (value === '2d' || value === '3d') return value
  } catch {
    /* no storage: the default */
  }
  // Phones start with the cards: five desks on a small screen are hard to read.
  return window.matchMedia?.('(max-width: 640px)').matches ? '2d' : '3d'
}

function officeWorker(w: WorkerRow): OfficeState['workers'][number] {
  const st = workerState(w)
  const state: WorkerState =
    w.start?.state === 'error'
      ? 'error'
      : w.alive && w.busy
        ? 'busy'
        : w.alive && !w.draining
          ? 'idle'
          : st.label === 'đang khởi động' || w.draining
            ? 'starting'
            : 'off'
  return {
    id: w.id,
    type: w.type,
    state,
    stateLabel: st.label,
    model: w.loaded_model ?? w.models[0] ?? '',
    running: w.running,
    slots: w.slots ?? w.concurrency,
  }
}

function officeTask(a: Activity): OfficeState['tasks'][number] {
  const step = [a.step ? `bước ${a.step}` : null, a.action ? `vừa ${a.action}` : null].filter(Boolean).join(' · ')
  return { id: a.task_id, goal: short(a.goal || a.task_id, 70), worker: a.worker, step: step || null }
}

function onPick(pick: Pick) {
  if (pick.kind === 'worker') go('/workers')
  else if (pick.kind === 'task') go(`/tasks/${encodeURIComponent(pick.id)}`)
  else if (pick.kind === 'lead') go('/chat')
  else if (pick.kind === 'gateway') go('/journal')
  else go('/tasks')
}

export default function Overview() {
  const { data, error } = useLoad(() => get<OverviewData>('/api/overview'), [], 10000)
  const activity = useKite((s) => s.activity)
  const events = useKite((s) => s.events)
  const gatewayUp = useKite((s) => s.gatewayUp)
  const chat = useLoad(() => get<{ busy?: number }>('/api/chat/info'), [], 15000)
  const dark = useComputedColorScheme('dark') === 'dark'
  const small = useMediaQuery('(max-width: 48em)')
  const [webgl] = useState(hasWebGL)
  const [mode, setMode] = useState<'3d' | '2d'>(() => (hasWebGL() ? savedMode() : '2d'))
  const office = useMemo<OfficeState | null>(
    () =>
      data
        ? {
            workers: data.workers.map(officeWorker),
            tasks: activity.map(officeTask),
            chatBusy: (chat.data?.busy ?? 0) > 0,
            gatewayUp: gatewayUp ?? data.gateway_up,
            dark,
          }
        : null,
    [data, activity, chat.data?.busy, gatewayUp, dark],
  )
  if (!data || !office) return <Loading error={error} />
  const quota = data.gpu_quota ?? {}
  return (
    <Page title="Tổng quan">
      <SimpleGrid cols={{ base: 2, md: 4 }}>
        <Stat label="Gateway" value={data.gateway_up ? 'đang chạy' : 'tắt'} color={data.gateway_up ? 'teal' : 'red'} />
        <Stat label="Việc đang chờ / đang chạy" value={`${data.queued_jobs} / ${data.running_jobs}`} />
        <Stat label="Chi hôm nay" value={usd(data.spend.today_usd)} />
        <Stat label="Chi tháng này" value={usd(data.spend.month_usd)} />
      </SimpleGrid>
      {Object.keys(quota).length ? (
        <Card withBorder>
          <Text fw={600} mb={6}>
            Quota GPU Kaggle
          </Text>
          {Object.entries(quota).map(([worker, q]) => (
            <Text key={worker} size="sm">
              <b>{worker}</b>: {q.detail}
            </Text>
          ))}
        </Card>
      ) : null}
      <Card withBorder p="xs">
        <Group justify="space-between" px={6} pb={6}>
          <Text fw={600}>Văn phòng Kite</Text>
          <SegmentedControl
            size="xs"
            data={[
              { value: '3d', label: '3D', disabled: !webgl },
              { value: '2d', label: '2D' },
            ]}
            value={mode}
            onChange={(v) => {
              const next = v === '3d' ? '3d' : '2d'
              setMode(next)
              try {
                localStorage.setItem(MODE_KEY, next)
              } catch {
                /* the choice lasts for this visit only */
              }
            }}
          />
        </Group>
        {mode === '3d' ? (
          <Suspense fallback={<Loading label="Đang tải văn phòng 3D…" />}>
            <Office3D state={office} onPick={onPick} height={small ? 360 : 500} />
          </Suspense>
        ) : (
          <WorkerCards workers={data.workers} />
        )}
        {!webgl ? (
          <Text size="xs" c="dimmed" px={6} pt={6}>
            Trình duyệt này không có WebGL nên chỉ xem được 2D.
          </Text>
        ) : null}
      </Card>
      <Title order={4}>Đang làm</Title>
      {activity.length ? (
        <Stack gap="xs">
          {activity.map((a) => (
            <Card key={a.task_id} withBorder p="sm" className="clickable" onClick={() => go(`/tasks/${encodeURIComponent(a.task_id)}`)}>
              <Group justify="space-between" wrap="nowrap">
                <Text fw={600} truncate>
                  {a.goal || a.task_id}
                </Text>
                <StateBadge state={a.state} />
              </Group>
              <Text size="sm" c="dimmed">
                {[
                  a.worker ? `worker ${a.worker}` : null,
                  a.phase ? PHASES[a.phase] ?? a.phase : null,
                  a.step ? `bước ${a.step}` : null,
                  a.action ? `vừa ${a.action}` : null,
                  a.since ? `từ ${ago(a.since)}` : null,
                ]
                  .filter(Boolean)
                  .join(' · ') || (a.state === 'QUEUED' ? 'đang chờ worker' : 'đang chuẩn bị')}
              </Text>
            </Card>
          ))}
        </Stack>
      ) : (
        <Empty>Không có task nào đang chạy.</Empty>
      )}
      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Card withBorder>
          <Text fw={600} mb="xs">
            Task gần đây
          </Text>
          {data.tasks.recent.length ? (
            <Table highlightOnHover>
              <Table.Tbody>
                {data.tasks.recent.map((t) => (
                  <Table.Tr key={t.task_id} className="clickable" onClick={() => go(`/tasks/${encodeURIComponent(t.task_id)}`)}>
                    <Table.Td>
                      <Text size="sm" lineClamp={1}>
                        {t.goal || t.task_id}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <StateBadge state={t.state} />
                    </Table.Td>
                    <Table.Td>
                      <Text size="xs" c="dimmed">
                        {t.created}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          ) : (
            <Empty>Chưa có task.</Empty>
          )}
        </Card>
        <Card withBorder>
          <Text fw={600} mb="xs">
            Sự kiện mới
          </Text>
          {events.length ? (
            <Stack gap={4}>
              {events.slice(0, 14).map((e, i) => (
                <Text key={i} size="xs" className="mono" lineClamp={1}>
                  <Text span c="dimmed">
                    {new Date(e.at).toLocaleTimeString('vi-VN', { hour12: false })} {e.file}
                  </Text>{' '}
                  {short(describe(e.record), 160)}
                </Text>
              ))}
            </Stack>
          ) : (
            <Empty>Sự kiện mới sẽ hiện ở đây khi Kite làm việc.</Empty>
          )}
        </Card>
      </SimpleGrid>
    </Page>
  )
}

function WorkerCards({ workers }: { workers: WorkerRow[] }) {
  if (!workers.length) return <Empty>Chưa có worker nào: thêm ở tab Provider.</Empty>
  return (
    <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} p={6}>
      {workers.map((w) => {
        const st = workerState(w)
        return (
          <Card key={w.id} withBorder className="clickable" onClick={() => go('/workers')}>
            <Group justify="space-between">
              <Group gap={8}>
                <Dot color={st.color} />
                <Text fw={600}>{w.id}</Text>
              </Group>
              <Badge variant="light" color={st.color}>
                {st.label}
              </Badge>
            </Group>
            <Text size="sm" c="dimmed" mt={4}>
              {w.type} · {w.loaded_model ? `đang nạp ${w.loaded_model}` : w.models.slice(0, 3).join(', ')}
            </Text>
            {w.alive && w.busy ? (
              <Text size="sm" mt={4}>
                {w.running}/{w.slots ?? w.concurrency} việc đang chạy
              </Text>
            ) : null}
            {w.start?.detail ? (
              <Text size="xs" c="yellow" mt={4}>
                {short(w.start.detail, 140)}
              </Text>
            ) : null}
          </Card>
        )
      })}
    </SimpleGrid>
  )
}

function describe(record: Record<string, any>): string {
  const bits = [record.tool, record.event, record.worker, record.model, record.task_id ?? record.tag, record.outcome, record.detail]
  const text = bits.filter((b) => b !== undefined && b !== null && b !== '').join(' · ')
  return text || JSON.stringify(record)
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <Card withBorder p="md">
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text fw={700} size="lg" c={color}>
        {value}
      </Text>
    </Card>
  )
}
