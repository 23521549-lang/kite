// Cảnh báo theo ngưỡng do chủ tự đặt (Gói 8, worker (c)) — được Monitor.tsx import.
// Tự fetch `GET /api/monitor/alerts`; worker lấy từ `GET /api/monitor` (label || id).
// Không có rule mặc định: chủ tự tạo. Đánh giá lười: rule chỉ bắn khi giá trị vượt
// ngưỡng từ dưới lên (xem `web/monitor_alerts.py`); nút "Kiểm tra ngay" đánh giá ngay.
import { Alert, Badge, Button, Card, Group, NumberInput, Select, Stack, Table, Text, Title } from '@mantine/core'
import { useState } from 'react'
import { del, get, post } from '../api'
import { Empty, Loading } from '../components/common'
import { fmt, useMonitorStrings, type MonitorKey } from '../i18n/monitor'
import { attempt, fmtTime, notifyError, notifyOk, useLoad, usd } from '../util'

type AlertRule = {
  id: string
  worker_id: string
  metric: string
  threshold: number
  enabled: boolean
  created_at: string
}

type AlertHistory = {
  at: string
  worker_id: string
  metric: string
  value: number
  threshold: number
  message: string
  notified: boolean
}

type AlertsData = { rules: AlertRule[]; history: AlertHistory[]; ntfy_configured: boolean }
type MonitorWorker = { id: string; label?: string | null }

const METRICS = ['cost_day_pct', 'cost_month_pct', 'kaggle_quota_pct', 'runpod_month_usd'] as const
type Metric = (typeof METRICS)[number]

// i18n keys are kebab-case; metric values (backend) use underscores.
const METRIC_KEYS: Record<Metric, MonitorKey> = {
  cost_day_pct: 'monitor.alerts.metric.cost-day-pct',
  cost_month_pct: 'monitor.alerts.metric.cost-month-pct',
  kaggle_quota_pct: 'monitor.alerts.metric.kaggle-quota-pct',
  runpod_month_usd: 'monitor.alerts.metric.runpod-month-usd',
}

function formatValue(metric: string, value: number): string {
  return metric === 'runpod_month_usd' ? usd(value) : `${value.toFixed(1)}%`
}

