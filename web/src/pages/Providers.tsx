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
import { attempt, notifyError, usd, useLoad } from '../util'

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

export default function Providers() {
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
    <Page title="Provider">
      {restartNote ? <RestartNote onDone={() => setRestartNote(false)} /> : null}
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>
        {types.data.types.map((t) => (
          <Card key={t.type} withBorder>
            <Text fw={600}>{t.label}</Text>
            <Text size="sm" c="dimmed">
              {t.cost}
            </Text>
            {t.note ? (
              <Text size="xs" c="dimmed" mt={4}>
                {t.note}
              </Text>
            ) : null}
            <Button mt="sm" size="xs" variant="light" leftSection={<IconPlus size={14} />} onClick={() => setAdding(t)}>
              Thêm worker
            </Button>
          </Card>
        ))}
      </SimpleGrid>
      {types.data.types.map((t) => (
        <Stack key={t.type} gap="xs">
          <Title order={4}>{t.label}</Title>
          {byType(t.type).length ? (
            <SimpleGrid cols={{ base: 1, md: 2 }}>
              {byType(t.type).map((w) => (
                <WorkerConfigCard key={w.id} worker={w} onEdit={() => setEditing(w)} onChanged={changed} />
              ))}
            </SimpleGrid>
          ) : (
            <Empty>Chưa có.</Empty>
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

function WorkerConfigCard({ worker: w, onEdit, onChanged }: { worker: WorkerRow; onEdit: () => void; onChanged: () => void }) {
  const rows: [string, ReactNode][] = [['Model', w.models.join(', ')]]
  if (w.kaggle) rows.push(['Notebook', `${w.kaggle.kernel} (bật qua ${w.kaggle.via === 'browser' ? 'trình duyệt Kite' : 'Kaggle API'})`])
  if (w.api?.base_url) rows.push(['Base URL', w.api.base_url])
  if (w.api && !w.pod) rows.push(['Giá', `${w.api.price.input} / ${w.api.price.output} USD mỗi triệu token vào/ra`])
  if (w.pod) rows.push(['Pod', `${w.pod.id}:${w.pod.port}${w.pod.price_hour ? ` · ${w.pod.price_hour} USD/giờ` : ''} · tắt sau ${w.pod.idle_minutes} phút rảnh`])
  if (w.api?.caps && (w.api.caps.daily_usd || w.api.caps.monthly_usd))
    rows.push(['Hạn mức', `${w.api.caps.daily_usd ?? '—'}/ngày · ${w.api.caps.monthly_usd ?? '—'}/tháng`])
  rows.push(['Đã chi', `${usd(w.spend.today_usd)} hôm nay · ${usd(w.spend.month_usd)} tháng này`])
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
          {w.enabled ? 'đang dùng' : 'tắt trong cấu hình'}
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
          Sửa
        </Button>
        <Button
          size="xs"
          variant="subtle"
          onClick={async () => {
            await attempt(() => patch(`/api/workers/${w.id}`, { enabled: !w.enabled }), w.enabled ? 'Đã tắt trong cấu hình' : 'Đã bật trong cấu hình')
            onChanged()
          }}
        >
          {w.enabled ? 'Tắt trong cấu hình' : 'Bật trong cấu hình'}
        </Button>
        <ConfirmButton
          size="xs"
          variant="subtle"
          color="red"
          label="Xóa"
          title={`Xóa worker ${w.id}?`}
          message="Xóa khỏi config.yaml (có bản sao lưu) và xóa các file key riêng của worker này."
          confirmLabel="Xóa"
          onConfirm={async () => {
            await attempt(() => del(`/api/workers/${w.id}`), 'Đã xóa')
            onChanged()
          }}
        />
      </Group>
    </Card>
  )
}

function AddWorker({ type, onClose, onAdded }: { type: ProviderType; onClose: () => void; onAdded: () => void }) {
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
    <Modal opened onClose={onClose} title={`Thêm worker: ${type.label}`} size="lg">
      {added ? (
        <Stack>
          <Alert color="teal" title={`Đã thêm ${added.added}`}>
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
          <Group justify="flex-end">
            <Button
              onClick={() => {
                onAdded()
                onClose()
              }}
            >
              Xong
            </Button>
          </Group>
        </Stack>
      ) : (
        <Stack>
          {isRelay && brandOptions.length ? (
            <Stack gap={4}>
              <Select
                label="Hãng"
                description="Chọn hãng để điền sẵn Base URL, model gợi ý và giá tham chiếu; mọi ô vẫn sửa tay được."
                data={[{ value: 'custom', label: 'Tùy chỉnh (tự nhập)' }, ...brandOptions.map((p) => ({ value: p.brand, label: p.label }))]}
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
            <Alert color={test.ok ? 'teal' : 'red'} title={test.ok ? 'Kết nối được' : 'Chưa kết nối được'}>
              {test.detail}
              {test.models?.length ? <Text size="xs" mt={4}>Model: {test.models.slice(0, 20).join(', ')}</Text> : null}
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
              Thử kết nối
            </Button>
            <Group gap="xs">
              <Button variant="default" onClick={onClose}>
                Thôi
              </Button>
              <Button
                onClick={async () => {
                  const res = await attempt(() => post('/api/workers', body()))
                  if (res) setAdded(res)
                }}
              >
                Thêm
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
    <Modal opened onClose={onClose} title={`Sửa ${w.id}`} size="lg">
      <Stack>
        {fields.map((f) => (
          <FieldInput key={f} name={f} value={values[f]} onChange={(v) => setValues((s) => ({ ...s, [f]: v }))} editing />
        ))}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Thôi
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
              const res = await attempt(() => patch(`/api/workers/${w.id}`, changes), 'Đã lưu')
              if (res) {
                onSaved()
                onClose()
              }
            }}
          >
            Lưu
          </Button>
        </Group>
        <Text size="xs" c="dimmed">
          Trường không sửa ở đây được (ID, loại, chủ): xóa rồi thêm lại.
        </Text>
      </Stack>
    </Modal>
  )
}
