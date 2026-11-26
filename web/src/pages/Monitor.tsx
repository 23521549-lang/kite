import { Alert, Badge, Button, Card, Code, Group, Progress, SimpleGrid, Stack, Text, Title } from '@mantine/core'
import { get } from '../api'
import { Empty, KeyValue, Loading, Page } from '../components/common'
import { duration, usd, useLoad } from '../util'
import { useT } from '../i18n'
import MonitorAlerts from './MonitorAlerts'

// Shape of `GET /api/monitor` — per the Gói 8 contract in HOP-DONG-GIAO-TIEP.md.
type MonitorWorker = {
  id: string
  label: string | null
  type: string
  brand: string
  enabled: boolean
  alive: boolean
  spend: { today_usd: number; month_usd: number }
  relay: {
    price: { input: number; output: number }
    caps: { daily_usd: number | null; monthly_usd: number | null }
    cap_pct_day: number | null
    cap_pct_month: number | null
  } | null
  kaggle: {
    quota_gpu_seconds: number
    used_gpu_seconds: number
    remaining_gpu_seconds: number
    pct_used: number | null
    used_source: string
  } | null
  runpod: { price_hour: number; running_hours_month: number; cost_month_usd: number; running: boolean } | null
}

type MonitorData = {
  workers: MonitorWorker[]
  gpu_quota: unknown
  totals: { today_usd: number; month_usd: number }
  alerts_active: number
}

export default function Monitor() {
  const t = useT()
  const monitor = useLoad(() => get<MonitorData>('/api/monitor'), [], 30000)
  const d = monitor.data
  return (
    <Page title={t('monitor.title')}>
      <Stack gap="md">
        <Doctor />
        <Card withBorder>
          <Title order={5} mb="xs">
            {t('monitor.cost.title')}
          </Title>
          {!d ? (
            <Loading error={monitor.error} />
          ) : (
            <Stack gap="md">
              <KeyValue
                rows={[
                  [t('monitor.cost.total-today'), usd(d.totals.today_usd)],
                  [t('monitor.cost.total-month'), usd(d.totals.month_usd)],
                ]}
              />
              {d.workers.length ? (
                <SimpleGrid cols={{ base: 1, lg: 2 }}>
                  {d.workers.map((w) => (
                    <WorkerCostCard key={w.id} worker={w} gpuQuota={d.gpu_quota} />
                  ))}
                </SimpleGrid>
              ) : (
                <Empty>{t('monitor.cost.empty')}</Empty>
              )}
            </Stack>
          )}
        </Card>
        <MonitorAlerts />
      </Stack>
    </Page>
  )
}

/** Component `Doctor()` chuyển nguyên vẹn từ `Settings.tsx`: cùng endpoint, badge, danh sách. */
function Doctor() {
  const t = useT()
  const doctor = useLoad(() => get<Record<string, any>>('/api/system/doctor'), [])
  const d = doctor.data
  return (
    <Card withBorder>
      <Group justify="space-between" mb="xs">
        <Title order={5}>{t('monitor.health.title')}</Title>
        <Button size="xs" variant="subtle" onClick={doctor.reload}>
          {t('monitor.health.recheck')}
        </Button>
      </Group>
      {!d ? (
        <Loading error={doctor.error} label={t('monitor.health.checking')} />
      ) : (
        <Stack gap="xs">
          <Badge color={d.fail ? 'red' : d.warn ? 'yellow' : 'teal'} variant="light">
            {d.fail ? t('monitor.health.errors', { n: d.fail }) : d.warn ? t('monitor.health.warnings', { n: d.warn }) : t('monitor.health.ok')}
          </Badge>
          {(d.problems ?? []).map((p: any, i: number) => (
            <Alert key={i} color={p.status === 'lỗi' ? 'red' : 'yellow'} title={p.check} py={6}> {/* i18n-ignore: p.status là giá trị trạng thái do backend /api/doctor trả về, không phải chuỗi UI — giữ nguyên phép so sánh. */}
              <Text size="sm">{p.detail}</Text>
              {p.fix ? (
                <Text size="xs" mt={4}>
                  {t('monitor.health.fix')} <Code>{p.fix}</Code>
                </Text>
              ) : null}
            </Alert>
          ))}
          {d.gpu_quota ? (
            <Text size="sm">{t('monitor.health.quota-gpu', { value: typeof d.gpu_quota === 'string' ? d.gpu_quota : JSON.stringify(d.gpu_quota) })}</Text>
          ) : null}
          <Text size="xs" c="dimmed">
            {t('monitor.health.ok-list', { list: (d.ok ?? []).join(', ') })}
          </Text>
        </Stack>
      )}
    </Card>
  )
}

