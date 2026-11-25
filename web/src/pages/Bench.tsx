import { Alert, Badge, Button, Card, Checkbox, Code, Group, ScrollArea, Select, Stack, Table, Text, TextInput } from '@mantine/core'
import { useState } from 'react'
import { get, post, type BenchHistory, type BenchLevel, type BenchRunRow, type WorkerRow } from '../api'
import { Empty, Loading, Page } from '../components/common'
import RadarChart from '../components/RadarChart'
import { fmt, useBenchStrings, type BenchKey } from '../i18n/bench'
import { attempt, useLoad } from '../util'

// Trục theo thứ tự chuẩn của bench-standard §6. Map tĩnh tên hiển thị -> key
// i18n (không chế key động): tên trục trong `axes`/`radar` của API là tên chuẩn
// tiếng Việt.
const AXIS_LABEL_BY_NAME: Record<string, BenchKey> = {
  Coding: 'bench.axis.coding',
  'Phân tích': 'bench.axis.phan-tich',
  'Suy luận': 'bench.axis.suy-luan',
  'Tốc độ': 'bench.axis.toc-do',
  'Đúng việc': 'bench.axis.dung-viec',
  'Chi phí': 'bench.axis.chi-phi',
  'Ổn định': 'bench.axis.on-dinh',
  'Trí nhớ': 'bench.axis.tri-nho',
}
const AXIS_ORDER = Object.keys(AXIS_LABEL_BY_NAME)

function levelName(s: Record<BenchKey, string>, level: BenchLevel | null | undefined): string {
  if (!level) return '—'
  // Key i18n do backend đóng dấu (§9.2); rơi về tên tiếng Việt trong record
  // nếu key chưa có trong dict.
  return (s as Record<string, string>)[level.name_i18n_key] ?? level.name_vi
}

