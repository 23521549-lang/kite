// Threshold alerts the owner sets himself (Gói 8, worker (c)) — imported by Monitor.tsx.
// Fetches `GET /api/monitor/alerts` itself; workers come from `GET /api/monitor` (label || id).
// No default rules: the owner creates them. Lazy evaluation: a rule only fires when its
// value crosses the threshold from below (see `web/monitor_alerts.py`); the "Check now"
// button evaluates immediately.
import { Alert, Badge, Button, Card, Group, NumberInput, Select, Stack, Table, Text, Title } from '@mantine/core'
import { useState } from 'react'
import { del, get, post } from '../api'
import { Empty, Loading } from '../components/common'
import { useT, type DictKey } from '../i18n'
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
const METRIC_KEYS: Record<Metric, DictKey> = {
  cost_day_pct: 'monitor.alerts.metric.cost-day-pct',
  cost_month_pct: 'monitor.alerts.metric.cost-month-pct',
  kaggle_quota_pct: 'monitor.alerts.metric.kaggle-quota-pct',
  runpod_month_usd: 'monitor.alerts.metric.runpod-month-usd',
}

function formatValue(metric: string, value: number): string {
  return metric === 'runpod_month_usd' ? usd(value) : `${value.toFixed(1)}%`
}

export default function MonitorAlerts() {
  const t = useT()
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
  const metricLabel = (m: string) => (METRIC_KEYS[m as Metric] ? t(METRIC_KEYS[m as Metric]) : m)

  async function save() {
    if (!workerId || threshold === '') return
    const created = await attempt(
      () => post('/api/monitor/alerts', { worker_id: workerId, metric, threshold }),
      t('monitor.alerts.saved'),
    )
    if (created) {
      setThreshold('')
      alerts.reload()
    }
  }

  async function remove(id: string) {
    const done = await attempt(() => del(`/api/monitor/alerts/${id}`), t('monitor.alerts.deleted'))
    if (done) alerts.reload()
  }

  async function checkNow() {
    setChecking(true)
    try {
      const res = await post<{ fired: AlertHistory[]; active_count: number }>('/api/monitor/alerts/check', {})
      notifyOk(
        res.fired.length > 0
          ? t('monitor.alerts.checked-fired', { n: res.fired.length })
          : t('monitor.alerts.checked-none'),
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
      <Title order={4}>{t('monitor.alerts.title')}</Title>
      {!data.ntfy_configured ? <Alert color="yellow">{t('monitor.alerts.ntfy-missing')}</Alert> : null}
      <Text size="sm" c="dimmed">
        {t('monitor.alerts.g4-note')}
      </Text>

      <Card withBorder>
        <Title order={5} mb="sm">
          {t('monitor.alerts.add-title')}
        </Title>
        <Group align="end" gap="md">
          <Select
            label={t('monitor.alerts.worker')}
            placeholder={workerOptions.length ? undefined : t('monitor.alerts.workers-empty')}
            data={workerOptions}
            value={workerId}
            onChange={setWorkerId}
            w={220}
          />
          <Select
            label={t('monitor.alerts.metric')}
            data={METRICS.map((m) => ({ value: m, label: metricLabel(m) }))}
            value={metric}
            onChange={(v) => v && setMetric(v as Metric)}
            w={260}
          />
          <NumberInput
            label={t('monitor.alerts.threshold')}
            description={t('monitor.alerts.threshold-hint')}
            value={threshold}
            onChange={(v) => setThreshold(v === '' ? '' : Number(v))}
            min={0}
            decimalScale={2}
            allowNegative={false}
            w={180}
          />
          <Button onClick={save} disabled={!workerId || threshold === ''}>
            {t('monitor.alerts.save')}
          </Button>
        </Group>
      </Card>

      <Card withBorder>
        <Title order={5} mb="sm">
          {t('monitor.alerts.rules-title')}
        </Title>
        {data.rules.length ? (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t('monitor.alerts.worker')}</Table.Th>
                <Table.Th>{t('monitor.alerts.metric')}</Table.Th>
                <Table.Th>{t('monitor.alerts.threshold')}</Table.Th>
                <Table.Th>{t('monitor.alerts.enabled')}</Table.Th>
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
                    {r.enabled ? <Badge color="teal">{t('monitor.alerts.enabled')}</Badge> : null}
                  </Table.Td>
                  <Table.Td>
                    <Button variant="subtle" color="red" size="xs" onClick={() => remove(r.id)}>
                      {t('monitor.alerts.delete')}
                    </Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : (
          <Empty>{t('monitor.alerts.rules-empty')}</Empty>
        )}
      </Card>

      <Card withBorder>
        <Group justify="space-between" mb="sm">
          <Title order={5}>{t('monitor.alerts.history-title')}</Title>
          <Button onClick={checkNow} loading={checking}>
            {checking ? t('monitor.alerts.checking') : t('monitor.alerts.check-now')}
          </Button>
        </Group>
        {data.history.length ? (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t('monitor.alerts.history-time')}</Table.Th>
                <Table.Th>{t('monitor.alerts.worker')}</Table.Th>
                <Table.Th>{t('monitor.alerts.metric')}</Table.Th>
                <Table.Th>{t('monitor.alerts.history-value-col')}</Table.Th>
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
                    {t('monitor.alerts.history-value', {
                      value: formatValue(h.metric, h.value),
                      threshold: formatValue(h.metric, h.threshold),
                    })}
                  </Table.Td>
                  <Table.Td>
                    {h.notified ? (
                      <Badge color="teal">{t('monitor.alerts.notified-yes')}</Badge>
                    ) : (
                      <Badge color="gray">{t('monitor.alerts.notified-no')}</Badge>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : (
          <Empty>{t('monitor.alerts.history-empty')}</Empty>
        )}
      </Card>
    </Stack>
  )
}