export default function MonitorAlerts() {
  const s = useMonitorStrings()
  const alerts = useLoad(() => get<AlertsData>('/api/monitor/alerts'), [])
  const workers = useLoad(() => get<{ workers: MonitorWorker[] }>('/api/monitor'), [])
  const [workerId, setWorkerId] = useState<string | null>(null)
  const [metric, setMetric] = useState<Metric>('cost_day_pct')
  const [threshold, setThreshold] = useState<number | ''>('')
  const [checking, setChecking] = useState(false)

  const data = alerts.data
  if (!data) return <Loading error={alerts.error} />
  const workerOptions = (workers.data?.workers ?? []).map((w) => ({ value: w.id, label: w.label || w.id }))
  const workerLabel = (id: string) => workers.data?.workers.find((w) => w.id === id)?.label || id
  const metricLabel = (m: string) => s[METRIC_KEYS[m as Metric]] ?? m

  async function save() {
    if (!workerId || threshold === '') return
    const created = await attempt(
      () => post('/api/monitor/alerts', { worker_id: workerId, metric, threshold }),
      s['monitor.alerts.saved'],
    )
    if (created) {
      setThreshold('')
      alerts.reload()
    }
  }

  async function remove(id: string) {
    const done = await attempt(() => del(`/api/monitor/alerts/${id}`), s['monitor.alerts.deleted'])
    if (done) alerts.reload()
  }

  async function checkNow() {
    setChecking(true)
    try {
      const res = await post<{ fired: AlertHistory[]; active_count: number }>('/api/monitor/alerts/check', {})
      notifyOk(
        res.fired.length > 0
          ? fmt(s['monitor.alerts.checked-fired'], { n: res.fired.length })
          : s['monitor.alerts.checked-none'],
      )
      alerts.reload()
    } catch (e) {
      notifyError(e)
    } finally {
      setChecking(false)
    }
  }

  return (
    <Stack gap="md">
      <Title order={4}>{s['monitor.alerts.title']}</Title>
      {!data.ntfy_configured ? <Alert color="yellow">{s['monitor.alerts.ntfy-missing']}</Alert> : null}
      <Text size="sm" c="dimmed">
        {s['monitor.alerts.g4-note']}
      </Text>

      <Card withBorder>
        <Title order={5} mb="sm">
          {s['monitor.alerts.add-title']}
        </Title>
        <Group align="end" gap="md">
          <Select
            label={s['monitor.alerts.worker']}
            placeholder={workerOptions.length ? undefined : s['monitor.alerts.workers-empty']}
            data={workerOptions}
            value={workerId}
            onChange={setWorkerId}
            w={220}
          />
          <Select
            label={s['monitor.alerts.metric']}
            data={METRICS.map((m) => ({ value: m, label: metricLabel(m) }))}
            value={metric}
            onChange={(v) => v && setMetric(v as Metric)}
            w={260}
          />
          <NumberInput
            label={s['monitor.alerts.threshold']}
            description={s['monitor.alerts.threshold-hint']}
            value={threshold}
            onChange={(v) => setThreshold(v === '' ? '' : Number(v))}
            min={0}
            decimalScale={2}
            allowNegative={false}
            w={180}
          />
          <Button onClick={save} disabled={!workerId || threshold === ''}>
            {s['monitor.alerts.save']}
          </Button>
        </Group>
      </Card>

      <Card withBorder>
        <Title order={5} mb="sm">
          {s['monitor.alerts.rules-title']}
        </Title>
        {data.rules.length ? (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{s['monitor.alerts.worker']}</Table.Th>
                <Table.Th>{s['monitor.alerts.metric']}</Table.Th>
                <Table.Th>{s['monitor.alerts.threshold']}</Table.Th>
                <Table.Th>{s['monitor.alerts.enabled']}</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.rules.map((r) => (
                <Table.Tr key={r.id}>
                  <Table.Td>{workerLabel(r.worker_id)}</Table.Td>
                  <Table.Td>{metricLabel(r.metric)}</Table.Td>
                  <Table.Td>{formatValue(r.metric, r.threshold)}</Table.Td>
                  <Table.Td>
                    {r.enabled ? <Badge color="teal">{s['monitor.alerts.enabled']}</Badge> : null}
                  </Table.Td>
                  <Table.Td>
                    <Button variant="subtle" color="red" size="xs" onClick={() => remove(r.id)}>
                      {s['monitor.alerts.delete']}
                    </Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : (
          <Empty>{s['monitor.alerts.rules-empty']}</Empty>
        )}
      </Card>

      <Card withBorder>
        <Group justify="space-between" mb="sm">
          <Title order={5}>{s['monitor.alerts.history-title']}</Title>
          <Button onClick={checkNow} loading={checking}>
            {checking ? s['monitor.alerts.checking'] : s['monitor.alerts.check-now']}
          </Button>
        </Group>
        {data.history.length ? (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{s['monitor.alerts.history-time']}</Table.Th>
                <Table.Th>{s['monitor.alerts.worker']}</Table.Th>
                <Table.Th>{s['monitor.alerts.metric']}</Table.Th>
                <Table.Th>{s['monitor.alerts.history-value-col']}</Table.Th>
                <Table.Th>ntfy</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {[...data.history].reverse().map((h, i) => (
                <Table.Tr key={`${h.at}-${i}`}>
                  <Table.Td>{fmtTime(h.at)}</Table.Td>
                  <Table.Td>{workerLabel(h.worker_id)}</Table.Td>
                  <Table.Td>{metricLabel(h.metric)}</Table.Td>
                  <Table.Td>
                    {fmt(s['monitor.alerts.history-value'], {
                      value: formatValue(h.metric, h.value),
                      threshold: formatValue(h.metric, h.threshold),
                    })}
                  </Table.Td>
                  <Table.Td>
                    {h.notified ? (
                      <Badge color="teal">{s['monitor.alerts.notified-yes']}</Badge>
                    ) : (
                      <Badge color="gray">{s['monitor.alerts.notified-no']}</Badge>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : (
          <Empty>{s['monitor.alerts.history-empty']}</Empty>
        )}
      </Card>
    </Stack>
  )
}
