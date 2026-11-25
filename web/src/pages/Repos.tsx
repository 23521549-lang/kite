import { ActionIcon, Alert, Button, Card, Code, Grid, Group, Stack, Table, Tabs, Text, TextInput, Textarea } from '@mantine/core'
import { IconPlus, IconTrash } from '@tabler/icons-react'
import { useState } from 'react'
import { del, get, post, put, qs } from '../api'
import { ConfirmButton, Empty, KeyValue, Loading, Page } from '../components/common'
import { go } from '../router'
import { useT } from '../i18n'
import { attempt, useLoad } from '../util'

type RepoRow = { id: number; name: string; path: string; url?: string; added_at?: number }
type ReposData = { repos: RepoRow[]; allowed_roots: string[]; memory: { chat: string[] } }

export default function Repos({ parts }: { parts: string[] }) {
  const t = useT()
  const selected = parts[0] ?? null
  const { data, error, reload } = useLoad(() => get<ReposData>('/api/repos'), [])
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [url, setUrl] = useState('')
  const repo = data?.repos.find((r) => String(r.id) === selected)
  return (
    <Page title={t('repos.title')}>
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
                              title={t('repos.remove-title', { name: r.name })}
                              message={t('repos.remove-message')}
                              confirmLabel={t('repos.remove')}
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
                  <Empty>{t('repos.no-repos')}</Empty>
                )}
              </Card>
              <Card withBorder>
                <Text fw={600} mb="xs">
                  {t('repos.add-repo')}
                </Text>
                <Stack gap="xs">
                  <TextInput label={t('repos.name')} placeholder="kite" value={name} onChange={(e) => setName(e.currentTarget.value)} />
                  <TextInput
                    label={t('repos.path')}
                    placeholder="/home/ubuntu/sweetpaw"
                    value={path}
                    onChange={(e) => setPath(e.currentTarget.value)}
                    description={t('repos.path-desc', { roots: data.allowed_roots.join(', ') })}
                  />
                  <TextInput label={t('repos.github-url')} value={url} onChange={(e) => setUrl(e.currentTarget.value)} />
                  <Group>
                    <Button
                      leftSection={<IconPlus size={16} />}
                      disabled={!name || !path}
                      onClick={async () => {
                        const res = await attempt(() => post<{ id: number }>('/api/repos', { name, path, url }), t('repos.added'))
                        if (res) {
                          setName('')
                          setPath('')
                          setUrl('')
                          reload()
                          go(`/repos/${res.id}`)
                        }
                      }}
                    >
                      {t('repos.add')}
                    </Button>
                  </Group>
                </Stack>
              </Card>
              <ChatMemory lines={data.memory.chat} onChanged={reload} />
            </Stack>
          </Grid.Col>
          <Grid.Col span={{ base: 12, md: 7 }}>{repo ? <RepoDetail key={repo.id} repo={repo} /> : <Empty>{t('repos.select-repo')}</Empty>}</Grid.Col>
        </Grid>
      )}
    </Page>
  )
}

function ChatMemory({ lines, onChanged }: { lines: string[]; onChanged: () => void }) {
  const t = useT()
  const [text, setText] = useState('')
  return (
    <Card withBorder>
      <Text fw={600}>{t('repos.memory-title')}</Text>
      <Text size="xs" c="dimmed" mb="xs">
        {t('repos.memory-note')}
      </Text>
      <MemoryLines lines={lines} repo="" onChanged={onChanged} />
      <Group gap="xs" mt="xs" wrap="nowrap">
        <TextInput style={{ flex: 1 }} placeholder={t('repos.memory-placeholder')} value={text} onChange={(e) => setText(e.currentTarget.value)} />
        <Button
          disabled={!text.trim()}
          onClick={async () => {
            if (await attempt(() => post('/api/repos/memory', { text, repo: '' }), t('repos.remembered'))) {
              setText('')
              onChanged()
            }
          }}
        >
          {t('repos.remember')}
        </Button>
      </Group>
    </Card>
  )
}

