import {
  Badge,
  Button,
  Card,
  Checkbox,
  Grid,
  Group,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
  Textarea,
} from '@mantine/core'
import { IconPlus } from '@tabler/icons-react'
import { useEffect, useRef, useState } from 'react'
import { get, post, type TaskRow } from '../api'
import { Empty, Loading, Page, StateBadge } from '../components/common'
import { go } from '../router'
import { attempt, short, useLoad } from '../util'
import { useT } from '../i18n'
import TaskDetail from './TaskDetail'
import TaskJournal from './TaskJournal'

// The "unknown origin" filter value: old tasks were never stamped with a session/environment.
const UNKNOWN = '__unknown__'

function distinct(values: (string | null | undefined)[]): string[] {
  return [...new Set(values.filter((v): v is string => !!v))]
}

function matchOrigin(value: string | null | undefined, filter: string | null): boolean {
  if (filter === null) return true
  if (filter === UNKNOWN) return !value
  return value === filter
}

function originOptions(values: (string | null | undefined)[], unknownLabel: string): { value: string; label: string }[] {
  return [
    { value: UNKNOWN, label: unknownLabel },
    ...distinct(values).map((v) => ({ value: v, label: short(v, 24) })),
  ]
}

export default function Tasks({ parts }: { parts: string[] }) {
  const t = useT()
  const unknownLabel = t('tasks.origin.unknown')
  const envLabel = t('tasks.origin.env')
  const sessionLabel = t('tasks.origin.session')
  const filterEnvTitle = t('tasks.origin.filter-env')
  const filterSessionTitle = t('tasks.origin.filter-session')
  const FILTERS = [
    { value: 'all', label: t('tasks.filter.all') },
    { value: 'active', label: t('tasks.filter.active') },
    { value: 'done', label: t('tasks.filter.done') },
    { value: 'bad', label: t('tasks.filter.bad') },
  ]
  const taskId = parts[0] ?? null
  const { data, error, reload } = useLoad(() => get<{ tasks: TaskRow[] }>('/api/tasks?limit=50'), [], 8000)
  const [filter, setFilter] = useState('all')
  const [tab, setTab] = useState('tasks')
  // Init from the query (?session=…&env=…) so links from a task detail filter the list.
  const [sessionFilter, setSessionFilter] = useState<string | null>(() => new URLSearchParams(location.search).get('session'))
  const [envFilter, setEnvFilter] = useState<string | null>(() => new URLSearchParams(location.search).get('env'))
  // Re-sync when the route changes inside the SPA: the component does not remount
  // when going from /tasks/<id> to /tasks?session=…, so the initializer above does not re-run.
  // Only sync when the query really changed — picking by hand in the Select does not
  // change the URL, so it is not reset.
  const lastQuery = useRef(location.search)
  useEffect(() => {
    if (location.search !== lastQuery.current) {
      lastQuery.current = location.search
      const q = new URLSearchParams(location.search)
      setSessionFilter(q.get('session'))
      setEnvFilter(q.get('env'))
    }
  })
  const [creating, setCreating] = useState(false)
  const allTasks = data?.tasks ?? []
  const tasks = allTasks.filter(
    (t) =>
      (filter === 'all'
        ? true
        : filter === 'active'
          ? ['QUEUED', 'LEASED'].includes(t.state)
          : filter === 'done'
            ? t.state === 'COMPLETED'
            : ['FAILED', 'CANCELLED'].includes(t.state)) &&
      matchOrigin(t.chat_session_id, sessionFilter) &&
      matchOrigin(t.environment_id, envFilter),
  )
  return (
    <Page
      title={t('tasks.title')}
      actions={
        <Button leftSection={<IconPlus size={16} />} onClick={() => setCreating(true)}>
          {t('tasks.new-task')}
        </Button>
      }
    >
      <NewTask
        opened={creating}
        onClose={() => setCreating(false)}
        onCreated={(id) => {
          setCreating(false)
          reload()
          go(`/tasks/${encodeURIComponent(id)}`)
        }}
      />
      <Tabs value={tab} onChange={(v) => setTab(v ?? 'tasks')}>
        <Tabs.List>
          <Tabs.Tab value="tasks">{t('tasks.tab.tasks')}</Tabs.Tab>
          <Tabs.Tab value="journal">{t('tasks.tab.journal')}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="tasks" pt="md">
        <Grid>
          <Grid.Col span={{ base: 12, md: taskId ? 4 : 12 }}>
            <Card withBorder p="xs">
              <SegmentedControl fullWidth size="xs" data={FILTERS} value={filter} onChange={setFilter} mb="xs" />
              <Group grow mb="xs">
                <Select
                  size="xs"
                  placeholder={t('tasks.filter.env-all')}
                  data={originOptions(allTasks.map((t) => t.environment_id), unknownLabel)}
                  value={envFilter}
                  onChange={setEnvFilter}
                  clearable
                />
                <Select
                  size="xs"
                  placeholder={t('tasks.filter.session-all')}
                  data={originOptions(allTasks.map((t) => t.chat_session_id), unknownLabel)}
                  value={sessionFilter}
                  onChange={setSessionFilter}
                  clearable
                />
              </Group>
              {!data ? (
                <Loading error={error} />
              ) : tasks.length ? (
                <Table highlightOnHover>
                  <Table.Tbody>
                    {tasks.map((t) => (
                      <Table.Tr
                        key={t.task_id}
                        className="clickable"
                        onClick={() => go(`/tasks/${encodeURIComponent(t.task_id)}`)}
                        style={t.task_id === taskId ? { background: 'var(--mantine-color-default-hover)' } : undefined}
                      >
                        <Table.Td>
                          <Text size="sm" lineClamp={2}>
                            {t.goal || t.task_id}
                          </Text>
                          <Text size="xs" c="dimmed">
                            {t.task_id} · {t.task_type} · {t.created}
                          </Text>
                          <Group gap={4} mt={2}>
                            <Text size="xs" c="dimmed">
                              {envLabel}
                            </Text>
                            <Badge
                              size="xs"
                              variant="light"
                              style={{ cursor: 'pointer' }}
                              title={filterEnvTitle}
                              onClick={(e) => {
                                e.stopPropagation()
                                setEnvFilter(t.environment_id ?? UNKNOWN)
                              }}
                            >
                              {t.environment_id ? short(t.environment_id, 20) : unknownLabel}
                            </Badge>
                            <Text size="xs" c="dimmed">
                              →
                            </Text>
                            <Text size="xs" c="dimmed">
                              {sessionLabel}
                            </Text>
                            <Badge
                              size="xs"
                              variant="light"
                              style={{ cursor: 'pointer' }}
                              title={filterSessionTitle}
                              onClick={(e) => {
                                e.stopPropagation()
                                setSessionFilter(t.chat_session_id ?? UNKNOWN)
                              }}
                            >
                              {t.chat_session_id ? short(t.chat_session_id, 20) : unknownLabel}
                            </Badge>
                          </Group>
                        </Table.Td>
                        <Table.Td w={128}>
                          <StateBadge state={t.state} />
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              ) : (
                <Empty>{t('tasks.no-tasks')}</Empty>
              )}
            </Card>
          </Grid.Col>
          {taskId ? (
            <Grid.Col span={{ base: 12, md: 8 }}>
              <TaskDetail key={taskId} taskId={taskId} onChanged={reload} />
            </Grid.Col>
          ) : null}
        </Grid>
        </Tabs.Panel>
        <Tabs.Panel value="journal" pt="md">
          <TaskJournal tasks={allTasks} />
        </Tabs.Panel>
      </Tabs>
    </Page>
  )
}

function NewTask({ opened, onClose, onCreated }: { opened: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const t = useT()
  const TASK_TYPE_OPTIONS = [
    { value: 'coding', label: t('tasks.new.type.coding') },
    { value: 'analysis', label: t('tasks.new.type.analysis') },
    { value: 'review', label: t('tasks.new.type.review') },
    { value: 'docs', label: t('tasks.new.type.docs') },
    { value: 'vision', label: t('tasks.new.type.vision') },
  ]
  const repos = useLoad(() => get<{ repos: { id: number; name: string; path: string }[]; allowed_roots: string[] }>('/api/repos'), [opened])
  const [repo, setRepo] = useState<string | null>(null)
  const [goal, setGoal] = useState('')
  const [taskType, setTaskType] = useState('coding')
  const [model, setModel] = useState('')
  const [continueFrom, setContinueFrom] = useState('')
  const [ignoreDirty, setIgnoreDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const options = (repos.data?.repos ?? []).map((r) => ({ value: r.path, label: `${r.name} · ${r.path}` }))
  return (
    <Modal opened={opened} onClose={onClose} title={t('tasks.new.title')} size="lg">
      <Stack>
        <Select
          label={t('tasks.new.repo')}
          description={t('tasks.new.repo-desc')}
          data={options}
          value={repo}
          onChange={setRepo}
          searchable
          clearable
          nothingFoundMessage={t('tasks.new.repo-nothing-found')}
          onSearchChange={(q) => {
            if (q.startsWith('/') && !options.some((o) => o.value === q)) setRepo(q)
          }}
        />
        <Textarea
          label={t('tasks.new.goal')}
          description={t('tasks.new.goal-desc')}
          autosize
          minRows={4}
          value={goal}
          onChange={(e) => setGoal(e.currentTarget.value)}
        />
        <Group grow>
          <Select label={t('tasks.new.type')} data={TASK_TYPE_OPTIONS} value={taskType} onChange={(v) => setTaskType(v ?? 'coding')} allowDeselect={false} />
          <TextInput label={t('tasks.new.model')} value={model} onChange={(e) => setModel(e.currentTarget.value)} />
        </Group>
        <TextInput
          label={t('tasks.new.continue-from')}
          description={t('tasks.new.continue-from-desc')}
          value={continueFrom}
          onChange={(e) => setContinueFrom(e.currentTarget.value)}
        />
        <Checkbox
          label={t('tasks.new.ignore-dirty')}
          checked={ignoreDirty}
          onChange={(e) => setIgnoreDirty(e.currentTarget.checked)}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            loading={busy}
            disabled={!repo || !goal.trim()}
            onClick={async () => {
              setBusy(true)
              const res = await attempt(() =>
                post<{ task_id: string }>('/api/tasks', {
                  repo,
                  goal,
                  task_type: taskType,
                  model: model.trim() || null,
                  continue_from: continueFrom.trim(),
                  ignore_uncommitted: ignoreDirty,
                }),
              )
              setBusy(false)
              if (res?.task_id) onCreated(res.task_id)
            }}
          >
            {t('tasks.new.assign')}
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

