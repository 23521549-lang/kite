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
import { attempt, notifyError, useLoad } from '../util'

type FieldDef = { label: string; kind: 'text' | 'secret' | 'number' | 'models' | 'via'; placeholder?: string; description?: string; required?: boolean }

const FIELDS: Record<string, FieldDef> = {
  id: { label: 'ID worker', kind: 'text', required: true, placeholder: 'kaggle-han, openrouter…', description: 'chữ thường, số, dấu -' },
  owner: { label: 'Chủ notebook', kind: 'text', placeholder: 'self' },
  models: { label: 'Model', kind: 'models', required: true, description: 'Tên model; dạng ten=model-that-cua-api nếu muốn đổi tên. Enter để thêm.' },
  kernel: { label: 'Notebook Kaggle', kind: 'text', placeholder: '<user>/<slug>', description: 'Để trống nếu không cho Kite tự bật notebook' },
  kaggle_api_token: { label: 'Kaggle API token', kind: 'secret', description: 'kaggle.com/settings → API → Create New Token' },
  via: { label: 'Cách bật notebook', kind: 'via' },
  idle_minutes: { label: 'Rảnh bao nhiêu phút thì tắt', kind: 'number', placeholder: '30' },
  base_url: { label: 'Base URL', kind: 'text', placeholder: 'https://openrouter.ai/api/v1', required: true },
  api_key: { label: 'API key', kind: 'secret' },
  runpod_api_key: { label: 'RunPod API key', kind: 'secret', required: true, description: 'quyền pods:write' },
  pod_id: { label: 'Pod ID', kind: 'text', required: true },
  port: { label: 'Cổng HTTP của pod', kind: 'number', placeholder: '8000' },
  price_in: { label: 'Giá token vào (USD/1 triệu)', kind: 'number', placeholder: '0' },
  price_out: { label: 'Giá token ra (USD/1 triệu)', kind: 'number', placeholder: '0' },
  price_hour: { label: 'Giá theo giờ (USD)', kind: 'number', description: 'để trống: lấy giá RunPod báo' },
  daily_cap: { label: 'Hạn mức chi/ngày (USD)', kind: 'number' },
  monthly_cap: { label: 'Hạn mức chi/tháng (USD)', kind: 'number' },
  concurrency: { label: 'Số việc chạy cùng lúc', kind: 'number', placeholder: '2' },
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
  const f = FIELDS[name] ?? { label: name, kind: 'text' }
  const common = { label: f.label, description: f.description, placeholder: f.placeholder, required: f.required && !editing }
  switch (f.kind) {
    case 'secret':
      return (
        <PasswordInput
          {...common}
          placeholder={editing ? 'để trống: giữ key cũ' : f.placeholder}
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
            { value: 'browser', label: 'Trình duyệt Kite (Save Version)' },
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
  const [busy, setBusy] = useState(false)
  return (
    <Alert color="blue" title="Cần khởi động lại gateway để áp thay đổi">
      <Group>
        <Text size="sm">Cấu hình đã lưu (có bản sao lưu). Gateway đọc lại khi khởi động lại; không làm được khi có task đang chạy.</Text>
        <Button
          size="xs"
          loading={busy}
          onClick={async () => {
            setBusy(true)
            const res = await attempt(() => post('/api/system/restart', { services: ['kite-gateway'] }), 'Đã khởi động lại gateway')
            setBusy(false)
            if (res) onDone()
          }}
        >
          Khởi động lại gateway
        </Button>
      </Group>
    </Alert>
  )
}

export default function WorkerForm({ mode, workerId }: { mode: 'new' | 'edit'; workerId?: string }) {
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

  const title = mode === 'new' ? 'Thêm worker' : `Sửa ${worker?.label ?? 'worker'}`
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
        <Alert color="red" title="Không tìm thấy worker">
          Worker này không còn trong cấu hình. Có thể nó vừa bị xóa.
        </Alert>
        <Group>
          <Button variant="default" onClick={() => go('/workers')}>
            Về trang Worker
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
      setNameError('Tên hiển thị bắt buộc, 1–100 ký tự.')
      return null
    }
    const slug = slugifyLabel(name)
    if (!SLUG_RE.test(slug)) {
      setNameError('tên phải chứa ít nhất một chữ cái (a–z)')
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
      setNameError('Tên hiển thị tối đa 100 ký tự.')
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
      const res = await attempt(() => patch(`/api/workers/${worker.id}`, changes), 'Đã lưu')
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
      <Page title="Thêm worker">
        <Stack>
          <Alert color="teal" title={`Đã thêm ${label.trim()}`}>
            {added.next ?? 'Khởi động lại gateway để dùng worker mới.'}
          </Alert>
          {added.worker_token ? (
            <Stack gap={4}>
              <Text size="sm" fw={600}>
                Worker token (chỉ hiện một lần): dán vào Kaggle → Add-ons → Secrets, tên KITE_WORKER_TOKEN
              </Text>
              <Code block>{added.worker_token}</Code>
            </Stack>
          ) : null}
          {added.warning ? <Alert color="yellow">{added.warning}</Alert> : null}
          {restartNote ? <RestartNote onDone={() => setRestartNote(false)} /> : null}
          <Group justify="flex-end">
            <Button onClick={() => go('/workers')}>Xong</Button>
          </Group>
        </Stack>
      </Page>
    )
  }

  if (saved) {
    return (
      <Page title={title}>
        <Stack>
          <Alert color="teal" title="Đã lưu">
            Cấu hình worker đã lưu (có bản sao lưu).
          </Alert>
          {restartNote ? <RestartNote onDone={() => setRestartNote(false)} /> : null}
          <Group justify="flex-end">
            <Button onClick={() => go('/workers')}>Xong</Button>
          </Group>
        </Stack>
      </Page>
    )
  }

  const brandChoices = [
    ...relayPresets.map((p) => ({ key: p.brand, label: p.label, hint: p.note || p.base_url })),
    { key: 'kaggle', label: 'Kaggle', hint: 'miễn phí, quota GPU theo tuần' },
    { key: 'runpod', label: 'RunPod', hint: 'máy GPU thuê theo giờ' },
    { key: 'custom', label: 'Tùy chỉnh', hint: 'tự nhập Base URL và model' },
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
          label="Tên hiển thị"
          description="Tên gọi của worker trong giao diện; sửa tự do, không đổi ID kỹ thuật."
          placeholder="vd: Kaggle chính, Groq nhanh…"
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
            Hãng: {worker?.brand} · Loại: {type?.label ?? worker?.type}
          </Text>
        ) : !presetsLoad.data ? (
          presetsLoad.error ? (
            <Stack gap={4}>
              <Alert color="red" title="Không tải được danh sách hãng">
                {presetsLoad.error} — vẫn chọn được Kaggle, RunPod hoặc Tùy chỉnh bên dưới.
              </Alert>
              {brandGrid}
            </Stack>
          ) : (
            <Loading label="Đang tải danh sách hãng…" />
          )
        ) : (
          <Stack gap={4}>
            <Text fw={600} size="sm">
              Hãng
            </Text>
            <Text size="xs" c="dimmed">
              Chọn hãng để điền sẵn Base URL, model gợi ý và giá tham chiếu; mọi ô vẫn sửa tay được.
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
            Chọn hãng ở trên để hiện form cấu hình.
          </Text>
        ) : (
          <>
            {fields.map((f) => (
              <FieldInput key={f} name={f} value={values[f]} onChange={set(f)} editing={mode === 'edit'} />
            ))}
            {test ? (
              <Alert color={test.ok ? 'teal' : 'red'} title={test.ok ? 'Kết nối được' : 'Chưa kết nối được'}>
                {test.detail}
                {test.models?.length ? (
                  <Text size="xs" mt={4}>
                    Model: {test.models.slice(0, 20).join(', ')}
                  </Text>
                ) : null}
                {test.model_tested ? (
                  <Text size="xs" mt={4}>
                    Model đã thử: {test.model_tested} — {test.model_ok ? 'gọi được' : 'chưa gọi được'}
                    {typeof test.latency_ms === 'number' ? ` · độ trễ ${test.latency_ms} ms` : ''}
                    {test.chat_detail ? ` · ${test.chat_detail}` : ''}
                  </Text>
                ) : null}
                {test.usage_reported === false ? (
                  <Text size="xs" mt={4} c="orange.7">
                    API không trả usage — chi phí sẽ tính theo ước lượng
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
                  Thử kết nối
                </Button>
              ) : (
                <span />
              )}
              <Group gap="xs">
                <Button variant="default" onClick={() => go('/workers')}>
                  Thôi
                </Button>
                {mode === 'new' ? (
                  <Button loading={saving} disabled={!resolvedType} onClick={submitCreate}>
                    Thêm
                  </Button>
                ) : (
                  <Button loading={saving} onClick={submitEdit}>
                    Lưu
                  </Button>
                )}
              </Group>
            </Group>
            {mode === 'edit' ? (
              <Text size="xs" c="dimmed">
                Trường không sửa ở đây được (ID, loại, chủ): xóa rồi thêm lại.
              </Text>
            ) : null}
          </>
        )}
      </Stack>
    </Page>
  )
}
