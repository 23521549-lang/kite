import { Alert, Button, Card, Checkbox, Code, Group, ScrollArea, Select, Stack, Table, Text, TextInput } from '@mantine/core'
import { useState } from 'react'
import { get, post, type WorkerRow } from '../api'
import { Empty, Loading, Page } from '../components/common'
import { useT } from '../i18n'
import { attempt, useLoad } from '../util'

export default function Bench() {
  const t = useT()
  const SUITES = [
    { value: 'code', label: t('bench.suite.code') },
    { value: 'lead', label: t('bench.suite.lead') },
    { value: 'speed', label: t('bench.suite.speed') },
  ]
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
              {SUITES.map((s) => (
                <Checkbox key={s.value} value={s.value} label={s.label} />
              ))}
            </Group>
          </Checkbox.Group>
          <Group>
            <Button
              disabled={!label.trim() || !suites.length}
              onClick={async () => {
                const res = await attempt(() => post('/api/bench/run', { label, suites: suites.join(','), models, worker: worker ?? '' }), t('bench.new.started'))
                if (res) setStarted(res)
              }}
            >
              {t('bench.new.run-bg')}
            </Button>
            <Text size="xs" c="dimmed">
              {t('bench.new.note')}
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
          <Text fw={600}>{t('bench.history')}</Text>
          <Button size="xs" variant="subtle" onClick={history.reload}>
            {t('bench.history.refresh')}
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
