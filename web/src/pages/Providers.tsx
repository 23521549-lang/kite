// Provider tab: add a worker of a provider type (Kaggle, OpenAI-compatible, Anthropic, RunPod), test the
// connection, edit or remove the ones config.yaml has. Keys go to 0600 files on the server and never
// come back to the browser.
import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  Modal,
  NumberInput,
  PasswordInput,
  Select,
  SimpleGrid,
  Stack,
  TagsInput,
  Text,
  TextInput,
  Title,
} from '@mantine/core'
import { IconPlus } from '@tabler/icons-react'
import { useState, type ReactNode } from 'react'
import { del, get, patch, post, type ProviderType, type WorkerRow } from '../api'
import { ConfirmButton, Empty, Loading, Page } from '../components/common'
import { useT, type DictKey } from '../i18n'
import { attempt, notifyError, usd, useLoad } from '../util'

type FieldDef = { kind: 'text' | 'secret' | 'number' | 'models' | 'via'; labelKey: DictKey | null; descKey?: DictKey; placeholder?: string; required?: boolean }

const FIELDS: Record<string, FieldDef> = {
  id: { labelKey: 'providers.field.id', kind: 'text', required: true, placeholder: 'kaggle-han, openrouter…', descKey: 'providers.field.id-desc' },
  owner: { labelKey: 'providers.field.owner', kind: 'text', placeholder: 'self' },
  models: { labelKey: 'providers.field.models', kind: 'models', required: true, descKey: 'providers.field.models-desc' },
  kernel: { labelKey: 'providers.field.kernel', kind: 'text', placeholder: '<user>/<slug>', descKey: 'providers.field.kernel-desc' },
  kaggle_api_token: { labelKey: 'providers.field.kaggle-api-token', kind: 'secret', descKey: 'providers.field.kaggle-api-token-desc' },
  via: { labelKey: 'providers.field.via', kind: 'via' },
  idle_minutes: { labelKey: 'providers.field.idle-minutes', kind: 'number', placeholder: '30' },
  base_url: { labelKey: 'providers.field.base-url', kind: 'text', placeholder: 'https://openrouter.ai/api/v1', required: true },
  api_key: { labelKey: 'providers.field.api-key', kind: 'secret' },
  runpod_api_key: { labelKey: 'providers.field.runpod-api-key', kind: 'secret', required: true, descKey: 'providers.field.runpod-api-key-desc' },
  pod_id: { labelKey: 'providers.field.pod-id', kind: 'text', required: true },
  port: { labelKey: 'providers.field.port', kind: 'number', placeholder: '8000' },
  price_in: { labelKey: 'providers.field.price-in', kind: 'number', placeholder: '0' },
  price_out: { labelKey: 'providers.field.price-out', kind: 'number', placeholder: '0' },
  price_hour: { labelKey: 'providers.field.price-hour', kind: 'number', descKey: 'providers.field.price-hour-desc' },
  daily_cap: { labelKey: 'providers.field.daily-cap', kind: 'number' },
  monthly_cap: { labelKey: 'providers.field.monthly-cap', kind: 'number' },
  concurrency: { labelKey: 'providers.field.concurrency', kind: 'number', placeholder: '2' },
}

type Values = Record<string, any>

type ProviderPreset = {
  brand: string
  label: string
  type: string
  base_url: string
  models: { id: string; price_in: number; price_out: number; context: number }[]
  note: string
  catalog_date: string
}

function FieldInput({ name, value, onChange, editing }: { name: string; value: any; onChange: (v: any) => void; editing?: boolean }) {
  const t = useT()
  const f: FieldDef = FIELDS[name] ?? { labelKey: null, kind: 'text' }
  const common = { label: f.labelKey ? t(f.labelKey) : name, description: f.descKey ? t(f.descKey) : undefined, placeholder: f.placeholder, required: f.required && !editing }
  switch (f.kind) {
    case 'secret':
      return (
        <PasswordInput
          {...common}
          placeholder={editing ? t('providers.field.keep-key') : f.placeholder}
          value={value ?? ''}
          onChange={(e) => onChange(e.currentTarget.value)}
          autoComplete="off"
        />
      )
    case 'number':
      return <NumberInput {...common} value={value ?? ''} onChange={(v) => onChange(v === '' ? null : v)} min={0} decimalScale={4} />
    case 'models':
      return <TagsInput {...common} value={value ?? []} onChange={onChange} clearable />
    case 'via':
      return (
        <Select
          {...common}
          data={[
            { value: 'api', label: t('providers.field.via-api') },
            { value: 'browser', label: t('providers.field.via-browser') },
          ]}
          value={value ?? 'api'}
          onChange={onChange}
          allowDeselect={false}
        />
      )
    default:
      return <TextInput {...common} value={value ?? ''} onChange={(e) => onChange(e.currentTarget.value)} />
  }
}

