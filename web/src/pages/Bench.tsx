import { Alert, Button, Card, Checkbox, Code, Group, ScrollArea, Select, Stack, Table, Text, TextInput } from '@mantine/core'
import { useState } from 'react'
import { get, post, type WorkerRow } from '../api'
import { Empty, Loading, Page } from '../components/common'
import { attempt, useLoad } from '../util'

const SUITES = [
  { value: 'code', label: 'code: sửa code thật, Kite tự test' },
  { value: 'lead', label: 'lead: agent trưởng chọn tool đúng' },
  { value: 'speed', label: 'speed: tốc độ model' },
]

export default function Bench() {
  const history = useLoad(() => get<{ runs: Record<string, any>[]; table: string }>('/api/bench/history'), [], 30000)
  const workers = useLoad(() => get<{ workers: WorkerRow[] }>('/api/workers'), [])
  const [label, setLabel] = useState('')
  const [suites, setSuites] = useState<string[]>(['code'])
  const [models, setModels] = useState('')
  const [worker, setWorker] = useState<string | null>(null)
  const [started, setStarted] = useState<Record<string, any> | null>(null)
  const runs = history.data?.runs ?? []
  const columns = runs.length ? Object.keys(runs[0]).filter((k) => typeof runs[0][k] !== 'object').slice(0, 9) : []
  return (
    <Page title="Bench">
      <Card withBorder>
        <Text fw={600} mb="xs">
          Chạy đo mới
        </Text>
        <Stack gap="xs">
          <Group grow align="flex-start">
            <TextInput label="Tên lần đo" placeholder="v8" value={label} onChange={(e) => setLabel(e.currentTarget.value)} />
            <TextInput label="Model (để trống: tất cả)" placeholder="qwen3.6-fast,devstral" value={models} onChange={(e) => setModels(e.currentTarget.value)} />
            <Select
              label="Worker (để trống: worker rảnh)"
              data={(workers.data?.workers ?? []).map((w) => w.id)}
              value={worker}
              onChange={setWorker}
              clearable
            />
          </Group>
          <Checkbox.Group label="Bộ đo" value={suites} onChange={setSuites}>
            <Group mt={4}>
              {SUITES.map((s) => (
                <Checkbox key={s.value} value={s.value} label={s.label} />
              ))}
            </Group>
          </Checkbox.Group>
          <Group>
            <Button
              disabled={!label.trim() || !suites.length}
              onClick={async () => {
                const res = await attempt(() => post('/api/bench/run', { label, suites: suites.join(','), models, worker: worker ?? '' }), 'Đã bắt đầu đo ở nền')
                if (res) setStarted(res)
              }}
            >
              Chạy ở nền
            </Button>
            <Text size="xs" c="dimmed">
              Đo dùng GPU của worker; task của bạn sẽ phải chờ.
            </Text>
          </Group>
          {started ? (
            <Alert color="teal">
              <Code block>{JSON.stringify(started, null, 2)}</Code>
            </Alert>
          ) : null}
        </Stack>
      </Card>
      <Card withBorder>
        <Group justify="space-between" mb="xs">
          <Text fw={600}>Các lần đo</Text>
          <Button size="xs" variant="subtle" onClick={history.reload}>
            Làm mới
          </Button>
        </Group>
        {!history.data ? (
          <Loading error={history.error} />
        ) : runs.length ? (
          <ScrollArea type="auto">
            <Table striped highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  {columns.map((c) => (
                    <Table.Th key={c}>{c}</Table.Th>
                  ))}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {runs.map((r, i) => (
                  <Table.Tr key={i}>
                    {columns.map((c) => (
                      <Table.Td key={c}>
                        <Text size="sm">{String(r[c] ?? '—')}</Text>
                      </Table.Td>
                    ))}
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        ) : (
          <Empty>Chưa có lần đo nào.</Empty>
        )}
        {history.data?.table ? (
          <details>
            <summary>
              <Text span size="sm" c="dimmed">
                Bảng như kite bench history
              </Text>
            </summary>
            <Code block>{history.data.table}</Code>
          </details>
        ) : null}
      </Card>
    </Page>
  )
}
