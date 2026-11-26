import { Alert, Badge, Button, Card, Checkbox, Code, Group, NumberInput, ScrollArea, Select, Stack, Table, Text, TextInput, Textarea } from '@mantine/core'
import { useState } from 'react'
import { get, importBenchTask, listBenchTasks, post, type BenchHistory, type BenchImportResult, type BenchLevel, type BenchRunRow, type WorkerRow } from '../api'
import { Empty, Loading, Page } from '../components/common'
import RadarChart from '../components/RadarChart'
import { useLanguage, useT, type DictKey, type Language } from '../i18n'
import { attempt, useLoad } from '../util'

// Axes in the canonical order of bench-standard §6. Static map display name -> i18n key
// (no dynamic keys): axis names in the API `axes`/`radar` are canonical Vietnamese names.
const AXIS_LABEL_BY_NAME: Record<string, DictKey> = {
  Coding: 'bench.axis.coding',
  'Phân tích': 'bench.axis.phan-tich', // i18n-ignore: canonical axis name stamped by the backend (bench-standard §6); lookup key, not UI text
  'Suy luận': 'bench.axis.suy-luan', // i18n-ignore: canonical axis name stamped by the backend (bench-standard §6); lookup key, not UI text
  'Tốc độ': 'bench.axis.toc-do', // i18n-ignore: canonical axis name stamped by the backend (bench-standard §6); lookup key, not UI text
  'Đúng việc': 'bench.axis.dung-viec', // i18n-ignore: canonical axis name stamped by the backend (bench-standard §6); lookup key, not UI text
  'Chi phí': 'bench.axis.chi-phi', // i18n-ignore: canonical axis name stamped by the backend (bench-standard §6); lookup key, not UI text
  'Ổn định': 'bench.axis.on-dinh', // i18n-ignore: canonical axis name stamped by the backend (bench-standard §6); lookup key, not UI text
  'Trí nhớ': 'bench.axis.tri-nho', // i18n-ignore: canonical axis name stamped by the backend (bench-standard §6); lookup key, not UI text
}
const AXIS_ORDER = Object.keys(AXIS_LABEL_BY_NAME)

function levelName(language: Language, level: BenchLevel | null | undefined): string {
  if (!level) return '—'
  // The backend stamps the bilingual level name per criteria version (§9.2);
  // render it in the current language, Vietnamese as fallback.
  return language === 'en' ? level.name_en || level.name_vi : level.name_vi
}