function WorkerCostCard({ worker: w, gpuQuota }: { worker: MonitorWorker; gpuQuota: unknown }) {
  const t = useT()
  const name = w.label || w.id
  const quotaText = typeof gpuQuota === 'string' ? gpuQuota : gpuQuota == null ? null : JSON.stringify(gpuQuota)
  return (
    <Card withBorder>
      <Group justify="space-between" wrap="nowrap">
        <Group gap={8} wrap="nowrap">
          <Text fw={600}>{name}</Text>
          <Badge variant="outline" size="sm">
            {w.type}
          </Badge>
        </Group>
        <Text size="sm" c="dimmed">
          {w.brand}
        </Text>
      </Group>
      <Stack gap="xs" mt="sm">
        {w.relay ? (
          <Stack gap="xs">
            <Text size="sm" fw={600}>
              {t('monitor.cost.relay')}
            </Text>
            <KeyValue
              rows={[
                [t('monitor.cost.today'), usd(w.spend.today_usd)],
                [t('monitor.cost.month'), usd(w.spend.month_usd)],
              ]}
            />
            <CapProgress label={t('monitor.cost.cap-day')} pct={w.relay.cap_pct_day} spent={w.spend.today_usd} cap={w.relay.caps.daily_usd} />
            <CapProgress label={t('monitor.cost.cap-month')} pct={w.relay.cap_pct_month} spent={w.spend.month_usd} cap={w.relay.caps.monthly_usd} />
          </Stack>
        ) : null}
        {w.kaggle ? (
          <Stack gap="xs">
            <Text size="sm" fw={600}>
              {t('monitor.cost.kaggle')}
            </Text>
            <KeyValue
              rows={[
                [
                  t('monitor.cost.quota-used'),
                  w.kaggle.used_source === 'account'
                    ? `${duration(w.kaggle.used_gpu_seconds)} (${t('monitor.cost.kaggle-account')})`
                    : duration(w.kaggle.used_gpu_seconds),
                ],
                [t('monitor.cost.quota-remaining'), duration(w.kaggle.remaining_gpu_seconds)],
                [t('monitor.cost.quota-pct'), w.kaggle.pct_used == null ? '—' : `${w.kaggle.pct_used}%`],
                ...(quotaText != null ? ([[t('monitor.cost.quota-account'), quotaText]] as [string, string][]) : []),
              ]}
            />
          </Stack>
        ) : null}
        {w.runpod ? (
          <Stack gap="xs">
            <Text size="sm" fw={600}>
              {t('monitor.cost.runpod')}
            </Text>
            <KeyValue
              rows={[
                [t('monitor.cost.runpod-hours'), duration(w.runpod.running_hours_month * 3600)],
                [t('monitor.cost.runpod-cost'), usd(w.runpod.cost_month_usd)],
              ]}
            />
            <Badge color={w.runpod.running ? 'teal' : 'gray'} variant="light">
              {w.runpod.running ? t('monitor.cost.running') : t('monitor.cost.stopped')}
            </Badge>
          </Stack>
        ) : null}
        {!w.relay && !w.kaggle && !w.runpod ? (
          <KeyValue
            rows={[
              [t('monitor.cost.today'), usd(w.spend.today_usd)],
              [t('monitor.cost.month'), usd(w.spend.month_usd)],
            ]}
          />
        ) : null}
      </Stack>
    </Card>
  )
}

/** Thanh % so với trần; `cap` null nghĩa là không đặt trần thì chỉ hiện chữ, không có thanh. */
function CapProgress({ label, pct, spent, cap }: { label: string; pct: number | null; spent: number; cap: number | null }) {
  const t = useT()
  if (cap == null) {
    return (
      <Text size="sm" c="dimmed">
        {label}: {t('monitor.cost.no-cap')}
      </Text>
    )
  }
  return (
    <Stack gap={4}>
      <Text size="sm">
        {label}: {pct == null ? '—' : `${pct}%`} · {usd(spent)} / {usd(cap)}
      </Text>
      <Progress value={pct == null ? 0 : Math.min(100, Math.max(0, pct))} color={pct != null && pct >= 100 ? 'red' : pct != null && pct >= 80 ? 'yellow' : 'teal'} />
    </Stack>
  )
}
