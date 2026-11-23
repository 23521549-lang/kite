import { Badge, Button, Card, Group, SegmentedControl, SimpleGrid, Stack, Table, Tabs, Text } from '@mantine/core'
import { useState } from 'react'
import { get, qs } from '../api'
import { Bars, Empty, JsonView, Loading, Page } from '../components/common'
import { fmtTime, num, short, usd, useLoad } from '../util'

const FILES = [
  { value: 'gateway', label: 'Request model' },
  { value: 'tasks', label: 'Task' },
  { value: 'actions', label: 'Agent trưởng' },
  { value: 'launches', label: 'Bật worker' },
  { value: 'spend', label: 'Chi phí' },
  { value: 'notes', label: 'Nhận xét' },
]

const METRICS = [
  { value: 'requests', label: 'Request' },
  { value: 'completion_tokens', label: 'Token ra' },
  { value: 'gpu_seconds', label: 'Giây GPU' },
  { value: 'tasks', label: 'Task' },
  { value: 'errors', label: 'Lỗi' },
  { value: 'usd', label: 'USD' },
]

export default function Journal() {
  return (
    <Page title="Nhật ký">
      <Tabs defaultValue="summary" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="summary">Tổng hợp</Tabs.Tab>
          <Tabs.Tab value="events">Sự kiện</Tabs.Tab>
          <Tabs.Tab value="audit">Thao tác của bạn</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="summary" pt="md">
          <Summary />
        </Tabs.Panel>
        <Tabs.Panel value="events" pt="md">
          <Events />
        </Tabs.Panel>
        <Tabs.Panel value="audit" pt="md">
          <Audit />
        </Tabs.Panel>
      </Tabs>
    </Page>
  )
}

function Summary() {
  const [days, setDays] = useState('7')
  const [metric, setMetric] = useState('requests')
  const summary = useLoad(() => get<Record<string, any>>(`/api/journal/summary${qs({ days })}`), [days])
  const daily = useLoad(() => get<{ days: Record<string, any>[] }>('/api/journal/daily?days=30'), [])
  const s = summary.data
  return (
    <Stack>
      <Card withBorder>
        <Group justify="space-between" mb="xs">
          <Text fw={600}>30 ngày qua</Text>
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
            { value: '1', label: 'Hôm nay' },
            { value: '7', label: '7 ngày' },
            { value: '30', label: '30 ngày' },
            { value: '0', label: 'Tất cả' },
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
              Task
            </Text>
            <Text size="sm">
              {s.tasks.runs} lần chạy · đã áp {s.tasks.applied} · đã commit {s.tasks.committed}
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
                    <Table.Th>Model</Table.Th>
                    <Table.Th>Chạy</Table.Th>
                    <Table.Th>Qua test</Table.Th>
                    <Table.Th>TB giây</Table.Th>
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
                  Lỗi gần đây
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
              Request tới model ({s.gateway.requests})
            </Text>
            {Object.keys(s.gateway.by_model ?? {}).length ? (
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Model</Table.Th>
                    <Table.Th>Request</Table.Th>
                    <Table.Th>Token vào/ra</Table.Th>
                    <Table.Th>tok/s ra</Table.Th>
                    <Table.Th>Chờ (giây)</Table.Th>
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
              <Empty>Chưa có request.</Empty>
            )}
            {Object.keys(s.gateway.errors ?? {}).length ? (
              <Stack gap={2} mt="sm">
                <Text size="sm" fw={600}>
                  Lỗi
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
              Bật worker
            </Text>
            <JsonView data={s.autostart} maxHeight={220} />
          </Card>
          <Card withBorder>
            <Text fw={600} mb="xs">
              Chi phí
            </Text>
            <JsonView data={s.spend} maxHeight={220} />
          </Card>
        </SimpleGrid>
      )}
    </Stack>
  )
}

function Events() {
  const [file, setFile] = useState('gateway')
  const [extra, setExtra] = useState<Record<string, any>[]>([])
  const { data, error } = useLoad(async () => {
    setExtra([])
    return get<{ records: Record<string, any>[] }>(`/api/journal/events${qs({ file, limit: 100 })}`)
  }, [file])
  const records = [...(data?.records ?? []), ...extra]
  const last = records[records.length - 1]
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
        <Empty>Chưa có sự kiện.</Empty>
      )}
      {records.length >= 100 && last?.ts ? (
        <Group>
          <Button
            variant="light"
            size="xs"
            onClick={async () => {
              const more = await get<{ records: Record<string, any>[] }>(`/api/journal/events${qs({ file, limit: 100, before: last.ts })}`)
              setExtra((x) => [...x, ...more.records])
            }}
          >
            Xem cũ hơn
          </Button>
        </Group>
      ) : null}
    </Stack>
  )
}

function EventRow({ record: r }: { record: Record<string, any> }) {
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
        <Table.Td w={60}>{r.ok === false || r.outcome === 'error' ? <Badge color="red" size="xs">lỗi</Badge> : null}</Table.Td>
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
  const { data, error } = useLoad(() => get<{ rows: Record<string, any>[] }>('/api/audit?limit=200'), [])
  if (!data) return <Loading error={error} />
  if (!data.rows.length) return <Empty>Chưa có thao tác nào.</Empty>
  return (
    <Card withBorder p={0}>
      <Table striped>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Lúc</Table.Th>
            <Table.Th>Thao tác</Table.Th>
            <Table.Th>Đối tượng</Table.Th>
            <Table.Th>Địa chỉ</Table.Th>
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
              <Table.Td>{r.ok ? <Badge color="teal" size="xs">ok</Badge> : <Badge color="red" size="xs">lỗi</Badge>}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Card>
  )
}