export default function Bench() {
  const t = useT()
  const { language } = useLanguage()
  const SUITES = [
    { value: 'code', label: t('bench.suite.code') },
    { value: 'lead', label: t('bench.suite.lead') },
    { value: 'speed', label: t('bench.suite.speed') },
  ]
  const history = useLoad(() => get<BenchHistory>('/api/bench/history'), [], 30000)
  const workers = useLoad(() => get<{ workers: WorkerRow[] }>('/api/workers'), [])
  const tasks = useLoad(() => listBenchTasks(), [], 30000)
  const [importSource, setImportSource] = useState('terminal-bench')
  const [importSrc, setImportSrc] = useState('')
  const [importTaskId, setImportTaskId] = useState('')
  const [importInstance, setImportInstance] = useState('')
  const [importInstanceId, setImportInstanceId] = useState('')
  const [importCheckout, setImportCheckout] = useState('')
  const [importAgentTimeout, setImportAgentTimeout] = useState<number>(360)
  const [importTestTimeout, setImportTestTimeout] = useState<number>(60)
  const [importResult, setImportResult] = useState<BenchImportResult | null>(null)
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
    <Page title={t('bench.title')}>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('bench.new-run')}
        </Text>
        <Stack gap="xs">
          <Group grow align="flex-start">
            <TextInput label={t('bench.new.label')} placeholder="v8" value={label} onChange={(e) => setLabel(e.currentTarget.value)} />
            <TextInput label={t('bench.new.models')} placeholder="qwen3.6-fast,devstral" value={models} onChange={(e) => setModels(e.currentTarget.value)} />
            <Select
              label={t('bench.new.worker')}
              data={(workers.data?.workers ?? []).map((w) => w.id)}
              value={worker}
              onChange={setWorker}
              clearable
            />
          </Group>
          <Checkbox.Group label={t('bench.new.suites')} value={suites} onChange={setSuites}>
            <Group mt={4}>
              {SUITES.map((su) => (
                <Checkbox key={su.value} value={su.value} label={su.label} />
              ))}
            </Group>
          </Checkbox.Group>
          {declared.length ? (
            <Stack gap="xs">
              <Text fw={600} size="sm">
                {t('bench.new.network-title')}
              </Text>
              <Text size="sm">
                {t('bench.new.network-hosts')}:{' '}
                {declared.map((h) => (
                  <Code key={h}>{h}</Code>
                ))}
              </Text>
              <Checkbox label={t('bench.new.network-confirm')} checked={netAck} onChange={(e) => setNetAck(e.currentTarget.checked)} />
              <Text size="xs" c="dimmed">
                {t('bench.new.network-note')}
              </Text>
            </Stack>
          ) : (
            <Text size="sm" c="dimmed">
              {t('bench.new.network-empty')}
            </Text>
          )}
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
                  t('bench.new.started'),
                )
                if (res) setStarted(res)
              }}
            >
              {t('bench.new.run-bg')}
            </Button>
            <Text size="xs" c="dimmed">
              {t('bench.new.exclusive-note')}
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
        <Text fw={600} mb="xs">
          {t('bench.import.title')}
        </Text>
        <Stack gap="xs">
          <Select
            label={t('bench.import.source')}
            data={[
              { value: 'terminal-bench', label: t('bench.import.source-tb') },
              { value: 'swe-bench', label: t('bench.import.source-swe') },
            ]}
            value={importSource}
            onChange={(v) => setImportSource(v ?? 'terminal-bench')}
          />
          {importSource === 'terminal-bench' ? (
            <Group grow align="flex-start">
              <TextInput
                label={t('bench.import.src-label')}
                placeholder={t('bench.import.src-placeholder')}
                value={importSrc}
                onChange={(e) => setImportSrc(e.currentTarget.value)}
              />
              <TextInput
                label={t('bench.import.task-id-label')}
                value={importTaskId}
                onChange={(e) => setImportTaskId(e.currentTarget.value)}
              />
            </Group>
          ) : (
            <Stack gap="xs">
              <Textarea
                label={t('bench.import.instance-label')}
                placeholder={t('bench.import.instance-placeholder')}
                value={importInstance}
                onChange={(e) => setImportInstance(e.currentTarget.value)}
                autosize
                minRows={3}
              />
              <Group grow align="flex-start">
                <TextInput
                  label={t('bench.import.repo-checkout-label')}
                  placeholder={t('bench.import.repo-checkout-placeholder')}
                  value={importCheckout}
                  onChange={(e) => setImportCheckout(e.currentTarget.value)}
                />
                <TextInput
                  label={t('bench.import.instance-id-label')}
                  value={importInstanceId}
                  onChange={(e) => setImportInstanceId(e.currentTarget.value)}
                />
              </Group>
              <Group grow align="flex-start">
                <NumberInput
                  label={t('bench.import.agent-timeout-label')}
                  value={importAgentTimeout}
                  onChange={(v) => setImportAgentTimeout(Number(v) || 360)}
                  min={1}
                />
                <NumberInput
                  label={t('bench.import.test-timeout-label')}
                  value={importTestTimeout}
                  onChange={(v) => setImportTestTimeout(Number(v) || 60)}
                  min={1}
                />
              </Group>
            </Stack>
          )}
          <Group>
            <Button
              disabled={importSource === 'terminal-bench' ? !importSrc.trim() : !importInstance.trim() || !importCheckout.trim()}
              onClick={async () => {
                const res = await attempt(() =>
                  importBenchTask({
                    source: importSource,
                    src: importSrc.trim(),
                    task_id: importTaskId.trim(),
                    instance_json: importInstance,
                    instance_id: importInstanceId.trim(),
                    repo_checkout: importCheckout.trim(),
                    agent_timeout: importAgentTimeout,
                    test_timeout: importTestTimeout,
                  }),
                )
                if (res) {
                  setImportResult(res)
                  if (res.ok) tasks.reload()
                }
              }}
            >
              {t('bench.import.run')}
            </Button>
          </Group>
          {importResult ? (
            <Alert color={importResult.ok ? 'teal' : 'red'}>
              <Text size="sm">
                {importResult.ok
                  ? t('bench.import.ok', { id: importResult.task_id ?? '' })
                  : t('bench.import.skipped', { reason: importResult.reason ?? '' })}
              </Text>
              {importResult.warnings.length ? (
                <Text size="sm" mt="xs">
                  {t('bench.import.warnings')}: {importResult.warnings.join('; ')}
                </Text>
              ) : null}
            </Alert>
          ) : null}
        </Stack>
      </Card>
      <Card withBorder>
        <Group justify="space-between" mb="xs">
          <Text fw={600}>{t('bench.tasks.title')}</Text>
          <Button size="xs" variant="subtle" onClick={tasks.reload}>
            {t('bench.tasks.refresh')}
          </Button>
        </Group>
        {!tasks.data ? (
          <Loading error={tasks.error} />
        ) : tasks.data.tasks.length ? (
          <ScrollArea type="auto">
            <Table striped highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t('bench.tasks.id')}</Table.Th>
                  <Table.Th>{t('bench.tasks.kind')}</Table.Th>
                  <Table.Th>{t('bench.tasks.source')}</Table.Th>
                  <Table.Th>{t('bench.tasks.axes')}</Table.Th>
                  <Table.Th>{t('bench.tasks.difficulty')}</Table.Th>
                  <Table.Th>{t('bench.tasks.status')}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {tasks.data.tasks.map((row) => (
                  <Table.Tr key={row.id}>
                    <Table.Td>
                      <Text size="sm">{row.id}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{row.kind ?? '—'}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{row.source}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{row.axes.length ? row.axes.join(', ') : '—'}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{row.difficulty != null ? String(row.difficulty) : '—'}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c={row.ok ? undefined : 'red'}>
                        {row.ok
                          ? t('bench.tasks.status-ok')
                          : `${t('bench.tasks.status-invalid')}: ${row.errors.map((e) => e.message).join('; ')}`}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        ) : (
          <Empty>{t('bench.tasks.empty')}</Empty>
        )}
      </Card>
      {official ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {t('bench.official.title')}
          </Text>
          <Group gap="md" align="center">
            <Text size="xl" fw={700}>
              {official.overall != null ? official.overall.toFixed(1) : '—'}
            </Text>
            {official.official && official.level ? (
              <Text fw={600}>{levelName(language, official.level)}</Text>
            ) : (
              <Badge color="gray">{t('bench.official.not-official')}</Badge>
            )}
            <Text size="sm" c="dimmed">
              {t('bench.official.runs', { n: official.runs })}
            </Text>
            <Text size="sm" c="dimmed">
              {t('bench.official.criteria-version', { version: official.criteria_version })}
            </Text>
          </Group>
          {official.level_unstable ? (
            <Text size="sm" c="orange" mt="xs">
              {t('bench.official.unstable')}
            </Text>
          ) : null}
        </Card>
      ) : null}
      {radarRuns.length ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {t('bench.radar.title')}
          </Text>
          <RadarChart
            axes={AXIS_ORDER.map((name) => t(AXIS_LABEL_BY_NAME[name]))}
            series={radarRuns.map((r, i) => ({
              label: `${String(r.folder ?? '')} · ${String(r.model ?? '')} (${i === 0 ? t('bench.radar.latest') : t('bench.radar.previous')})`,
              values: AXIS_ORDER.map((name) => r.radar?.find((p) => p.axis === name)?.score ?? null),
            }))}
          />
        </Card>
      ) : null}
      {criteriaRows.length ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {t('bench.runs.title')}
          </Text>
          <ScrollArea type="auto">
            <Table striped highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t('bench.runs.run')}</Table.Th>
                  <Table.Th>{t('bench.runs.started')}</Table.Th>
                  <Table.Th>{t('bench.runs.model')}</Table.Th>
                  <Table.Th>{t('bench.runs.overall')}</Table.Th>
                  <Table.Th>{t('bench.runs.level')}</Table.Th>
                  <Table.Th>{t('bench.runs.criteria')}</Table.Th>
                  <Table.Th>{t('bench.runs.network')}</Table.Th>
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
                        {levelName(language, r.level)}
                        {r.level_unstable ? ` (${t('bench.runs.unstable')})` : ''}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{r.criteria_version ?? '—'}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">
                        {r.network
                          ? r.network.mode === 'allowlist'
                            ? `${t('bench.runs.network-allowlist')}: ${r.network.hosts.join(', ')}`
                            : t('bench.runs.network-offline')
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
            {t('bench.axes.title')}
          </Text>
          <Table striped highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t('bench.axes.axis')}</Table.Th>
                <Table.Th>{t('bench.axes.score')}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {AXIS_ORDER.map((name) => {
                const score = latestAxesRow.axes?.[name]
                return (
                  <Table.Tr key={name}>
                    <Table.Td>
                      <Text size="sm">{t(AXIS_LABEL_BY_NAME[name])}</Text>
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
          <Text fw={600}>{t('bench.history')}</Text>
          <Button size="xs" variant="subtle" onClick={history.reload}>
            {t('bench.history.refresh')}
          </Button>
        </Group>
        {!history.data ? (
          <Loading error={history.error} />
        ) : criteriaRows.length ? (
          <Text size="sm" c="dimmed">
            {t('bench.history.table-note')}
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
          <Empty>{t('bench.history.empty')}</Empty>
        )}
        {history.data?.table ? (
          <details>
            <summary>
              <Text span size="sm" c="dimmed">
                {t('bench.history.table-note')}
              </Text>
            </summary>
            <Code block>{history.data.table}</Code>
          </details>
        ) : null}
      </Card>
    </Page>
  )
}
