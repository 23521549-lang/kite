import {
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
  Text,
  TextInput,
  Textarea,
} from '@mantine/core'
import { IconPlus } from '@tabler/icons-react'
import { useState } from 'react'
import { get, post, type TaskRow } from '../api'
import { Empty, Loading, Page, StateBadge } from '../components/common'
import { go } from '../router'
import { TASK_TYPES, attempt, useLoad } from '../util'
import TaskDetail from './TaskDetail'

const FILTERS = [
  { value: 'all', label: 'Tất cả' },
  { value: 'active', label: 'Đang chạy' },
  { value: 'done', label: 'Xong' },
  { value: 'bad', label: 'Lỗi/hủy' },
]

export default function Tasks({ parts }: { parts: string[] }) {
  const taskId = parts[0] ?? null
  const { data, error, reload } = useLoad(() => get<{ tasks: TaskRow[] }>('/api/tasks?limit=50'), [], 8000)
  const [filter, setFilter] = useState('all')
  const [creating, setCreating] = useState(false)
  const tasks = (data?.tasks ?? []).filter((t) =>
    filter === 'all'
      ? true
      : filter === 'active'
        ? ['QUEUED', 'LEASED'].includes(t.state)
        : filter === 'done'
          ? t.state === 'COMPLETED'
          : ['FAILED', 'CANCELLED'].includes(t.state),
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
      <Grid>
        <Grid.Col span={{ base: 12, md: taskId ? 4 : 12 }}>
          <Card withBorder p="xs">
            <SegmentedControl fullWidth size="xs" data={FILTERS} value={filter} onChange={setFilter} mb="xs" />
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

