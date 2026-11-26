import { Group, SimpleGrid, Stack, Table, Text, Title, useComputedColorScheme } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { useMemo, useState } from 'react'
import { get, type Activity, type Overview as OverviewData, type WorkerRow } from '../api'
import { Empty, Loading, Page, StateBadge } from '../components/common'
import { useLanguage, useT, type TFunction } from '../i18n'
import type { OfficeState, Pick, WorkerState } from '../office/scene'
import { go } from '../router'
import { useKite } from '../store'
import { ago, short, usd, useLoad, workerState } from '../util'
import HubBlock from './overview/HubBlock'
import OfficePanel from './overview/OfficePanel'

function hasWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    return !!(canvas.getContext('webgl2') || canvas.getContext('webgl'))
  } catch {
    return false
  }
}

function officeWorker(w: WorkerRow): OfficeState['workers'][number] {
  const st = workerState(w)
  // Compare the raw state field, never the display label: the `workerState()`
  // label changes language after the Gói 0(b) migration (comparing labels breaks in EN mode).
  // This regex matches exactly the condition `workerState()` uses for the starting state.
  const startPhase = String(w.start?.state ?? w.start?.status ?? w.start?.phase ?? '')
  const state: WorkerState =
    w.start?.state === 'error'
      ? 'error'
      : w.alive && w.busy
        ? 'busy'
        : w.alive && !w.draining
          ? 'idle'
          : /start|queue|wait|boot|launch|running/i.test(startPhase) || w.draining
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

function officeTask(a: Activity, t: TFunction): OfficeState['tasks'][number] {
  const step = [a.step ? t('overview.task.step', { step: a.step }) : null, a.action ? t('overview.task.action', { action: a.action }) : null]
    .filter(Boolean)
    .join(' · ')
  return { id: a.task_id, goal: short(a.goal || a.task_id, 70), worker: a.worker, step: step || null }
}

function onPick(pick: Pick) {
  if (pick.kind === 'worker') go('/workers')
  else if (pick.kind === 'task') go(`/tasks/${encodeURIComponent(pick.id)}`)
  else if (pick.kind === 'lead') go('/chat')
  else if (pick.kind === 'gateway') go('/tasks')
  else go('/tasks')
}

export default function Overview() {
  const t = useT()
  const { language } = useLanguage()
  const { data, error } = useLoad(() => get<OverviewData>('/api/overview'), [], 10000)
  const activity = useKite((s) => s.activity)
  const events = useKite((s) => s.events)
  const gatewayUp = useKite((s) => s.gatewayUp)
  const chat = useLoad(() => get<{ busy?: number }>('/api/chat/info'), [], 15000)
  const dark = useComputedColorScheme('dark') === 'dark'
  const small = useMediaQuery('(max-width: 48em)')
  const [webgl] = useState(hasWebGL)
  const office = useMemo<OfficeState | null>(
    () =>
      data
        ? {
            workers: data.workers.map(officeWorker),
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
        <Stat label={t('overview.stat.gateway')} value={data.gateway_up ? t('overview.stat.gateway-on') : t('overview.stat.gateway-off')} color={data.gateway_up ? 'teal' : 'red'} />
        <Stat label={t('overview.stat.jobs')} value={`${data.queued_jobs} / ${data.running_jobs}`} onClick={() => go('/tasks')} />
        <Stat label={t('overview.stat.spend-today')} value={usd(data.spend.today_usd)} onClick={() => go('/workers')} />
        <Stat label={t('overview.stat.spend-month')} value={usd(data.spend.month_usd)} onClick={() => go('/workers')} />
      </SimpleGrid>
      {brandSpend.length ? (
        <HubBlock title={t('overview.brand-spend.title')} onClick={() => go('/workers')}>
          {brandSpend.map(([brand, total]) => (
            <Text key={brand} size="sm">
              <b>{brand}</b>: {usd(total)}
            </Text>
          ))}
        </HubBlock>
      ) : null}
      {Object.keys(quota).length ? (
        <HubBlock title={t('overview.quota.title')} onClick={() => go('/workers')}>
          {Object.entries(quota).map(([worker, q]) => (
            <Text key={worker} size="sm">
              <b>{worker}</b>: {q.detail}
            </Text>
          ))}
        </HubBlock>
      ) : null}
      <OfficePanel office={office} workers={data.workers} webgl={webgl} small={small} onPick={onPick} />
      <Title order={4}>{t('overview.running.title')}</Title>
      {activity.length ? (
        <Stack gap="xs">
          {activity.map((a) => (
            <HubBlock key={a.task_id} title={a.goal || a.task_id} onClick={() => go(`/tasks/${encodeURIComponent(a.task_id)}`)}>
              <Group justify="space-between" wrap="nowrap" align="flex-start">
                <Text size="sm" c="dimmed" style={{ flex: 1, minWidth: 0 }}>
                  {[
                    a.worker ? t('overview.task.worker', { worker: a.worker }) : null,
                    a.phase
                      ? a.phase === 'fast_path'
                        ? t('overview.phase.fast-path')
                        : a.phase === 'review'
                          ? t('overview.phase.review')
                          : a.phase
                      : null,
                    a.step ? t('overview.task.step', { step: a.step }) : null,
                    a.action ? t('overview.task.action', { action: a.action }) : null,
                    a.since ? t('overview.task.since', { time: ago(a.since) }) : null,
                  ]
                    .filter(Boolean)
                    .join(' · ') || (a.state === 'QUEUED' ? t('overview.running.queued') : t('overview.running.preparing'))}
                </Text>
                <StateBadge state={a.state} />
              </Group>
            </HubBlock>
          ))}
        </Stack>
      ) : (
        <Empty>{t('overview.running.empty')}</Empty>
      )}
      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <HubBlock title={t('overview.recent-tasks.title')}>
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
            <Empty>{t('overview.recent-tasks.empty')}</Empty>
          )}
        </HubBlock>
        <HubBlock title={t('overview.recent-events.title')}>
          {events.length ? (
            <Stack gap={4}>
              {events.slice(0, 14).map((e, i) => {
                const taskId = typeof e.record.task_id === 'string' && e.record.task_id ? e.record.task_id : null
                return (
                  <Text
                    key={i}
                    size="xs"
                    className="mono clickable"
                    lineClamp={1}
                    onClick={() => go(taskId ? `/tasks/${encodeURIComponent(taskId)}` : '/tasks')}
                  >
                    <Text span c="dimmed">
                      {new Date(e.at).toLocaleTimeString('vi-VN', { hour12: false })} {e.file}
                    </Text>{' '}
                    {short(describe(e.record), 160)}
                  </Text>
                )
              })}
            </Stack>
          ) : (
            <Empty>{t('overview.recent-events.empty')}</Empty>
          )}
        </HubBlock>
      </SimpleGrid>
    </Page>
  )
}

function describe(record: Record<string, any>): string {
  const bits = [record.tool, record.event, record.worker, record.model, record.task_id ?? record.tag, record.outcome, record.detail]
  const text = bits.filter((b) => b !== undefined && b !== null && b !== '').join(' · ')
  return text || JSON.stringify(record)
}

function Stat({ label, value, color, onClick }: { label: string; value: string; color?: string; onClick?: () => void }) {
  return (
    <HubBlock title={label} onClick={onClick}>
      <Text fw={700} size="lg" c={color}>
        {value}
      </Text>
    </HubBlock>
  )
}
