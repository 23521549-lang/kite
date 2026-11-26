// The "Journal" tab folded into the Tasks page (moved from the deleted Journal.tsx).
// Journal data and endpoints are UNCHANGED — only the display moved.
// The client-side session/environment/task filter applies only to the Events tab,
// where records carry those fields; Summary and Audit show everything.
import { Badge, Button, Card, Group, SegmentedControl, Select, SimpleGrid, Stack, Table, Tabs, Text } from '@mantine/core'
import { useState } from 'react'
import { get, qs, type TaskRow } from '../api'
import { Bars, Empty, JsonView, Loading } from '../components/common'
import { fmtTime, num, short, usd, useLoad } from '../util'
import { useT, type DictKey } from '../i18n'

// The "unknown origin" filter value: records that carry no session/environment.
const UNKNOWN = '__unknown__'

function distinct(values: (string | null | undefined)[]): string[] {
  return [...new Set(values.filter((v): v is string => !!v))]
}

function originOptions(values: (string | null | undefined)[], unknownLabel: string): { value: string; label: string }[] {
  return [
    { value: UNKNOWN, label: unknownLabel },
    ...distinct(values).map((v) => ({ value: v, label: short(v, 24) })),
  ]
}

// A journal record's value in the fields that may carry session/environment/task.
// A record with none of those fields counts as "unknown origin".
function recordValue(r: Record<string, any>, fields: string[]): string | null {
  for (const f of fields) {
    const v = r[f]
    if (typeof v === 'string' && v) return v
    if (typeof v === 'number') return String(v)
  }
  return null
}

function recordMatches(r: Record<string, any>, fields: string[], filter: string | null): boolean {
  if (filter === null) return true
  const value = recordValue(r, fields)
  if (filter === UNKNOWN) return value === null
  return value === filter
}

export type JournalFilters = { session: string | null; env: string | null; task: string | null }

export default function TaskJournal({ tasks }: { tasks: TaskRow[] }) {
  const t = useT()
  const unknownLabel = t('tasks.origin.unknown')
  const [sessionFilter, setSessionFilter] = useState<string | null>(null)
  const [envFilter, setEnvFilter] = useState<string | null>(null)
  const [taskFilter, setTaskFilter] = useState<string | null>(null)
  const anyFilter = sessionFilter !== null || envFilter !== null || taskFilter !== null
  return (
    <Stack>
      <Group grow>
        <Select
          size="xs"
          placeholder={t('tasks.filter.env-all')}
          data={originOptions(tasks.map((t) => t.environment_id), unknownLabel)}
          value={envFilter}
          onChange={setEnvFilter}
          clearable
        />
        <Select
          size="xs"
          placeholder={t('tasks.filter.session-all')}
          data={originOptions(tasks.map((t) => t.chat_session_id), unknownLabel)}
          value={sessionFilter}
          onChange={setSessionFilter}
          clearable
        />
        <Select
          size="xs"
          placeholder={t('tasks.filter.task-all')}
          data={tasks.map((t) => ({ value: t.task_id, label: short(t.goal || t.task_id, 30) }))}
          value={taskFilter}
          onChange={setTaskFilter}
          clearable
        />
      </Group>
      {anyFilter ? (
        <Text size="xs" c="dimmed">
          {t('tasks.journal.filter-note')}
        </Text>
      ) : null}
      <Tabs defaultValue="summary" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="summary">{t('journal.tab.summary')}</Tabs.Tab>
          <Tabs.Tab value="events">{t('journal.tab.events')}</Tabs.Tab>
          <Tabs.Tab value="audit">{t('journal.tab.audit')}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="summary" pt="md">
          <Summary />
        </Tabs.Panel>
        <Tabs.Panel value="events" pt="md">
          <Events filters={{ session: sessionFilter, env: envFilter, task: taskFilter }} />
        </Tabs.Panel>
        <Tabs.Panel value="audit" pt="md">
          <Audit />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  )
}

const FILE_KEYS: Record<string, DictKey> = {
  gateway: 'journal.file.gateway',
  tasks: 'journal.file.tasks',
  actions: 'journal.file.actions',
  launches: 'journal.file.launches',
  spend: 'journal.file.spend',
  notes: 'journal.file.notes',
}

