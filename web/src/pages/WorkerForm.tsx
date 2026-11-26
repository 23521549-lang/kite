// Trang cấu hình worker: tạo mới (đặt tên → chọn hãng phẳng → form theo hãng) hoặc sửa worker đã có.
// ID kỹ thuật tự sinh từ tên hiển thị lúc tạo, không đổi, không hiện trong giao diện.
import {
  Alert,
  Button,
  Card,
  Code,
  Group,
  NumberInput,
  PasswordInput,
  Select,
  SimpleGrid,
  Stack,
  TagsInput,
  Text,
  TextInput,
} from '@mantine/core'
import { useEffect, useMemo, useState } from 'react'
import { get, patch, post, type ProviderType, type WorkerRow } from '../api'
import { Loading, Page } from '../components/common'
import { go } from '../router'
import { useT, type DictKey } from '../i18n'
import { attempt, notifyError, useLoad } from '../util'

type FieldDef = { labelKey: DictKey; kind: 'text' | 'secret' | 'number' | 'models' | 'via'; placeholder?: string; descriptionKey?: DictKey; required?: boolean }

const FIELDS: Record<string, FieldDef> = {
  id: { labelKey: 'workers.form.field.id', kind: 'text', required: true, placeholder: 'kaggle-han, openrouter…', descriptionKey: 'workers.form.field.id-desc' },
  owner: { labelKey: 'workers.form.field.owner', kind: 'text', placeholder: 'self' },
  models: { labelKey: 'workers.form.field.models', kind: 'models', required: true, descriptionKey: 'workers.form.field.models-desc' },
  kernel: { labelKey: 'workers.form.field.kernel', kind: 'text', placeholder: '<user>/<slug>', descriptionKey: 'workers.form.field.kernel-desc' },
  kaggle_api_token: { labelKey: 'workers.form.field.kaggle-api-token', kind: 'secret', descriptionKey: 'workers.form.field.kaggle-api-token-desc' },
  via: { labelKey: 'workers.form.field.via', kind: 'via' },
  idle_minutes: { labelKey: 'workers.form.field.idle-minutes', kind: 'number', placeholder: '30' },
  base_url: { labelKey: 'workers.form.field.base-url', kind: 'text', placeholder: 'https://openrouter.ai/api/v1', required: true },
  api_key: { labelKey: 'workers.form.field.api-key', kind: 'secret' },
  runpod_api_key: { labelKey: 'workers.form.field.runpod-api-key', kind: 'secret', required: true, descriptionKey: 'workers.form.field.runpod-api-key-desc' },
  pod_id: { labelKey: 'workers.form.field.pod-id', kind: 'text', required: true },
  port: { labelKey: 'workers.form.field.port', kind: 'number', placeholder: '8000' },
  price_in: { labelKey: 'workers.form.field.price-in', kind: 'number', placeholder: '0' },
  price_out: { labelKey: 'workers.form.field.price-out', kind: 'number', placeholder: '0' },
  price_hour: { labelKey: 'workers.form.field.price-hour', kind: 'number', descriptionKey: 'workers.form.field.price-hour-desc' },
  daily_cap: { labelKey: 'workers.form.field.daily-cap', kind: 'number' },
  monthly_cap: { labelKey: 'workers.form.field.monthly-cap', kind: 'number' },
  concurrency: { labelKey: 'workers.form.field.concurrency', kind: 'number', placeholder: '2' },
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
  const f = FIELDS[name]
  const common = {
    label: f ? t(f.labelKey) : name,
    description: f?.descriptionKey ? t(f.descriptionKey) : undefined,
    placeholder: f?.placeholder,
    required: (f?.required ?? false) && !editing,
  }
  switch (f?.kind) {
    case 'secret':
      return (
        <PasswordInput
          {...common}
          placeholder={editing ? t('workers.form.secret-keep') : f.placeholder}
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
            { value: 'api', label: 'Kaggle API (kaggle kernels push)' },
            { value: 'browser', label: t('workers.form.via-browser') },
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

/** Tên hiển thị → ID kỹ thuật: chữ thường, đ→d, bỏ dấu, cụm ký tự khác thành một `-`, tối đa 63 ký tự. */
export function slugifyLabel(label: string): string {
  return label
    .toLowerCase()
    .replace(/đ/g, 'd')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .slice(0, 63)
    .replace(/^-+|-+$/g, '')
}

const SLUG_RE = /^[a-z][a-z0-9-]{1,62}$/

const EDITABLE = ['models', 'base_url', 'api_key', 'runpod_api_key', 'kaggle_api_token', 'kernel', 'via', 'idle_minutes', 'price_in', 'price_out', 'price_hour', 'daily_cap', 'monthly_cap', 'concurrency']

function buildInitial(w: WorkerRow): Values {
  return {
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
}

function RestartNote({ onDone }: { onDone: () => void }) {
  const t = useT()
  const [busy, setBusy] = useState(false)
  return (
    <Alert color="blue" title={t('workers.form.restart-title')}>
      <Group>
        <Text size="sm">{t('workers.form.restart-body')}</Text>
        <Button
          size="xs"
          loading={busy}
          onClick={async () => {
            setBusy(true)
            const res = await attempt(() => post('/api/system/restart', { services: ['kite-gateway'] }), t('workers.form.restarted'))
            setBusy(false)
            if (res) onDone()
          }}
        >
          {t('workers.form.restart-button')}
        </Button>
      </Group>
    </Alert>
  )
}

export default function WorkerForm({ mode, workerId }: { mode: 'new' | 'edit'; workerId?: string }) {
  const t = useT()
  // Nạp song song ngay khi trang mount: preset về lúc nào thì bộ chọn hãng hiện lúc đó,
  // không đợi tới lúc mở modal như trước.
  const typesLoad = useLoad(() => get<{ types: ProviderType[] }>('/api/provider-types'), [])
  const presetsLoad = useLoad(() => get<{ presets: ProviderPreset[] }>('/api/provider-presets'), [])
  const workersLoad = useLoad(
    () => (mode === 'edit' ? get<{ workers: WorkerRow[] }>('/api/workers') : Promise.resolve({ workers: [] as WorkerRow[] })),
    [],
  )
  const worker = workersLoad.data?.workers.find((w) => w.id === workerId)

  const [label, setLabel] = useState('')
  const [nameError, setNameError] = useState<string | null>(null)
  const [selection, setSelection] = useState<string | null>(null)
  const [values, setValues] = useState<Values>({})
  const [test, setTest] = useState<Record<string, any> | null>(null)
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const [added, setAdded] = useState<Record<string, any> | null>(null)
  const [saved, setSaved] = useState(false)
  const [restartNote, setRestartNote] = useState(false)
  const [initialized, setInitialized] = useState(false)

  const initial = useMemo(() => (worker ? buildInitial(worker) : {}), [worker])

  useEffect(() => {
    if (mode !== 'edit' || initialized || !worker) return
    setLabel(worker.label ?? '')
    setValues(buildInitial(worker))
    setInitialized(true)
  }, [mode, worker, initialized])

  const types = typesLoad.data?.types ?? []
  // Chỉ lấy preset relay (type openai/anthropic); Kaggle, RunPod, Tùy chỉnh là lựa chọn riêng.
  const relayPresets = (presetsLoad.data?.presets ?? []).filter((p) => p.type === 'openai' || p.type === 'anthropic')
  const preset = selection ? relayPresets.find((p) => p.brand === selection) : undefined
  const resolvedType =
    mode === 'edit'
      ? worker?.type ?? null
      : selection === 'kaggle'
        ? 'kaggle'
        : selection === 'runpod'
          ? 'runpod'
          : preset
            ? preset.type
            : selection === 'custom'
              ? 'openai'
              : null
  const type = types.find((t) => t.type === resolvedType)
  const createFields = (type?.fields ?? []).filter((f) => f !== 'id' && !(type?.type === 'anthropic' && f === 'base_url'))
  const editFields = (type?.fields ?? []).filter(
    (f) => EDITABLE.includes(f) && !(worker?.type === 'kaggle' && !worker.kaggle && ['via', 'idle_minutes', 'kaggle_api_token'].includes(f)),
  )
  const fields = mode === 'edit' ? editFields : createFields

  const title = mode === 'new' ? t('workers.form.title-new') : t('workers.form.title-edit', { name: worker?.label ?? 'worker' })
  if (!typesLoad.data) {
    return (
      <Page title={title}>
        <Loading error={typesLoad.error} />
      </Page>
    )
  }
  if (mode === 'edit' && !workersLoad.data) {
    return (
      <Page title={title}>
        <Loading error={workersLoad.error} />
      </Page>
    )
  }
  if (mode === 'edit' && !worker) {
    return (
      <Page title={title}>
        <Alert color="red" title={t('workers.form.not-found')}>
          {t('workers.form.not-found-body')}
        </Alert>
        <Group>
          <Button variant="default" onClick={() => go('/workers')}>
            {t('workers.form.back-workers')}
          </Button>
        </Group>
      </Page>
    )
  }
  if (mode === 'edit' && !initialized) {
    return (
      <Page title={title}>
        <Loading />
      </Page>
    )
  }

  const set = (k: string) => (v: any) => setValues((s) => ({ ...s, [k]: v }))

  /** Chọn hãng: điền sẵn base_url/model/giá như applyBrand cũ; đổi hãng thì làm mới form theo hãng. */
  const choose = (key: string) => {
    setSelection(key)
    setTest(null)
    if (key === 'kaggle') {
      setValues({ via: 'browser', owner: 'self' })
      return
    }
    const chosen = relayPresets.find((p) => p.brand === key)
    if (chosen) {
      const targetFields = types.find((t) => t.type === chosen.type)?.fields ?? []
      const next: Values = {}
      if (targetFields.includes('base_url')) next.base_url = chosen.base_url
      if (targetFields.includes('models')) next.models = chosen.models.map((m) => m.id)
      const first = chosen.models[0]
      if (first && targetFields.includes('price_in')) next.price_in = first.price_in
      if (first && targetFields.includes('price_out')) next.price_out = first.price_out
      setValues(next)
      return
    }
    setValues({})
  }

  const validateName = (): string | null => {
    const name = label.trim()
    if (!name || name.length > 100) {
      setNameError(t('workers.form.name-error-required'))
      return null
    }
    const slug = slugifyLabel(name)
    if (!SLUG_RE.test(slug)) {
      setNameError(t('workers.form.name-error-slug'))
      return null
    }
    setNameError(null)
    return slug
  }

  const createBody = (slug: string): Values => {
    const out: Values = { id: slug, label: label.trim(), type: resolvedType }
    for (const f of createFields) if (values[f] !== undefined) out[f] = values[f]
    out.models = values.models ?? []
    if (resolvedType === 'kaggle' && !out.owner) out.owner = 'self'
    return out
  }

  const submitCreate = async () => {
    const slug = validateName()
    if (!slug || !resolvedType) return
    setSaving(true)
    try {
      const res = await attempt(() => post('/api/workers', createBody(slug)))
      if (res) {
        setAdded(res)
        setRestartNote(true)
      }
    } finally {
      setSaving(false)
    }
  }

  const submitEdit = async () => {
    if (!worker) return
    const name = label.trim()
    if (name.length > 100) {
      setNameError(t('workers.form.name-error-max'))
      return
    }
    setNameError(null)
    const changes: Values = {}
    for (const f of editFields) {
      if (FIELDS[f]?.kind === 'secret') {
        if (values[f]) changes[f] = values[f]
      } else if (JSON.stringify(values[f] ?? null) !== JSON.stringify(initial[f] ?? null)) {
        changes[f] = values[f] ?? null
      }
    }
    if (name !== (worker.label ?? '')) changes.label = name
    if (!Object.keys(changes).length) {
      go('/workers')
      return
    }
    setSaving(true)
    try {
      const res = await attempt(() => patch(`/api/workers/${worker.id}`, changes), t('workers.form.saved-title'))
      if (res) {
        setSaved(true)
        setRestartNote(true)
      }
    } finally {
      setSaving(false)
    }
  }

  if (added) {
    return (
      <Page title={t('workers.form.title-new')}>
        <Stack>
          <Alert color="teal" title={t('workers.form.added-title', { name: label.trim() })}>
            {added.next ?? t('workers.form.added-next')}
          </Alert>
          {added.worker_token ? (
            <Stack gap={4}>
              <Text size="sm" fw={600}>
                {t('workers.form.token-note')}
              </Text>
              <Code block>{added.worker_token}</Code>
            </Stack>
          ) : null}
          {added.warning ? <Alert color="yellow">{added.warning}</Alert> : null}
          {restartNote ? <RestartNote onDone={() => setRestartNote(false)} /> : null}
          <Group justify="flex-end">
            <Button onClick={() => go('/workers')}>{t('workers.form.done')}</Button>
          </Group>
        </Stack>
      </Page>
    )
  }

  if (saved) {
    return (
      <Page title={title}>
        <Stack>
          <Alert color="teal" title={t('workers.form.saved-title')}>
            {t('workers.form.saved-body')}
          </Alert>
          {restartNote ? <RestartNote onDone={() => setRestartNote(false)} /> : null}
          <Group justify="flex-end">
            <Button onClick={() => go('/workers')}>{t('workers.form.done')}</Button>
          </Group>
        </Stack>
      </Page>
    )
  }

  const brandChoices = [
    ...relayPresets.map((p) => ({ key: p.brand, label: p.label, hint: p.note || p.base_url })),
    { key: 'kaggle', label: 'Kaggle', hint: t('workers.form.brand-kaggle-hint') },
    { key: 'runpod', label: 'RunPod', hint: t('workers.form.brand-runpod-hint') },
    { key: 'custom', label: t('workers.form.brand-custom'), hint: t('workers.form.brand-custom-hint') },
  ]

  const brandGrid = (
    <SimpleGrid cols={{ base: 2, sm: 3, lg: 4 }}>
      {brandChoices.map((c) => (
        <Card
          key={c.key}
          withBorder
          padding="sm"
          style={{ cursor: 'pointer', borderColor: selection === c.key ? 'var(--mantine-color-teal-6)' : undefined }}
          onClick={() => choose(c.key)}
        >
          <Text fw={selection === c.key ? 700 : 500} size="sm">
            {c.label}
          </Text>
          <Text size="xs" c="dimmed">
            {c.hint}
          </Text>
        </Card>
      ))}
    </SimpleGrid>
  )

  return (
    <Page title={title}>
      <Stack>
        {restartNote ? <RestartNote onDone={() => setRestartNote(false)} /> : null}

        <TextInput
          label={t('workers.form.name-label')}
          description={t('workers.form.name-desc')}
          placeholder={t('workers.form.name-placeholder')}
          required
          maxLength={100}
          value={label}
          onChange={(e) => {
            setLabel(e.currentTarget.value)
            setNameError(null)
          }}
          error={nameError}
        />

        {mode === 'edit' ? (
          <Text size="sm" c="dimmed">
            {t('workers.form.brand-line', { brand: worker?.brand ?? '', type: type?.label ?? worker?.type ?? '' })}
          </Text>
        ) : !presetsLoad.data ? (
          presetsLoad.error ? (
            <Stack gap={4}>
              <Alert color="red" title={t('workers.form.brand-load-error')}>
                {t('workers.form.brand-load-error-body', { error: presetsLoad.error })}
              </Alert>
              {brandGrid}
            </Stack>
          ) : (
            <Loading label={t('workers.form.brand-loading')} />
          )
        ) : (
          <Stack gap={4}>
            <Text fw={600} size="sm">
              {t('workers.form.brand')}
            </Text>
            <Text size="xs" c="dimmed">
              {t('workers.form.brand-desc')}
            </Text>
            {brandGrid}
            {preset ? (
              <Text size="xs" c="dimmed">
                {preset.note}
              </Text>
            ) : null}
          </Stack>
        )}

        {mode === 'new' && !resolvedType ? (
          <Text size="sm" c="dimmed">
            {t('workers.form.pick-brand')}
          </Text>
        ) : (
          <>
            {fields.map((f) => (
              <FieldInput key={f} name={f} value={values[f]} onChange={set(f)} editing={mode === 'edit'} />
            ))}
            {test ? (
              <Alert color={test.ok ? 'teal' : 'red'} title={test.ok ? t('workers.form.test-ok') : t('workers.form.test-fail')}>
                {test.detail}
                {test.models?.length ? (
                  <Text size="xs" mt={4}>
                    Model: {test.models.slice(0, 20).join(', ')}
                  </Text>
                ) : null}
                {test.model_tested ? (
                  <Text size="xs" mt={4}>
                    {t('workers.form.test-model', { model: test.model_tested })} {test.model_ok ? t('workers.form.test-model-ok') : t('workers.form.test-model-fail')}
                    {typeof test.latency_ms === 'number' ? ` · ${t('workers.form.test-latency', { ms: test.latency_ms })}` : ''}
                    {test.chat_detail ? ` · ${test.chat_detail}` : ''}
                  </Text>
                ) : null}
                {test.usage_reported === false ? (
                  <Text size="xs" mt={4} c="orange.7">
                    {t('workers.form.test-no-usage')}
                  </Text>
                ) : null}
              </Alert>
            ) : null}
            <Group justify="space-between">
              {mode === 'new' ? (
                <Button
                  variant="default"
                  loading={busy}
                  disabled={!resolvedType}
                  onClick={async () => {
                    const slug = validateName()
                    if (!slug || !resolvedType) return
                    setBusy(true)
                    try {
                      setTest(await post('/api/providers/test', createBody(slug)))
                    } catch (e) {
                      notifyError(e)
                    } finally {
                      setBusy(false)
                    }
                  }}
                >
                  {t('workers.form.test-connection')}
                </Button>
              ) : (
                <span />
              )}
              <Group gap="xs">
                <Button variant="default" onClick={() => go('/workers')}>
                  {t('common.cancel')}
                </Button>
                {mode === 'new' ? (
                  <Button loading={saving} disabled={!resolvedType} onClick={submitCreate}>
                    {t('workers.form.add')}
                  </Button>
                ) : (
                  <Button loading={saving} onClick={submitEdit}>
                    {t('workers.form.save')}
                  </Button>
                )}
              </Group>
            </Group>
            {mode === 'edit' ? (
              <Text size="xs" c="dimmed">
                {t('workers.form.edit-note')}
              </Text>
            ) : null}
          </>
        )}
      </Stack>
    </Page>
  )
}