function MemoryLines({ lines, repo, onChanged }: { lines: string[]; repo: string; onChanged: () => void }) {
  const t = useT()
  if (!lines.length) return <Empty>{t('repos.memory-empty')}</Empty>
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
              title={t('repos.forget-line')}
              onClick={async () => {
                if (!confirm(t('repos.forget-confirm'))) return
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
  const t = useT()
  const status = useLoad(() => get<Record<string, any>>(`/api/repos/status${qs({ repo: repo.path })}`), [repo.path])
  const memory = useLoad(() => get<Record<string, any>>(`/api/repos/memory${qs({ repo: repo.path })}`), [repo.path])
  const config = useLoad(() => get<Record<string, any>>(`/api/repos/config${qs({ repo: repo.path })}`), [repo.path])
  const [draft, setDraft] = useState<string | null>(null)
  const [lesson, setLesson] = useState('')
  const s = status.data
  return (
    <Tabs defaultValue="status">
      <Tabs.List>
        <Tabs.Tab value="status">{t('repos.tab-status')}</Tabs.Tab>
        <Tabs.Tab value="config">{t('repos.tab-config')}</Tabs.Tab>
        <Tabs.Tab value="memory">{t('repos.tab-memory')}</Tabs.Tab>
      </Tabs.List>
      <Tabs.Panel value="status" pt="sm">
        <Card withBorder>
          {!s ? (
            <Loading error={status.error} />
          ) : (
            <Stack>
              <KeyValue
                rows={[
                  [t('repos.detail.path'), <Code key="p">{s.repo}</Code>],
                  [t('repos.detail.branch'), s.branch],
                  [t('repos.detail.head'), s.head],
                  ...(repo.url ? ([[t('repos.detail.github'), repo.url]] as [string, string][]) : []),
                ]}
              />
              <div>
                <Text size="sm" fw={600}>
                  {t('repos.detail.uncommitted')}
                </Text>
                <Code block>{s.changes}</Code>
              </div>
              {s.diff_stat ? (
                <div>
                  <Text size="sm" fw={600}>
                    {t('repos.detail.diff-stat')}
                  </Text>
                  <Code block>{s.diff_stat}</Code>
                </div>
              ) : null}
              <div>
                <Text size="sm" fw={600}>
                  {t('repos.detail.recent-commits')}
                </Text>
                <Code block>{s.recent_commits}</Code>
              </div>
              <Group>
                <Button size="xs" variant="light" onClick={() => status.reload()}>
                  {t('repos.detail.refresh')}
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
                {t('repos.detail.source')}: {config.data.source}
                {config.data.file ? ` (${config.data.file})` : ''}
              </Text>
              {config.data.source?.startsWith('.kite.yml') ? (
                <Alert color="blue">{t('repos.detail.kite-yml')}</Alert>
              ) : null}
              <Textarea
                className="mono"
                autosize
                minRows={8}
                value={draft ?? config.data.config ?? config.data.suggestion ?? ''}
                onChange={(e) => setDraft(e.currentTarget.value)}
                description={t('repos.detail.config-desc')}
              />
              <Group>
                <Button
                  disabled={draft === null}
                  onClick={async () => {
                    const res = await attempt(() => put('/api/repos/config', { repo: repo.path, config: draft }), t('repos.detail.saved'))
                    if (res) {
                      setDraft(null)
                      config.reload()
                    }
                  }}
                >
                  {t('repos.detail.save-check')}
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
                {t('repos.detail.lessons')}
              </Text>
              <MemoryLines lines={memory.data.lessons ?? []} repo={repo.path} onChanged={memory.reload} />
              <Group gap="xs" wrap="nowrap">
                <TextInput style={{ flex: 1 }} placeholder={t('repos.detail.lesson-placeholder')} value={lesson} onChange={(e) => setLesson(e.currentTarget.value)} />
                <Button
                  disabled={!lesson.trim()}
                  onClick={async () => {
                    if (await attempt(() => post('/api/repos/memory', { text: lesson, repo: repo.path }), t('repos.remembered'))) {
                      setLesson('')
                      memory.reload()
                    }
                  }}
                >
                  {t('repos.remember')}
                </Button>
              </Group>
            </Stack>
          )}
        </Card>
      </Tabs.Panel>
    </Tabs>
  )
}
