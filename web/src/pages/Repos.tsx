import { ActionIcon, Alert, Button, Card, Code, Grid, Group, Stack, Table, Tabs, Text, TextInput, Textarea } from '@mantine/core'
import { IconPlus, IconTrash } from '@tabler/icons-react'
import { useState } from 'react'
import { del, get, post, put, qs } from '../api'
import { ConfirmButton, Empty, KeyValue, Loading, Page } from '../components/common'
import { go } from '../router'
import { attempt, useLoad } from '../util'

type RepoRow = { id: number; name: string; path: string; url?: string; added_at?: number }
type ReposData = { repos: RepoRow[]; allowed_roots: string[]; memory: { chat: string[] } }

export default function Repos({ parts }: { parts: string[] }) {
  const selected = parts[0] ?? null
  const { data, error, reload } = useLoad(() => get<ReposData>('/api/repos'), [])
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [url, setUrl] = useState('')
  const repo = data?.repos.find((r) => String(r.id) === selected)
  return (
    <Page title="Repo">
      {!data ? (
        <Loading error={error} />
      ) : (
        <Grid>
          <Grid.Col span={{ base: 12, md: 5 }}>
            <Stack>
              <Card withBorder>
                {data.repos.length ? (
                  <Table highlightOnHover>
                    <Table.Tbody>
                      {data.repos.map((r) => (
                        <Table.Tr
                          key={r.id}
                          className="clickable"
                          onClick={() => go(`/repos/${r.id}`)}
                          style={String(r.id) === selected ? { background: 'var(--mantine-color-default-hover)' } : undefined}
                        >
                          <Table.Td>
                            <Text fw={600} size="sm">
                              {r.name}
                            </Text>
                            <Text size="xs" c="dimmed" className="mono">
                              {r.path}
                            </Text>
                          </Table.Td>
                          <Table.Td w={40} onClick={(e) => e.stopPropagation()}>
                            <ConfirmButton
                              size="compact-xs"
                              variant="subtle"
                              color="red"
                              label={<IconTrash size={14} />}
                              title={`Bỏ repo ${r.name} khỏi danh sách?`}
                              message="Chỉ bỏ khỏi danh sách của web; repo trên ổ đĩa không bị đụng tới."
                              confirmLabel="Bỏ"
                              onConfirm={async () => {
                                await attempt(() => del(`/api/repos/${r.id}`))
                                reload()
                                go('/repos')
                              }}
                            />
                          </Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                ) : (
                  <Empty>Chưa thêm repo nào.</Empty>
                )}
              </Card>
              <Card withBorder>
                <Text fw={600} mb="xs">
                  Thêm repo
                </Text>
                <Stack gap="xs">
                  <TextInput label="Tên" placeholder="kite" value={name} onChange={(e) => setName(e.currentTarget.value)} />
                  <TextInput
                    label="Đường dẫn trên máy Kite"
                    placeholder="/home/ubuntu/sweetpaw"
                    value={path}
                    onChange={(e) => setPath(e.currentTarget.value)}
                    description={`Một repo Git có ít nhất một commit, nằm trong: ${data.allowed_roots.join(', ')}`}
                  />
                  <TextInput label="URL GitHub (không bắt buộc)" value={url} onChange={(e) => setUrl(e.currentTarget.value)} />
                  <Group>
                    <Button
                      leftSection={<IconPlus size={16} />}
                      disabled={!name || !path}
                      onClick={async () => {
                        const res = await attempt(() => post<{ id: number }>('/api/repos', { name, path, url }), 'Đã thêm repo')
                        if (res) {
                          setName('')
                          setPath('')
                          setUrl('')
                          reload()
                          go(`/repos/${res.id}`)
                        }
                      }}
                    >
                      Thêm
                    </Button>
                  </Group>
                </Stack>
              </Card>
              <ChatMemory lines={data.memory.chat} onChanged={reload} />
            </Stack>
          </Grid.Col>
          <Grid.Col span={{ base: 12, md: 7 }}>{repo ? <RepoDetail key={repo.id} repo={repo} /> : <Empty>Chọn một repo để xem.</Empty>}</Grid.Col>
        </Grid>
      )}
    </Page>
  )
}

function ChatMemory({ lines, onChanged }: { lines: string[]; onChanged: () => void }) {
  const [text, setText] = useState('')
  return (
    <Card withBorder>
      <Text fw={600}>Điều chat luôn nhớ</Text>
      <Text size="xs" c="dimmed" mb="xs">
        Có hiệu lực từ phiên chat mới.
      </Text>
      <MemoryLines lines={lines} repo="" onChanged={onChanged} />
      <Group gap="xs" mt="xs" wrap="nowrap">
        <TextInput style={{ flex: 1 }} placeholder="Ví dụ: luôn hỏi trước khi giao task sửa tiếp" value={text} onChange={(e) => setText(e.currentTarget.value)} />
        <Button
          disabled={!text.trim()}
          onClick={async () => {
            if (await attempt(() => post('/api/repos/memory', { text, repo: '' }), 'Đã ghi nhớ')) {
              setText('')
              onChanged()
            }
          }}
        >
          Nhớ
        </Button>
      </Group>
    </Card>
  )
}

function MemoryLines({ lines, repo, onChanged }: { lines: string[]; repo: string; onChanged: () => void }) {
  if (!lines.length) return <Empty>Chưa có gì.</Empty>
  return (
    <Stack gap={4}>
      {lines.map((line) => {
        const n = Number(line.split('.')[0])
        return (
          <Group key={line} gap="xs" wrap="nowrap" align="flex-start">
            <Text size="sm" style={{ flex: 1 }}>
              {line}
            </Text>
            <ActionIcon
              size="sm"
              variant="subtle"
              color="red"
              title="Quên dòng này"
              onClick={async () => {
                if (!confirm('Quên dòng này?')) return
                await attempt(() => del(`/api/repos/memory/${n}${qs({ repo })}`))
                onChanged()
              }}
            >
              <IconTrash size={14} />
            </ActionIcon>
          </Group>
        )
      })}
    </Stack>
  )
}

function RepoDetail({ repo }: { repo: RepoRow }) {
  const status = useLoad(() => get<Record<string, any>>(`/api/repos/status${qs({ repo: repo.path })}`), [repo.path])
  const memory = useLoad(() => get<Record<string, any>>(`/api/repos/memory${qs({ repo: repo.path })}`), [repo.path])
  const config = useLoad(() => get<Record<string, any>>(`/api/repos/config${qs({ repo: repo.path })}`), [repo.path])
  const [draft, setDraft] = useState<string | null>(null)
  const [lesson, setLesson] = useState('')
  const s = status.data
  return (
    <Tabs defaultValue="status">
      <Tabs.List>
        <Tabs.Tab value="status">Tình trạng</Tabs.Tab>
        <Tabs.Tab value="config">Cài và test</Tabs.Tab>
        <Tabs.Tab value="memory">Điều Kite nhớ</Tabs.Tab>
      </Tabs.List>
      <Tabs.Panel value="status" pt="sm">
        <Card withBorder>
          {!s ? (
            <Loading error={status.error} />
          ) : (
            <Stack>
              <KeyValue
                rows={[
                  ['Đường dẫn', <Code key="p">{s.repo}</Code>],
                  ['Nhánh', s.branch],
                  ['HEAD', s.head],
                  ...(repo.url ? ([['GitHub', repo.url]] as [string, string][]) : []),
                ]}
              />
              <div>
                <Text size="sm" fw={600}>
                  Thay đổi chưa commit
                </Text>
                <Code block>{s.changes}</Code>
              </div>
              {s.diff_stat ? (
                <div>
                  <Text size="sm" fw={600}>
                    diff --stat
                  </Text>
                  <Code block>{s.diff_stat}</Code>
                </div>
              ) : null}
              <div>
                <Text size="sm" fw={600}>
                  Commit gần đây
                </Text>
                <Code block>{s.recent_commits}</Code>
              </div>
              <Group>
                <Button size="xs" variant="light" onClick={() => status.reload()}>
                  Làm mới
                </Button>
              </Group>
            </Stack>
          )}
        </Card>
      </Tabs.Panel>
      <Tabs.Panel value="config" pt="sm">
        <Card withBorder>
          {!config.data ? (
            <Loading error={config.error} />
          ) : (
            <Stack>
              <Text size="sm" c="dimmed">
                Nguồn: {config.data.source}
                {config.data.file ? ` (${config.data.file})` : ''}
              </Text>
              {config.data.source?.startsWith('.kite.yml') ? (
                <Alert color="blue">Repo có .kite.yml được commit: Kite dùng file đó; bản lưu riêng ở đây không có tác dụng.</Alert>
              ) : null}
              <Textarea
                className="mono"
                autosize
                minRows={8}
                value={draft ?? config.data.config ?? config.data.suggestion ?? ''}
                onChange={(e) => setDraft(e.currentTarget.value)}
                description="YAML: cách cài thư viện và chạy test (Kite chạy trong sandbox, không có mạng khi test)"
              />
              <Group>
                <Button
                  disabled={draft === null}
                  onClick={async () => {
                    const res = await attempt(() => put('/api/repos/config', { repo: repo.path, config: draft }), 'Đã lưu')
                    if (res) {
                      setDraft(null)
                      config.reload()
                    }
                  }}
                >
                  Kiểm tra và lưu
                </Button>
              </Group>
            </Stack>
          )}
        </Card>
      </Tabs.Panel>
      <Tabs.Panel value="memory" pt="sm">
        <Card withBorder>
          {!memory.data ? (
            <Loading error={memory.error} />
          ) : (
            <Stack>
              <Text size="sm" c="dimmed">
                Bài học Kite đưa cho mọi task sau trên repo này.
              </Text>
              <MemoryLines lines={memory.data.lessons ?? []} repo={repo.path} onChanged={memory.reload} />
              <Group gap="xs" wrap="nowrap">
                <TextInput style={{ flex: 1 }} placeholder="Ví dụ: test chạy bằng pytest -q tests/unit" value={lesson} onChange={(e) => setLesson(e.currentTarget.value)} />
                <Button
                  disabled={!lesson.trim()}
                  onClick={async () => {
                    if (await attempt(() => post('/api/repos/memory', { text: lesson, repo: repo.path }), 'Đã ghi nhớ')) {
                      setLesson('')
                      memory.reload()
                    }
                  }}
                >
                  Nhớ
                </Button>
              </Group>
            </Stack>
          )}
        </Card>
      </Tabs.Panel>
    </Tabs>
  )
}