export default function Providers() {
  const t = useT()
  const types = useLoad(() => get<{ types: ProviderType[] }>('/api/provider-types'), [])
  const workers = useLoad(() => get<{ workers: WorkerRow[] }>('/api/workers'), [])
  const [adding, setAdding] = useState<ProviderType | null>(null)
  const [editing, setEditing] = useState<WorkerRow | null>(null)
  const [restartNote, setRestartNote] = useState(false)
  if (!types.data || !workers.data) return <Loading error={types.error ?? workers.error} />
  const byType = (t: string) => workers.data!.workers.filter((w) => w.type === t)
  const changed = () => {
    workers.reload()
    setRestartNote(true)
  }
  return (
    <Page title={t('providers.title')}>
      {restartNote ? <RestartNote onDone={() => setRestartNote(false)} /> : null}
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>
        {types.data.types.map((pt) => (
          <Card key={pt.type} withBorder>
            <Text fw={600}>{pt.label}</Text>
            <Text size="sm" c="dimmed">
              {pt.cost}
            </Text>
            {pt.note ? (
              <Text size="xs" c="dimmed" mt={4}>
                {pt.note}
              </Text>
            ) : null}
            <Button mt="sm" size="xs" variant="light" leftSection={<IconPlus size={14} />} onClick={() => setAdding(pt)}>
              {t('providers.add-worker')}
            </Button>
          </Card>
        ))}
      </SimpleGrid>
      {types.data.types.map((pt) => (
        <Stack key={pt.type} gap="xs">
          <Title order={4}>{pt.label}</Title>
          {byType(pt.type).length ? (
            <SimpleGrid cols={{ base: 1, md: 2 }}>
              {byType(pt.type).map((w) => (
                <WorkerConfigCard key={w.id} worker={w} onEdit={() => setEditing(w)} onChanged={changed} />
              ))}
            </SimpleGrid>
          ) : (
            <Empty>{t('providers.no-workers')}</Empty>
          )}
        </Stack>
      ))}
      {adding ? <AddWorker type={adding} onClose={() => setAdding(null)} onAdded={changed} /> : null}
      {editing ? (
        <EditWorker
          worker={editing}
          type={types.data.types.find((t) => t.type === editing.type)}
          onClose={() => setEditing(null)}
          onSaved={changed}
        />
      ) : null}
    </Page>
  )
}

function RestartNote({ onDone }: { onDone: () => void }) {
  const t = useT()
  const [busy, setBusy] = useState(false)
  return (
    <Alert color="blue" title={t('providers.restart-title')}>
      <Group>
        <Text size="sm">{t('providers.restart-message')}</Text>
        <Button
          size="xs"
          loading={busy}
          onClick={async () => {
            setBusy(true)
            const res = await attempt(() => post('/api/system/restart', { services: ['kite-gateway'] }), t('providers.restarted'))
            setBusy(false)
            if (res) onDone()
          }}
        >
          {t('providers.restart-button')}
        </Button>
      </Group>
    </Alert>
  )
}

