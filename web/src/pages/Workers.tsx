import { Alert, Badge, Button, Card, Group, Modal, SimpleGrid, Stack, Switch, Text, Title } from '@mantine/core'
import { useState } from 'react'
import { get, post, type WorkerRow } from '../api'
import { Bars, Dot, Empty, KeyValue, Loading, Page, useWorkerStateLabel } from '../components/common'
import { useT } from '../i18n'
import KaggleScreen from '../components/KaggleScreen'
import { go } from '../router'
import { attempt, duration, notifyError, notifyOk, short, usd, useLoad, workerState } from '../util'
import WorkerForm from './WorkerForm'

export default function Workers({ parts }: { parts: string[] }) {
  const t = useT()
  const { data, error, reload } = useLoad(() => get<{ gateway_up: boolean; workers: WorkerRow[] }>('/api/workers'), [], 5000)
  const spend = useLoad(() => get<{ days: { day: string; usd: number; workers: Record<string, number> }[] }>('/api/spend?days=31'), [], 60000)
  const [screen, setScreen] = useState<string | null>(null)
  if (!data) return <Loading error={error} />
  if (parts[0] === 'new') return <WorkerForm key="new" mode="new" />
  const editing = parts[0] ? data.workers.find((w) => w.id === parts[0]) : undefined
  if (editing) return <WorkerForm key={editing.id} mode="edit" workerId={editing.id} />
  return (
    <Page title={t('workers.title')} actions={<Button onClick={() => go('/workers/new')}>{t('workers.create')}</Button>}>
      {!data.gateway_up ? <Alert color="red">{t('workers.gateway-down')}</Alert> : null}
      {data.workers.length ? (
        <SimpleGrid cols={{ base: 1, lg: 2 }}>
          {data.workers.map((w) => (
            <WorkerCard key={w.id} worker={w} onChanged={reload} onScreen={() => setScreen(w.id)} />
          ))}
        </SimpleGrid>
      ) : (
        <Empty>{t('workers.no-workers')}</Empty>
      )}
      <Card withBorder>
        <Title order={5} mb="xs">
          {t('workers.spend-31')}
        </Title>
        <Bars items={(spend.data?.days ?? []).map((d) => ({ label: d.day.slice(5), value: d.usd }))} format={(v) => usd(v)} color="teal" />
      </Card>
      <Modal opened={!!screen} onClose={() => setScreen(null)} title={t('workers.kaggle-browser')} size="90%" keepMounted={false}>
        {screen ? <KaggleScreen worker={screen} /> : null}
      </Modal>
    </Page>
  )
}

function WorkerCard({ worker: w, onChanged, onScreen }: { worker: WorkerRow; onChanged: () => void; onScreen: () => void }) {
  const t = useT()
  const workerStateLabel = useWorkerStateLabel()
  const [starting, setStarting] = useState(false)
  const st = workerState(w)
  const api = w.type === 'openai' || w.type === 'anthropic'
  const start = w.start ?? null
  async function turnOn() {
    setStarting(true)
    if (!api) notifyOk(t('workers.starting-note'))
    try {
      const res = await post(`/api/workers/${w.id}/start`, {})
      if (res?.state === 'none' || res?.error) notifyError(res.detail ?? res.error)
    } catch (e) {
      notifyError(e)
    } finally {
      setStarting(false)
      onChanged()
    }
  }
  return (
    <Card withBorder>
      <Group justify="space-between" wrap="nowrap">
        <Group gap={8} wrap="nowrap">
          <Dot color={st.color} />
          <Text fw={600}>{w.label || w.id}</Text>
          <Badge variant="outline" size="sm">
            {w.type}
          </Badge>
        </Group>
        <Badge color={st.color} variant="light">
          {workerStateLabel(st.key)}
        </Badge>
      </Group>
      <Text size="sm" c="dimmed">
        {w.brand} · {w.models[0] ?? ''}
      </Text>
      <Stack gap="xs" mt="sm">
        <KeyValue
          rows={[
            [t('workers.card.model'), w.loaded_model ? t('workers.card.loading-model', { model: w.loaded_model }) : w.models.join(', ')],
            [t('workers.card.jobs'), w.alive ? t('workers.card.jobs-running', { running: w.running, slots: w.slots ?? w.concurrency }) : '—'],
            [t('workers.card.last-signal'), w.seconds_since_heartbeat !== null && w.seconds_since_heartbeat !== undefined ? t('workers.card.last-signal-ago', { duration: duration(w.seconds_since_heartbeat) }) : '—'],
            [t('workers.card.spent'), t('workers.card.spent-value', { today: usd(w.spend.today_usd), month: usd(w.spend.month_usd) })],
            ...(w.version ? ([[t('workers.card.version'), w.version]] as [string, string][]) : []),
          ]}
        />
        {start ? (
          <Text size="sm" c={start.state === 'error' ? 'red' : 'yellow'}>
            {[start.state, start.detail, start.kaggle ? `Kaggle: ${start.kaggle}` : null, start.reason ? `(${start.reason})` : null]
              .filter(Boolean)
              .map((x) => short(x, 160))
              .join(' · ')}
          </Text>
        ) : null}
        <Group gap="lg">
          {api ? (
            <Switch
              label={t('workers.switch.on')}
              checked={w.switch.on}
              onChange={async (e) => {
                const on = e.currentTarget.checked
                await attempt(() => post(`/api/workers/${w.id}/${on ? 'start' : 'stop'}`, {}))
                onChanged()
              }}
            />
          ) : null}
          {!api ? (
            <Switch
              label={t('workers.switch.auto')}
              checked={w.switch.auto}
              onChange={async (e) => {
                const on = e.currentTarget.checked
                await attempt(() => post(`/api/workers/${w.id}/auto`, { on }))
                onChanged()
              }}
            />
          ) : null}
          {w.kaggle ? (
            <Switch
              label={t('workers.switch.auto-click')}
              checked={w.kaggle.auto_click}
              onChange={async (e) => {
                const on = e.currentTarget.checked
                if (on && !confirm(t('workers.auto-click-confirm'))) return
                await attempt(() => post(`/api/workers/${w.id}/auto-click`, { on }))
                onChanged()
              }}
            />
          ) : null}
        </Group>
        <Group gap="xs">
          <Button size="xs" variant="light" onClick={() => go(`/workers/${w.id}`)}>
            {t('workers.configure')}
          </Button>
          {!api && !w.alive ? (
            <Button size="xs" loading={starting} onClick={turnOn}>
              {t('workers.start-now')}
            </Button>
          ) : null}
          {!api && (w.alive || start) ? (
            <Button
              size="xs"
              variant="light"
              color="red"
              onClick={async () => {
                await attempt(() => post(`/api/workers/${w.id}/stop`, {}), t('workers.stop-requested'))
                onChanged()
              }}
            >
              {t('workers.stop')}
            </Button>
          ) : null}
          {w.kaggle ? (
            <Button
              size="xs"
              variant="light"
              onClick={async () => {
                onScreen()
                await attempt(() => post(`/api/workers/${w.id}/browser`, {}))
              }}
            >
              {t('workers.kaggle-browser-btn')}
            </Button>
          ) : null}
          {w.kaggle ? (
            <Button size="xs" variant="subtle" component="a" href={w.kaggle.url} target="_blank" rel="noreferrer">
              {t('workers.open-kaggle')}
            </Button>
          ) : null}
        </Group>
      </Stack>
    </Card>
  )
}