export default function Bench() {
  const s = useBenchStrings()
  const SUITES = [
    { value: 'code', label: s['bench.suite.code'] },
    { value: 'lead', label: s['bench.suite.lead'] },
    { value: 'speed', label: s['bench.suite.speed'] },
  ]
  const history = useLoad(() => get<BenchHistory>('/api/bench/history'), [], 30000)
  const workers = useLoad(() => get<{ workers: WorkerRow[] }>('/api/workers'), [])
  const [label, setLabel] = useState('')
  const [suites, setSuites] = useState<string[]>(['code'])
  const [models, setModels] = useState('')
  const [worker, setWorker] = useState<string | null>(null)
  const [started, setStarted] = useState<Record<string, unknown> | null>(null)
  const [netAck, setNetAck] = useState(false)
  const runs = history.data?.runs ?? []
  const declared = history.data?.declared_network_hosts ?? []
  const official = history.data?.official
  const criteriaRows = runs.filter((r) => r.overall != null || r.axes != null || r.network != null)
  // Radar: 2 lần chạy mới nhất có điểm trục (mới nhất trước).
  const radarRuns = [...runs].reverse().filter((r) => (r.radar?.length ?? 0) >= 3).slice(0, 2)
  const latestAxesRow = [...criteriaRows].reverse().find((r) => r.axes != null)
  const columns = runs.length ? Object.keys(runs[0]).filter((k) => typeof runs[0][k] !== 'object').slice(0, 9) : []
  return (
    <Page title={s['bench.title']}>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {s['bench.new-run']}
        </Text>
        <Stack gap="xs">
          <Group grow align="flex-start">
            <TextInput label={s['bench.new.label']} placeholder="v8" value={label} onChange={(e) => setLabel(e.currentTarget.value)} />
            <TextInput label={s['bench.new.models']} placeholder="qwen3.6-fast,devstral" value={models} onChange={(e) => setModels(e.currentTarget.value)} />
            <Select
              label={s['bench.new.worker']}
              data={(workers.data?.workers ?? []).map((w) => w.id)}
              value={worker}
              onChange={setWorker}
              clearable
            />
          </Group>
          <Checkbox.Group label={s['bench.new.suites']} value={suites} onChange={setSuites}>
            <Group mt={4}>
              {SUITES.map((su) => (
                <Checkbox key={su.value} value={su.value} label={su.label} />
              ))}
            </Group>
          </Checkbox.Group>
          {declared.length ? (
            <Stack gap="xs">
              <Text fw={600} size="sm">
                {s['bench.new.network-title']}
              </Text>
              <Text size="sm">
                {s['bench.new.network-hosts']}:{' '}
                {declared.map((h) => (
                  <Code key={h}>{h}</Code>
                ))}
              </Text>
              <Checkbox label={s['bench.new.network-confirm']} checked={netAck} onChange={(e) => setNetAck(e.currentTarget.checked)} />
              <Text size="xs" c="dimmed">
                {s['bench.new.network-note']}
              </Text>
            </Stack>
          ) : null}
          <Group>
            <Button
              disabled={!label.trim() || !suites.length}
              onClick={async () => {
                const res = await attempt(
                  () =>
                    post('/api/bench/run', {
                      label,
                      suites: suites.join(','),
                      models,
                      worker: worker ?? '',
                      network_ack: netAck,
                      network_hosts: netAck ? declared : [],
                    }),
                  s['bench.new.started'],
                )
                if (res) setStarted(res)
              }}
            >
              {s['bench.new.run-bg']}
            </Button>
            <Text size="xs" c="dimmed">
              {s['bench.new.exclusive-note']}
            </Text>
          </Group>
          {started ? (
            <Alert color="teal">
              <Code block>{JSON.stringify(started, null, 2)}</Code>
            </Alert>
          ) : null}
        </Stack>
      </Card>
      {official ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {s['bench.official.title']}
          </Text>
          <Group gap="md" align="center">
            <Text size="xl" fw={700}>
              {official.overall != null ? official.overall.toFixed(1) : '—'}
            </Text>
            {official.official && official.level ? (
              <Text fw={600}>{levelName(s, official.level)}</Text>
            ) : (
              <Badge color="gray">{s['bench.official.not-official']}</Badge>
            )}
            <Text size="sm" c="dimmed">
              {fmt(s['bench.official.runs'], { n: official.runs })}
            </Text>
            <Text size="sm" c="dimmed">
              {fmt(s['bench.official.criteria-version'], { version: official.criteria_version })}
            </Text>
          </Group>
          {official.level_unstable ? (
            <Text size="sm" c="orange" mt="xs">
              {s['bench.official.unstable']}
            </Text>
          ) : null}
        </Card>
      ) : null}
      {radarRuns.length ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {s['bench.radar.title']}
          </Text>
          <RadarChart
            axes={AXIS_ORDER.map((name) => s[AXIS_LABEL_BY_NAME[name]])}
            series={radarRuns.map((r, i) => ({
              label: `${String(r.folder ?? '')} · ${String(r.model ?? '')} (${i === 0 ? s['bench.radar.latest'] : s['bench.radar.previous']})`,
              values: AXIS_ORDER.map((name) => r.radar?.find((p) => p.axis === name)?.score ?? null),
            }))}
          />
        </Card>
      ) : null}
      {criteriaRows.length ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {s['bench.runs.title']}
          </Text>
          <ScrollArea type="auto">
            <Table striped highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{s['bench.runs.run']}</Table.Th>
                  <Table.Th>{s['bench.runs.started']}</Table.Th>
                  <Table.Th>{s['bench.runs.model']}</Table.Th>
                  <Table.Th>{s['bench.runs.overall']}</Table.Th>
                  <Table.Th>{s['bench.runs.level']}</Table.Th>
                  <Table.Th>{s['bench.runs.criteria']}</Table.Th>
                  <Table.Th>{s['bench.runs.network']}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {criteriaRows.map((r: BenchRunRow, i) => (
                  <Table.Tr key={i}>
                    <Table.Td>
                      <Text size="sm">{String(r.folder ?? '—')}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{String(r.started ?? '—')}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{String(r.model ?? '—')}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{r.overall != null ? (r.overall as number).toFixed(1) : '—'}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">
                        {levelName(s, r.level)}
                        {r.level_unstable ? ` (${s['bench.runs.unstable']})` : ''}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{r.criteria_version ?? '—'}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">
                        {r.network
                          ? r.network.mode === 'allowlist'
                            ? `${s['bench.runs.network-allowlist']}: ${r.network.hosts.join(', ')}`
                            : s['bench.runs.network-offline']
                          : '—'}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </Card>
      ) : null}
      {latestAxesRow ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {s['bench.axes.title']}
          </Text>
          <Table striped highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{s['bench.axes.axis']}</Table.Th>
                <Table.Th>{s['bench.axes.score']}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {AXIS_ORDER.map((name) => {
                const score = latestAxesRow.axes?.[name]
                return (
                  <Table.Tr key={name}>
                    <Table.Td>
                      <Text size="sm">{s[AXIS_LABEL_BY_NAME[name]]}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{score != null ? (score as number).toFixed(1) : '—'}</Text>
                    </Table.Td>
                  </Table.Tr>
                )
              })}
            </Table.Tbody>
          </Table>
        </Card>
      ) : null}
      <Card withBorder>
        <Group justify="space-between" mb="xs">
          <Text fw={600}>{s['bench.history']}</Text>
          <Button size="xs" variant="subtle" onClick={history.reload}>
            {s['bench.history.refresh']}
          </Button>
        </Group>
        {!history.data ? (
          <Loading error={history.error} />
        ) : criteriaRows.length ? (
          <Text size="sm" c="dimmed">
            {s['bench.history.table-note']}
          </Text>
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
          <Empty>{s['bench.history.empty']}</Empty>
        )}
        {history.data?.table ? (
          <details>
            <summary>
              <Text span size="sm" c="dimmed">
                {s['bench.history.table-note']}
              </Text>
            </summary>
            <Code block>{history.data.table}</Code>
          </details>
        ) : null}
      </Card>
    </Page>
  )
}