function WorkerConfigCard({ worker: w, onEdit, onChanged }: { worker: WorkerRow; onEdit: () => void; onChanged: () => void }) {
  const t = useT()
  const rows: [string, ReactNode][] = [[t('providers.card.model'), w.models.join(', ')]]
  if (w.kaggle) rows.push([t('providers.card.notebook'), `${w.kaggle.kernel} (${t('providers.card.via', { via: w.kaggle.via === 'browser' ? t('providers.card.via-browser') : t('providers.card.via-api') })})`])
  if (w.api?.base_url) rows.push([t('providers.card.base-url'), w.api.base_url])
  if (w.api && !w.pod) rows.push([t('providers.card.price'), `${w.api.price.input} / ${w.api.price.output} ${t('providers.card.price-unit')}`])
  if (w.pod) rows.push([t('providers.card.pod'), `${w.pod.id}:${w.pod.port}${w.pod.price_hour ? ` · ${t('providers.card.pod-price', { price: w.pod.price_hour })}` : ''} · ${t('providers.card.pod-idle', { minutes: w.pod.idle_minutes })}`])
  if (w.api?.caps && (w.api.caps.daily_usd || w.api.caps.monthly_usd))
    rows.push([t('providers.card.caps'), t('providers.card.caps-value', { daily: w.api.caps.daily_usd ?? '—', monthly: w.api.caps.monthly_usd ?? '—' })])
  rows.push([t('providers.card.spent'), t('providers.card.spent-value', { today: usd(w.spend.today_usd), month: usd(w.spend.month_usd) })])
  return (
    <Card withBorder>
      <Group justify="space-between">
        <div>
          <Text fw={600}>
            {w.brand} · {w.models[0] ?? w.id}
          </Text>
          <Text size="xs" c="dimmed">
            {w.id} · {w.type}
          </Text>
        </div>
        <Badge color={w.enabled ? 'teal' : 'gray'} variant="light">
          {w.enabled ? t('providers.card.enabled') : t('providers.card.disabled')}
        </Badge>
      </Group>
      <Stack gap={2} mt="xs">
        {rows.map(([k, v]) => (
          <Text key={k} size="sm">
            <Text span c="dimmed">
              {k}:
            </Text>{' '}
            {v}
          </Text>
        ))}
      </Stack>
      <Group gap="xs" mt="sm">
        <Button size="xs" variant="light" onClick={onEdit}>
          {t('providers.card.edit')}
        </Button>
        <Button
          size="xs"
          variant="subtle"
          onClick={async () => {
            await attempt(() => patch(`/api/workers/${w.id}`, { enabled: !w.enabled }), w.enabled ? t('providers.card.disabled-msg') : t('providers.card.enabled-msg'))
            onChanged()
          }}
        >
          {w.enabled ? t('providers.card.disable') : t('providers.card.enable')}
        </Button>
        <ConfirmButton
          size="xs"
          variant="subtle"
          color="red"
          label={t('providers.card.delete')}
          title={t('providers.card.delete-title', { id: w.id })}
          message={t('providers.card.delete-message')}
          confirmLabel={t('providers.card.delete')}
          onConfirm={async () => {
            await attempt(() => del(`/api/workers/${w.id}`), t('providers.card.deleted'))
            onChanged()
          }}
        />
      </Group>
    </Card>
  )
}

function AddWorker({ type, onClose, onAdded }: { type: ProviderType; onClose: () => void; onAdded: () => void }) {
  const t = useT()
  const [values, setValues] = useState<Values>(type.type === 'kaggle' ? { via: 'browser', owner: 'self' } : {})
  const [test, setTest] = useState<Record<string, any> | null>(null)
  const [busy, setBusy] = useState(false)
  const [added, setAdded] = useState<Record<string, any> | null>(null)
  const set = (k: string) => (v: any) => setValues((s) => ({ ...s, [k]: v }))
  const fields = type.fields.filter((f) => !(type.type === 'anthropic' && f === 'base_url'))
  const body = () => ({ ...values, type: type.type, models: values.models ?? [] })
  const isRelay = type.type === 'openai' || type.type === 'anthropic'
  const presets = useLoad(() => get<{ presets: ProviderPreset[] }>('/api/provider-presets'), [])
  const brandOptions = (presets.data?.presets ?? []).filter((p) => p.type === type.type)
  const [brand, setBrand] = useState('custom')
  const applyBrand = (b: string | null) => {
    const value = b ?? 'custom'
    setBrand(value)
    const preset = brandOptions.find((p) => p.brand === value)
    if (!preset) return
    setValues((s) => {
      const next = { ...s }
      if (fields.includes('base_url')) next.base_url = preset.base_url
      if (fields.includes('models')) next.models = preset.models.map((m) => m.id)
      const first = preset.models[0]
      if (first && fields.includes('price_in')) next.price_in = first.price_in
      if (first && fields.includes('price_out')) next.price_out = first.price_out
      return next
    })
  }
  return (
    <Modal opened onClose={onClose} title={t('providers.add.title', { label: type.label })} size="lg">
      {added ? (
        <Stack>
          <Alert color="teal" title={t('providers.add.added-title', { added: added.added })}>
            {added.next ?? t('providers.add.added-next')}
          </Alert>
          {added.worker_token ? (
            <Stack gap={4}>
              <Text size="sm" fw={600}>
                {t('providers.add.worker-token')}
              </Text>
              <Code block>{added.worker_token}</Code>
            </Stack>
          ) : null}
          {added.warning ? <Alert color="yellow">{added.warning}</Alert> : null}
          <Group justify="flex-end">
            <Button
              onClick={() => {
                onAdded()
                onClose()
              }}
            >
              {t('providers.add.done')}
            </Button>
          </Group>
        </Stack>
      ) : (
        <Stack>
          {isRelay && brandOptions.length ? (
            <Stack gap={4}>
              <Select
                label={t('providers.add.brand')}
                description={t('providers.add.brand-desc')}
                data={[{ value: 'custom', label: t('providers.add.brand-custom') }, ...brandOptions.map((p) => ({ value: p.brand, label: p.label }))]}
                value={brand}
                onChange={applyBrand}
                allowDeselect={false}
              />
              {brand !== 'custom' ? (
                <Text size="xs" c="dimmed">
                  {brandOptions.find((p) => p.brand === brand)?.note}
                </Text>
              ) : null}
            </Stack>
          ) : null}
          {fields.map((f) => (
            <FieldInput key={f} name={f} value={values[f]} onChange={set(f)} />
          ))}
          {test ? (
            <Alert color={test.ok ? 'teal' : 'red'} title={test.ok ? t('providers.add.test-ok') : t('providers.add.test-fail')}>
              {test.detail}
              {test.models?.length ? <Text size="xs" mt={4}>{t('providers.add.test-models', { models: test.models.slice(0, 20).join(', ') })}</Text> : null}
              {test.model_tested ? (
                <Text size="xs" mt={4}>
                  {t('providers.add.test-model-tried', { model: test.model_tested, ok: test.model_ok ? t('providers.add.test-model-ok') : t('providers.add.test-model-fail') })}
                  {typeof test.latency_ms === 'number' ? t('providers.add.test-latency', { ms: test.latency_ms }) : ''}
                  {test.chat_detail ? ` · ${test.chat_detail}` : ''}
                </Text>
              ) : null}
              {test.usage_reported === false ? (
                <Text size="xs" mt={4} c="orange.7">
                  {t('providers.add.test-no-usage')}
                </Text>
              ) : null}
            </Alert>
          ) : null}
          <Group justify="space-between">
            <Button
              variant="default"
              loading={busy}
              onClick={async () => {
                setBusy(true)
                try {
                  setTest(await post('/api/providers/test', body()))
                } catch (e) {
                  notifyError(e)
                } finally {
                  setBusy(false)
                }
              }}
            >
              {t('providers.add.test')}
            </Button>
            <Group gap="xs">
              <Button variant="default" onClick={onClose}>
                {t('common.cancel')}
              </Button>
              <Button
                onClick={async () => {
                  const res = await attempt(() => post('/api/workers', body()))
                  if (res) setAdded(res)
                }}
              >
                {t('providers.add.add')}
              </Button>
            </Group>
          </Group>
        </Stack>
      )}
    </Modal>
  )
}

