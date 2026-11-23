import { Badge, Card, Group, SimpleGrid, Stack, Table, Text, Title } from '@mantine/core'
import { get, type Overview as OverviewData } from '../api'
import { Dot, Empty, Loading, Page, StateBadge } from '../components/common'
import { go } from '../router'
import { useKite } from '../store'
import { ago, short, usd, useLoad, workerState } from '../util'

const PHASES: Record<string, string> = { fast_path: 'đường nhanh', review: 'rà soát' }

export default function Overview() {
  const { data, error } = useLoad(() => get<OverviewData>('/api/overview'), [], 10000)
  const activity = useKite((s) => s.activity)
  const events = useKite((s) => s.events)
  if (!data) return <Loading error={error} />
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
      <Title order={4}>Worker</Title>
      {data.workers.length ? (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
          {data.workers.map((w) => {
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
      ) : (
        <Empty>Chưa có worker nào: thêm ở tab Provider.</Empty>
      )}
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
