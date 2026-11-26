import { Badge, Card, Group, SegmentedControl, SimpleGrid, Stack, Table, Text, Title, useComputedColorScheme } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { Suspense, lazy, useMemo, useState } from 'react'
import { get, type Activity, type Overview as OverviewData, type WorkerRow } from '../api'
import { Dot, Empty, Loading, Page, StateBadge, useWorkerStateLabel } from '../components/common'
import type { OfficeState, Pick, WorkerState } from '../office/scene'
import { go } from '../router'
import { useLanguage, useT, type DictKey } from '../i18n'
import { useKite } from '../store'
import { ago, short, usd, useLoad, workerState, type WorkerStateKey } from '../util'

// three.js is only downloaded when the 3D office is shown.
const Office3D = lazy(() => import('../office/Office3D'))
const MODE_KEY = 'kite.overview.mode'
const PHASES: Record<string, DictKey> = { fast_path: 'overview.phase.fast-path', review: 'overview.phase.review' }

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

function officeWorker(w: WorkerRow, label: (key: WorkerStateKey) => string): OfficeState['workers'][number] {
  const st = workerState(w)
  const state: WorkerState =
    w.start?.state === 'error'
      ? 'error'
      : w.alive && w.busy
        ? 'busy'
        : w.alive && !w.draining
          ? 'idle'
          : st.label === 'đang khởi động' || w.draining // i18n-ignore: so sánh với nhãn nội bộ do workerState() trả về (giá trị mã để phân loại trạng thái, không phải chuỗi hiển thị) — giữ nguyên logic so sánh.
            ? 'starting'
            : 'off'
  return {
    id: w.id,
    type: w.type,
    state,
    // stateLabel is UI text shown in the 3D office tooltip (scene.ts):
    // translate it via the shared worker-state labels, not workerState()'s
    // Vietnamese fallback label.
    stateLabel: label(st.key),
    model: w.loaded_model ?? w.models[0] ?? '',
    running: w.running,
    slots: w.slots ?? w.concurrency,
  }
}

function officeTask(a: Activity, t: (key: DictKey, vars?: Record<string, string | number>) => string): OfficeState['tasks'][number] {
  const step = [a.step ? t('overview.activity-step', { step: a.step }) : null, a.action ? t('overview.activity-action', { action: a.action }) : null].filter(Boolean).join(' · ')
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
  const t = useT()
  const { language } = useLanguage()
  const workerStateLabel = useWorkerStateLabel()
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
            workers: data.workers.map((w) => officeWorker(w, workerStateLabel)),
            tasks: activity.map((a) => officeTask(a, t)),
            chatBusy: (chat.data?.busy ?? 0) > 0,
            gatewayUp: gatewayUp ?? data.gateway_up,
            dark,
          }
        : null,
    [data, activity, chat.data?.busy, gatewayUp, dark, language],
  )
  if (!data || !office) return <Loading error={error} />
  const quota = data.gpu_quota ?? {}
  const brandTotals = new Map<string, number>()
  for (const w of data.workers) brandTotals.set(w.brand, (brandTotals.get(w.brand) ?? 0) + w.spend.month_usd)
  const brandSpend = [...brandTotals.entries()].sort((a, b) => b[1] - a[1])
  return (
    <Page title={t('overview.title')}>
      <SimpleGrid cols={{ base: 2, md: 4 }}>
        <Stat label={t('overview.stat-gateway')} value={data.gateway_up ? t('overview.gateway-running') : t('overview.gateway-off')} color={data.gateway_up ? 'teal' : 'red'} />
        <Stat label={t('overview.stat-queued-running')} value={`${data.queued_jobs} / ${data.running_jobs}`} />
        <Stat label={t('overview.stat-spend-today')} value={usd(data.spend.today_usd)} />
        <Stat label={t('overview.stat-spend-month')} value={usd(data.spend.month_usd)} />
      </SimpleGrid>
      {brandSpend.length ? (
        <Card withBorder>
          <Text fw={600} mb={6}>
            {t('overview.spend-by-brand')}
          </Text>
          {brandSpend.map(([brand, total]) => (
            <Text key={brand} size="sm">
              <b>{brand}</b>: {usd(total)}
            </Text>
          ))}
        </Card>
      ) : null}
      {Object.keys(quota).length ? (
        <Card withBorder>
          <Text fw={600} mb={6}>
            {t('overview.gpu-quota')}
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
          <Text fw={600}>{t('overview.office')}</Text>
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
          <Suspense fallback={<Loading label={t('overview.office-loading')} />}>
            <Office3D state={office} onPick={onPick} height={small ? 360 : 500} />
          </Suspense>
        ) : (
          <WorkerCards workers={data.workers} />
        )}
        {!webgl ? (
          <Text size="xs" c="dimmed" px={6} pt={6}>
            {t('overview.office-no-webgl')}
          </Text>
        ) : null}
      </Card>
      <Title order={4}>{t('overview.doing')}</Title>
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
                  a.phase ? (PHASES[a.phase] ? t(PHASES[a.phase]) : a.phase) : null,
                  a.step ? t('overview.activity-step', { step: a.step }) : null,
                  a.action ? t('overview.activity-action', { action: a.action }) : null,
                  a.since ? t('overview.activity-since', { since: ago(a.since) }) : null,
                ]
                  .filter(Boolean)
                  .join(' · ') || (a.state === 'QUEUED' ? t('overview.activity-waiting') : t('overview.activity-preparing'))}
              </Text>
            </Card>
          ))}
        </Stack>
      ) : (
        <Empty>{t('overview.no-running-task')}</Empty>
      )}
      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Card withBorder>
          <Text fw={600} mb="xs">
            {t('overview.recent-tasks')}
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
            <Empty>{t('overview.no-task')}</Empty>
          )}
        </Card>
        <Card withBorder>
          <Text fw={600} mb="xs">
            {t('overview.recent-events')}
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
            <Empty>{t('overview.no-events')}</Empty>
          )}
        </Card>
      </SimpleGrid>
    </Page>
  )
}

function WorkerCards({ workers }: { workers: WorkerRow[] }) {
  const t = useT()
  const workerStateLabel = useWorkerStateLabel()
  if (!workers.length) return <Empty>{t('overview.no-workers')}</Empty>
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
                {workerStateLabel(st.key)}
              </Badge>
            </Group>
            <Text size="sm" c="dimmed" mt={4}>
              {w.type} · {w.loaded_model ? t('overview.worker-loading-model', { model: w.loaded_model }) : w.models.slice(0, 3).join(', ')}
            </Text>
            {w.alive && w.busy ? (
              <Text size="sm" mt={4}>
                {t('overview.worker-running', { running: w.running, slots: w.slots ?? w.concurrency })}
              </Text>
            ) : null}
            {w.start?.detail ? (
              <Text size="xs" c="yellow.9" mt={4}>
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
