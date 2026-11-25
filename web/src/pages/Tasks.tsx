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
import { TASK_TYPES, attempt, short, useLoad } from '../util'
import TaskDetail from './TaskDetail'
import TaskJournal from './TaskJournal'

const FILTERS = [
  { value: 'all', label: 'Tất cả' },
  { value: 'active', label: 'Đang chạy' },
  { value: 'done', label: 'Xong' },
  { value: 'bad', label: 'Lỗi/hủy' },
]

// Giá trị lọc "không rõ nguồn gốc": task cũ chưa được đóng dấu session/môi trường.
const UNKNOWN = '__unknown__'
const UNKNOWN_LABEL = 'Không rõ nguồn gốc'

function distinct(values: (string | null | undefined)[]): string[] {
  return [...new Set(values.filter((v): v is string => !!v))]
}

function matchOrigin(value: string | null | undefined, filter: string | null): boolean {
  if (filter === null) return true
  if (filter === UNKNOWN) return !value
  return value === filter
}

function originOptions(values: (string | null | undefined)[]): { value: string; label: string }[] {
  return [
    { value: UNKNOWN, label: UNKNOWN_LABEL },
    ...distinct(values).map((v) => ({ value: v, label: short(v, 24) })),
  ]
}

export default function Tasks({ parts }: { parts: string[] }) {
  const taskId = parts[0] ?? null
  const { data, error, reload } = useLoad(() => get<{ tasks: TaskRow[] }>('/api/tasks?limit=50'), [], 8000)
  const [filter, setFilter] = useState('all')
  const [tab, setTab] = useState('tasks')
  // Khởi tạo từ query (?session=…&env=…) để link từ chi tiết task lọc được danh sách.
  const [sessionFilter, setSessionFilter] = useState<string | null>(() => new URLSearchParams(location.search).get('session'))
  const [envFilter, setEnvFilter] = useState<string | null>(() => new URLSearchParams(location.search).get('env'))
  // Đồng bộ lại khi route đổi trong SPA: component không remount khi đi từ
  // /tasks/<id> sang /tasks?session=… nên initializer ở trên không chạy lại.
  // Chỉ đồng bộ khi query thật sự đổi — chọn tay trong Select không đổi URL nên không bị reset.
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
      title="Task"
      actions={
        <Button leftSection={<IconPlus size={16} />} onClick={() => setCreating(true)}>
          Task mới
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
          <Tabs.Tab value="tasks">Task</Tabs.Tab>
          <Tabs.Tab value="journal">Nhật ký</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="tasks" pt="md">
        <Grid>
          <Grid.Col span={{ base: 12, md: taskId ? 4 : 12 }}>
            <Card withBorder p="xs">
              <SegmentedControl fullWidth size="xs" data={FILTERS} value={filter} onChange={setFilter} mb="xs" />
              <Group grow mb="xs">
                <Select
                  size="xs"
                  placeholder="Môi trường: tất cả"
                  data={originOptions(allTasks.map((t) => t.environment_id))}
                  value={envFilter}
                  onChange={setEnvFilter}
                  clearable
                />
                <Select
                  size="xs"
                  placeholder="Session: tất cả"
                  data={originOptions(allTasks.map((t) => t.chat_session_id))}
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
                              Môi trường
                            </Text>
                            <Badge
                              size="xs"
                              variant="light"
                              style={{ cursor: 'pointer' }}
                              title="Lọc theo môi trường này"
                              onClick={(e) => {
                                e.stopPropagation()
                                setEnvFilter(t.environment_id ?? UNKNOWN)
                              }}
                            >
                              {t.environment_id ? short(t.environment_id, 20) : UNKNOWN_LABEL}
                            </Badge>
                            <Text size="xs" c="dimmed">
                              →
                            </Text>
                            <Text size="xs" c="dimmed">
                              Session
                            </Text>
                            <Badge
                              size="xs"
                              variant="light"
                              style={{ cursor: 'pointer' }}
                              title="Lọc theo session này"
                              onClick={(e) => {
                                e.stopPropagation()
                                setSessionFilter(t.chat_session_id ?? UNKNOWN)
                              }}
                            >
                              {t.chat_session_id ? short(t.chat_session_id, 20) : UNKNOWN_LABEL}
                            </Badge>
                          </Group>
                        </Table.Td>
                        <Table.Td w={90}>
                          <StateBadge state={t.state} />
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              ) : (
                <Empty>Không có task nào.</Empty>
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
    <Modal opened={opened} onClose={onClose} title="Giao task cho Kite" size="lg">
      <Stack>
        <Select
          label="Repo"
          description="Repo đã thêm ở tab Repo, hoặc gõ đường dẫn một repo Git trên máy Kite"
          data={options}
          value={repo}
          onChange={setRepo}
          searchable
          clearable
          nothingFoundMessage="Gõ đường dẫn đầy đủ rồi chọn"
          onSearchChange={(q) => {
            if (q.startsWith('/') && !options.some((o) => o.value === q)) setRepo(q)
          }}
        />
        <Textarea
          label="Việc cần làm"
          description="Cụ thể: file nào, hành vi mong muốn, cách kiểm tra"
          autosize
          minRows={4}
          value={goal}
          onChange={(e) => setGoal(e.currentTarget.value)}
        />
        <Group grow>
          <Select label="Loại task" data={TASK_TYPES} value={taskType} onChange={(v) => setTaskType(v ?? 'coding')} allowDeselect={false} />
          <TextInput label="Model (để trống: Kite chọn)" value={model} onChange={(e) => setModel(e.currentTarget.value)} />
        </Group>
        <TextInput
          label="Sửa tiếp từ task"
          description="ID một task đã xong: task mới bắt đầu từ kết quả của nó"
          value={continueFrom}
          onChange={(e) => setContinueFrom(e.currentTarget.value)}
        />
        <Checkbox
          label="Bỏ qua thay đổi chưa commit trong repo"
          checked={ignoreDirty}
          onChange={(e) => setIgnoreDirty(e.currentTarget.checked)}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Thôi
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
            Giao task
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