const EDITABLE = ['models', 'base_url', 'api_key', 'runpod_api_key', 'kaggle_api_token', 'kernel', 'via', 'idle_minutes', 'price_in', 'price_out', 'price_hour', 'daily_cap', 'monthly_cap', 'concurrency']

function EditWorker({ worker: w, type, onClose, onSaved }: { worker: WorkerRow; type?: ProviderType; onClose: () => void; onSaved: () => void }) {
  const t = useT()
  const initial: Values = {
    models: w.models.map((m) => (w.api?.upstream?.[m] ? `${m}=${w.api.upstream[m]}` : m)),
    base_url: w.api?.base_url ?? undefined,
    kernel: w.kaggle?.kernel,
    via: w.kaggle?.via,
    idle_minutes: w.kaggle?.idle_minutes ?? w.pod?.idle_minutes,
    price_in: w.api && !w.pod ? w.api.price.input : undefined,
    price_out: w.api && !w.pod ? w.api.price.output : undefined,
    price_hour: w.pod?.price_hour ?? undefined,
    daily_cap: w.api?.caps?.daily_usd ?? undefined,
    monthly_cap: w.api?.caps?.monthly_usd ?? undefined,
    concurrency: w.concurrency,
  }
  const [values, setValues] = useState<Values>(initial)
  const fields = (type?.fields ?? []).filter((f) => EDITABLE.includes(f) && !(w.type === 'kaggle' && !w.kaggle && ['via', 'idle_minutes', 'kaggle_api_token'].includes(f)))
  return (
    <Modal opened onClose={onClose} title={t('providers.edit.title', { id: w.id })} size="lg">
      <Stack>
        {fields.map((f) => (
          <FieldInput key={f} name={f} value={values[f]} onChange={(v) => setValues((s) => ({ ...s, [f]: v }))} editing />
        ))}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            onClick={async () => {
              const changes: Values = {}
              for (const f of fields) {
                const a = JSON.stringify(values[f] ?? null)
                const b = JSON.stringify(initial[f] ?? null)
                if (FIELDS[f]?.kind === 'secret') {
                  if (values[f]) changes[f] = values[f]
                } else if (a !== b) changes[f] = values[f] ?? null
              }
              if (!Object.keys(changes).length) return onClose()
              const res = await attempt(() => patch(`/api/workers/${w.id}`, changes), t('providers.edit.saved'))
              if (res) {
                onSaved()
                onClose()
              }
            }}
          >
            {t('providers.edit.save')}
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          {t('providers.edit.note')}
        </Text>
      </Stack>
    </Modal>
  )
}