function Summary() {
  const t = useT()
  const METRICS = [
    { value: 'requests', label: t('journal.metric.requests') },
    { value: 'completion_tokens', label: t('journal.metric.completion-tokens') },
    { value: 'gpu_seconds', label: t('journal.metric.gpu-seconds') },
    { value: 'tasks', label: t('journal.metric.tasks') },
    { value: 'errors', label: t('journal.metric.errors') },
    { value: 'usd', label: t('journal.metric.usd') },
  ]
  const [days, setDays] = useState('7')
  const [metric, setMetric] = useState('requests')
  const summary = useLoad(() => get<Record<string, any>>(`/api/journal/summary${qs({ days })}`), [days])
  const daily = useLoad(() => get<{ days: Record<string, any>[] }>('/api/journal/daily?days=30'), [])
  const s = summary.data
  return (
    <Stack>
      <Card withBorder>
        <Group justify="space-between" mb="xs">
          <Text fw={600}>{t('journal.summary.last-30')}</Text>
          <SegmentedControl size="xs" data={METRICS} value={metric} onChange={setMetric} />
        </Group>
        {daily.data ? (
          <Bars
            items={daily.data.days.map((d) => ({ label: String(d.day).slice(5), value: Number(d[metric] ?? 0) }))}
            format={(v) => (metric === 'usd' ? usd(v) : num(Math.round(v * 100) / 100))}
          />
        ) : (
          <Loading error={daily.error} />
        )}
      </Card>
      <Group>
        <SegmentedControl
          size="xs"
          data={[
            { value: '1', label: t('journal.range.today') },
            { value: '7', label: t('journal.range.7d') },
            { value: '30', label: t('journal.range.30d') },
            { value: '0', label: t('journal.range.all') },
          ]}
          value={days}
          onChange={setDays}
        />
      </Group>
      {!s ? (
        <Loading error={summary.error} />
      ) : (
        <SimpleGrid cols={{ base: 1, lg: 2 }}>
          <Card withBorder>
            <Text fw={600} mb="xs">
              {t('journal.summary.tasks')}
            </Text>
            <Text size="sm">
              {t('journal.summary.runs', { runs: s.tasks.runs, applied: s.tasks.applied, committed: s.tasks.committed })}
            </Text>
            <Group gap={6} mt="xs">
              {Object.entries(s.tasks.reasons ?? {}).map(([k, v]) => (
                <Badge key={k} variant="light">
                  {k}: {String(v)}
                </Badge>
              ))}
            </Group>
            {Object.keys(s.tasks.by_model ?? {}).length ? (
              <Table mt="sm">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>{t('journal.summary.table.model')}</Table.Th>
                    <Table.Th>{t('journal.summary.table.runs')}</Table.Th>
                    <Table.Th>{t('journal.summary.table.verified')}</Table.Th>
                    <Table.Th>{t('journal.summary.table.avg-seconds')}</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {Object.entries(s.tasks.by_model as Record<string, any>).map(([model, v]) => (
                    <Table.Tr key={model}>
                      <Table.Td>{model}</Table.Td>
                      <Table.Td>{v.runs}</Table.Td>
                      <Table.Td>{v.verified}</Table.Td>
                      <Table.Td>{v.avg_seconds ?? '—'}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            ) : null}
            {(s.tasks.recent_failures ?? []).length ? (
              <Stack gap={2} mt="sm">
                <Text size="sm" fw={600}>
                  {t('journal.summary.recent-failures')}
                </Text>
                {(s.tasks.recent_failures as any[]).map((f, i) => (
                  <Text key={i} size="xs">
                    {f.task_id} · {f.model} · {f.reason}
                  </Text>
                ))}
              </Stack>
            ) : null}
          </Card>
          <Card withBorder>
            <Text fw={600} mb="xs">
              {t('journal.summary.gateway-requests', { count: s.gateway.requests })}
            </Text>
            {Object.keys(s.gateway.by_model ?? {}).length ? (
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>{t('journal.summary.table.model')}</Table.Th>
                    <Table.Th>{t('journal.metric.requests')}</Table.Th>
                    <Table.Th>{t('journal.summary.table.tokens')}</Table.Th>
                    <Table.Th>{t('journal.summary.table.tps')}</Table.Th>
                    <Table.Th>{t('journal.summary.table.queue')}</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {Object.entries(s.gateway.by_model as Record<string, any>).map(([model, v]) => (
                    <Table.Tr key={model}>
                      <Table.Td>{model}</Table.Td>
                      <Table.Td>{v.requests}</Table.Td>
                      <Table.Td>
                        {num(v.prompt_tokens)} / {num(v.completion_tokens)}
                      </Table.Td>
                      <Table.Td>{v.avg_gen_tps ?? '—'}</Table.Td>
                      <Table.Td>{v.avg_queue_s ?? '—'}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            ) : (
              <Empty>{t('journal.summary.no-requests')}</Empty>
            )}
            {Object.keys(s.gateway.errors ?? {}).length ? (
              <Stack gap={2} mt="sm">
                <Text size="sm" fw={600}>
                  {t('journal.summary.errors')}
                </Text>
                {Object.entries(s.gateway.errors as Record<string, number>).map(([k, v]) => (
                  <Text key={k} size="xs">
                    {v}× {k}
                  </Text>
                ))}
              </Stack>
            ) : null}
          </Card>
          <Card withBorder>
            <Text fw={600} mb="xs">
              {t('journal.summary.launches')}
            </Text>
            <JsonView data={s.autostart} maxHeight={220} />
          </Card>
          <Card withBorder>
            <Text fw={600} mb="xs">
              {t('journal.summary.spend')}
            </Text>
            <JsonView data={s.spend} maxHeight={220} />
          </Card>
        </SimpleGrid>
      )}
    </Stack>
  )
}

function Events({ filters }: { filters: JournalFilters }) {
  const t = useT()
  const FILES = Object.entries(FILE_KEYS).map(([value, key]) => ({ value, label: t(key) }))
  const [file, setFile] = useState('gateway')
  const [extra, setExtra] = useState<Record<string, any>[]>([])
  const { data, error } = useLoad(async () => {
    setExtra([])
    return get<{ records: Record<string, any>[] }>(`/api/journal/events${qs({ file, limit: 100 })}`)
  }, [file])
  const allRecords = [...(data?.records ?? []), ...extra]
  const records = allRecords.filter(
    (r) =>
      recordMatches(r, ['task_id', 'tag'], filters.task) &&
      recordMatches(r, ['chat_session_id', 'session_id'], filters.session) &&
      recordMatches(r, ['environment_id', 'env_id'], filters.env),
  )
  const last = allRecords[allRecords.length - 1]
  return (
    <Stack>
      <SegmentedControl data={FILES} value={file} onChange={setFile} />
      {!data ? (
        <Loading error={error} />
      ) : records.length ? (
        <Card withBorder p={0}>
          <Table striped highlightOnHover>
            <Table.Tbody>
              {records.map((r, i) => (
                <EventRow key={i} record={r} />
              ))}
            </Table.Tbody>
          </Table>
        </Card>
      ) : (
        <Empty>{t('journal.events.no-events')}</Empty>
      )}
      {allRecords.length >= 100 && last?.ts ? (
        <Group>
          <Button
            variant="light"
            size="xs"
            onClick={async () => {
              const more = await get<{ records: Record<string, any>[] }>(`/api/journal/events${qs({ file, limit: 100, before: last.ts })}`)
              setExtra((x) => [...x, ...more.records])
            }}
          >
            {t('journal.events.older')}
          </Button>
        </Group>
      ) : null}
    </Stack>
  )
}

function EventRow({ record: r }: { record: Record<string, any> }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const main = [r.tool, r.event, r.model, r.worker, r.tag ?? r.task_id, r.outcome, r.verdict, r.usd !== undefined ? usd(r.usd) : null]
    .filter((x) => x !== undefined && x !== null && x !== '')
    .join(' · ')
  return (
    <>
      <Table.Tr className="clickable" onClick={() => setOpen((o) => !o)}>
        <Table.Td w={120}>
          <Text size="xs" c="dimmed">
            {fmtTime(r.ts ?? r.at)}
          </Text>
        </Table.Td>
        <Table.Td>
          <Text size="sm" lineClamp={1}>
            {main || short(r, 140)}
          </Text>
        </Table.Td>
        <Table.Td w={60}>{r.ok === false || r.outcome === 'error' ? <Badge color="red" size="xs">{t('journal.events.error')}</Badge> : null}</Table.Td>
      </Table.Tr>
      {open ? (
        <Table.Tr>
          <Table.Td colSpan={3}>
            <JsonView data={r} maxHeight={300} />
          </Table.Td>
        </Table.Tr>
      ) : null}
    </>
  )
}

function Audit() {
  const t = useT()
  const { data, error } = useLoad(() => get<{ rows: Record<string, any>[] }>('/api/audit?limit=200'), [])
  if (!data) return <Loading error={error} />
  if (!data.rows.length) return <Empty>{t('journal.audit.no-rows')}</Empty>
  return (
    <Card withBorder p={0}>
      <Table striped>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{t('journal.audit.time')}</Table.Th>
            <Table.Th>{t('journal.audit.action')}</Table.Th>
            <Table.Th>{t('journal.audit.target')}</Table.Th>
            <Table.Th>{t('journal.audit.address')}</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {data.rows.map((r, i) => (
            <Table.Tr key={i}>
              <Table.Td>
                <Text size="xs">{fmtTime(r.ts)}</Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm">{r.action}</Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm" lineClamp={1}>
                  {r.target}
                </Text>
              </Table.Td>
              <Table.Td>
                <Text size="xs" c="dimmed">
                  {r.ip}
                </Text>
              </Table.Td>
              <Table.Td>{r.ok ? <Badge color="teal" size="xs">{t('journal.audit.ok')}</Badge> : <Badge color="red" size="xs">{t('journal.audit.error')}</Badge>}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Card>
  )
}
