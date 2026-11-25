// Environments (Gói 2): replaces the old Repo tab. A Default card is fixed
// (cannot be deleted); other cards are GitHub repos Kite clones/manages or
// existing folders. Each card shows display name, owner/repo, branch, HEAD,
// clean/dirty, last sync, session/task counts, spend and lesson count.
// Detail route /environments/<id> has tabs: Overview | Sessions & Tasks |
// Context | Lessons. The back end (GET/POST /api/environments, sync, PAT)
// is built by worker (a) in parallel; fields are optional and the page
// degrades gracefully when the server returns a subset.
import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Grid,
  Group,
  PasswordInput,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
} from '@mantine/core'
import { IconBrandGithub, IconFolder, IconPlus, IconRefresh, IconTrash } from '@tabler/icons-react'
import { useState } from 'react'
import {
  approveExperience,
  createChatSession,
  createEnvironment,
  deleteEnvironment,
  deleteEnvironmentPat,
  envDisplayName,
  envLastSync,
  envLessonCount,
  envRepoLabel,
  envSessionCount,
  envTaskCount,
  getExperience,
  getExperienceDiff,
  getRepoStatus,
  listChatSessions,
  listEnvironments,
  listSessionMoves,
  rejectExperience,
  saveEnvironmentPat,
  syncEnvironment,
  type EnvironmentRow,
  type ExperienceDoc,
  type KiteChatSession,
  type RepoStatus,
  type SessionMove,
  type TaskRow,
} from '../api'
import { ConfirmButton, Empty, KeyValue, Loading, Page, StateBadge } from '../components/common'
import { useEnvStrings } from '../i18n/env'
import { go } from '../router'
import { ago, attempt, fmtTime, num, useLoad, usd } from '../util'
import { get } from '../api'

const DEFAULT_ENV: EnvironmentRow = { id: 'default', name: 'Default', kind: 'default' }

function withDefault(envs: EnvironmentRow[]): EnvironmentRow[] {
  if (envs.some((e) => e.id === 'default')) return envs
  return [DEFAULT_ENV, ...envs]
}

export default function Environments({ parts }: { parts: string[] }) {
  const t = useEnvStrings()
  const id = parts[0] ?? null
  const tab = parts[1] ?? 'overview'
  const { data, error, reload } = useLoad(() => listEnvironments(), [])
  const envs = data ? withDefault(data) : null
  // GET /api/environments carries no counts, so derive them here from the
  // session registry and the task list.
  const sessionsAll = useLoad(() => listChatSessions().catch(() => [] as KiteChatSession[]), [])
  const tasksAll = useLoad(() => get<{ tasks: TaskRow[] }>('/api/tasks?limit=50').catch(() => ({ tasks: [] as TaskRow[] })), [])
  const sessionCounts: Record<string, number> = {}
  for (const s of sessionsAll.data ?? []) {
    if (s.environment_id) sessionCounts[s.environment_id] = (sessionCounts[s.environment_id] ?? 0) + 1
  }
  const taskCounts: Record<string, number> = {}
  for (const tk of tasksAll.data?.tasks ?? []) {
    if (tk.environment_id) taskCounts[tk.environment_id] = (taskCounts[tk.environment_id] ?? 0) + 1
  }
  const env = id ? envs?.find((e) => e.id === id) : null
  if (!envs) return <Page title={t['env.title']}>{<Loading error={error} />}</Page>
  if (id && !env)
    return (
      <Page title={t['env.title']}>
        <Alert color="yellow">{t['env.empty']}</Alert>
        <Button mt="md" variant="light" onClick={() => go('/environments')}>
          {t['env.detail-back']}
        </Button>
      </Page>
    )
  if (env) return <EnvDetail key={env.id} env={env} tab={tab} onChanged={reload} />
  return (
    <Page
      title={t['env.title']}
      actions={
        <Button leftSection={<IconPlus size={16} />} onClick={() => go('/environments/new')}>
          {t['env.new']}
        </Button>
      }
    >
      <Text size="sm" c="dimmed" mb="md">
        {t['env.subtitle']}
      </Text>
      {parts[0] === 'new' ? <AddEnvironment onAdded={(e) => go(`/environments/${encodeURIComponent(e.id)}`)} onCancel={() => go('/environments')} /> : null}
      {envs.length <= 1 ? <Empty>{t['env.empty']}</Empty> : null}
      <Grid>
        {envs.map((e) => (
          <Grid.Col key={e.id} span={{ base: 12, md: 6 }}>
            <EnvCard env={e} sessionCount={sessionCounts[e.id]} taskCount={taskCounts[e.id]} onChanged={reload} />
          </Grid.Col>
        ))}
      </Grid>
    </Page>
  )
}

function EnvCard({ env, sessionCount, taskCount, onChanged }: { env: EnvironmentRow; sessionCount?: number; taskCount?: number; onChanged: () => void }) {
  const t = useEnvStrings()
  const isDefault = env.id === 'default'
  const repoLabel = envRepoLabel(env)
  const [syncing, setSyncing] = useState(false)
  const shownSessions = sessionCount ?? envSessionCount(env)
  const shownTasks = taskCount ?? envTaskCount(env)
  async function openChat() {
    const res = await attempt(() => createChatSession({ environment_id: env.id }))
    if (res?.kite_session_id) go(`/chat/${encodeURIComponent(res.kite_session_id)}`)
  }
  return (
    <Card withBorder>
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <div style={{ minWidth: 0 }}>
          <Group gap="xs">
            <Text fw={600} truncate>
              {isDefault ? t['env.default-name'] : envDisplayName(env)}
            </Text>
            <Badge variant="light" size="sm">
              {env.kind === 'github' ? t['env.kind-github'] : env.kind === 'local' ? t['env.kind-local'] : t['env.kind-default']}
            </Badge>
          </Group>
          {isDefault ? (
            <Text size="sm" c="dimmed">
              {t['env.default-desc']}
            </Text>
          ) : (
            <Text size="sm" c="dimmed" truncate>
              {repoLabel ?? env.path ?? ''}
            </Text>
          )}
        </div>
        {!isDefault ? (
          <ConfirmButton
            size="compact-xs"
            variant="subtle"
            color="red"
            label={<IconTrash size={14} />}
            title={t['env.delete-title']}
            message={t['env.delete-message']}
            confirmLabel={t['env.delete']}
            onConfirm={async () => {
              const res = await attempt(() => deleteEnvironment(env.id))
              if (res !== undefined) onChanged()
            }}
          />
        ) : null}
      </Group>
      {!isDefault ? (
        <Stack gap={2} mt="sm">
          <Group gap="xs">
            <Text size="sm">
              {t['env.branch']}: <Code>{env.branch ?? '—'}</Code>
            </Text>
            <Text size="sm">
              {t['env.head']}: <Code>{env.head ? env.head.slice(0, 8) : '—'}</Code>
            </Text>
            {env.clean === true ? (
              <Badge color="teal" size="sm">
                {t['env.clean']}
              </Badge>
            ) : env.clean === false ? (
              <Badge color="orange" size="sm">
                {t['env.dirty']}
              </Badge>
            ) : (
              <Badge color="gray" size="sm">
                {t['env.clean-unknown']}
              </Badge>
            )}
          </Group>
          <Text size="sm" c="dimmed">
            {t['env.last-sync']}: {envLastSync(env) ? ago(envLastSync(env)) : t['env.never-synced']}
          </Text>
          <Text size="sm" c="dimmed">
            {num(shownSessions)} {t['env.sessions']} · {num(shownTasks)} {t['env.tasks']} · {num(envLessonCount(env))}{' '}
            {t['env.lessons']}
            {env.spend_month_usd !== null && env.spend_month_usd !== undefined ? ` · ${t['env.spend-month']}: ${usd(env.spend_month_usd)}` : ''}
          </Text>
        </Stack>
      ) : null}
      <Group gap="xs" mt="md">
        <Button size="xs" onClick={openChat}>
          {t['env.open-chat']}
        </Button>
        {!isDefault ? (
          <Button
            size="xs"
            variant="light"
            leftSection={<IconRefresh size={14} />}
            loading={syncing}
            onClick={async () => {
              setSyncing(true)
              await attempt(() => syncEnvironment(env.id), t['env.sync-done'])
              setSyncing(false)
              onChanged()
            }}
          >
            {t['env.sync']}
          </Button>
        ) : null}
        <Button size="xs" variant="light" onClick={() => go(`/environments/${encodeURIComponent(env.id)}/sessions`)}>
          {t['env.view-sessions']}
        </Button>
        <Button size="xs" variant="light" onClick={() => go(`/environments/${encodeURIComponent(env.id)}/sessions`)}>
          {t['env.view-tasks']}
        </Button>
        <Button size="xs" variant="light" onClick={() => go(`/environments/${encodeURIComponent(env.id)}/lessons`)}>
          {t['env.view-lessons']}
        </Button>
      </Group>
    </Card>
  )
}

function AddEnvironment({ onAdded, onCancel }: { onAdded: (e: EnvironmentRow) => void; onCancel: () => void }) {
  const t = useEnvStrings()
  const [kind, setKind] = useState<'github' | 'local'>('github')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  // The back end (EnvIn) requires a folder path that is already a Git repo;
  // a bare GitHub URL alone is not accepted (see report to the lead).
  const valid = name.trim() && path.trim()
  return (
    <Card withBorder mb="md">
      <Text fw={600} mb="xs">
        {t['env.add-title']}
      </Text>
      <Stack gap="xs">
        <TextInput label={t['env.form-name']} placeholder={t['env.form-name-ph']} value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <Select
          label={t['env.form-kind']}
          data={[
            { value: 'github', label: t['env.kind-github'] },
            { value: 'local', label: t['env.kind-local'] },
          ]}
          value={kind}
          onChange={(v) => setKind(v === 'local' ? 'local' : 'github')}
          allowDeselect={false}
        />
        <TextInput
          label={t['env.form-path']}
          placeholder={t['env.form-path-ph']}
          description={kind === 'github' ? t['env.form-path-desc-github'] : t['env.form-path-desc-local']}
          value={path}
          onChange={(e) => setPath(e.currentTarget.value)}
          leftSection={<IconFolder size={16} />}
        />
        {kind === 'github' ? (
          <>
            <TextInput
              label={t['env.form-url']}
              placeholder={t['env.form-url-ph']}
              value={url}
              onChange={(e) => setUrl(e.currentTarget.value)}
              leftSection={<IconBrandGithub size={16} />}
            />
            <Text size="xs" c="dimmed">
              {t['env.form-pat-after']}
            </Text>
          </>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onCancel}>
            {t['env.form-cancel']}
          </Button>
          <Button
            leftSection={<IconPlus size={16} />}
            loading={busy}
            disabled={!valid}
            onClick={async () => {
              setBusy(true)
              const res = await attempt(
                () =>
                  createEnvironment({
                    name: name.trim(),
                    path: path.trim(),
                    url: kind === 'github' ? url.trim() || undefined : undefined,
                  }),
                t['env.form-added'],
              )
              setBusy(false)
              if (res) onAdded(res)
            }}
          >
            {t['env.form-add']}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

// ---------------------------------------------------------------- detail
function EnvDetail({ env, tab, onChanged }: { env: EnvironmentRow; tab: string; onChanged: () => void }) {
  const t = useEnvStrings()
  const active = ['overview', 'sessions', 'context', 'lessons'].includes(tab) ? tab : 'overview'
  return (
    <Page
      title={env.id === 'default' ? t['env.default-name'] : envDisplayName(env)}
      actions={
        <Button variant="light" onClick={() => go('/environments')}>
          {t['env.detail-back']}
        </Button>
      }
    >
      <Tabs value={active} onChange={(v) => v && go(`/environments/${encodeURIComponent(env.id)}${v === 'overview' ? '' : `/${v}`}`)}>
        <Tabs.List>
          <Tabs.Tab value="overview">{t['env.detail-overview']}</Tabs.Tab>
          <Tabs.Tab value="sessions">{t['env.detail-sessions-tasks']}</Tabs.Tab>
          <Tabs.Tab value="context">{t['env.detail-context']}</Tabs.Tab>
          <Tabs.Tab value="lessons">{t['env.detail-lessons']}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="overview" pt="sm">
          <EnvOverview env={env} onChanged={onChanged} />
        </Tabs.Panel>
        <Tabs.Panel value="sessions" pt="sm">
          <EnvSessionsTasks env={env} />
        </Tabs.Panel>
        <Tabs.Panel value="context" pt="sm">
          <EnvContext env={env} />
        </Tabs.Panel>
        <Tabs.Panel value="lessons" pt="sm">
          <EnvLessons env={env} />
        </Tabs.Panel>
      </Tabs>
    </Page>
  )
}

// Git info comes from the existing /api/repos/status endpoint: GET
// /api/environments carries no branch/HEAD. Falls back to the (mostly
// empty) fields on the environment row.
function useEnvGit(env: EnvironmentRow): { branch: string | null; head: string | null; clean: boolean | null; commits: string } {
  const { data } = useLoad(() => (env.path ? getRepoStatus(env.path).catch(() => null as RepoStatus | null) : Promise.resolve(null as RepoStatus | null)), [env.path])
  const gs = data ?? null
  const commits = gs?.recent_commits ?? (Array.isArray(env.recent_commits) ? env.recent_commits.join('\n') : (env.recent_commits ?? ''))
  return {
    branch: gs?.branch ?? env.branch ?? null,
    head: gs?.head ?? env.head ?? null,
    clean: gs?.changes === undefined || gs?.changes === null ? (env.clean ?? null) : !gs.changes.trim() || /nothing to commit/i.test(gs.changes),
    commits,
  }
}

function EnvOverview({ env, onChanged }: { env: EnvironmentRow; onChanged: () => void }) {
  const t = useEnvStrings()
  const [pat, setPat] = useState('')
  const [editingPat, setEditingPat] = useState(false)
  const repoLabel = envRepoLabel(env)
  const git = useEnvGit(env)
  const cleanLabel = git.clean === true ? t['env.clean'] : git.clean === false ? t['env.dirty'] : t['env.clean-unknown']
  return (
    <Stack>
      <Card withBorder>
        <KeyValue
          rows={[
            [t['env.branch'], git.branch ?? '—'],
            [t['env.head'], git.head ?? '—'],
            ...(repoLabel ? ([['Repo', repoLabel]] as [string, string][]) : []),
            ...(env.path ? ([[t['env.detail-path'], env.path]] as [string, string][]) : []),
            ...(env.url ? ([['URL', env.url]] as [string, string][]) : []),
            [t['env.last-sync'], envLastSync(env) ? fmtTime(envLastSync(env)) : t['env.never-synced']],
            [t['env.clean'], cleanLabel],
          ]}
        />
        {git.commits ? (
          <div>
            <Text size="sm" fw={600} mt="sm">
              {t['env.detail-recent-commits']}
            </Text>
            <Code block>{git.commits}</Code>
          </div>
        ) : null}
        <Group gap="xs" mt="md">
          <Button
            size="xs"
            onClick={async () => {
              const res = await attempt(() => createChatSession({ environment_id: env.id }))
              if (res?.kite_session_id) go(`/chat/${encodeURIComponent(res.kite_session_id)}`)
            }}
          >
            {t['env.detail-open-env-chat']}
          </Button>
          {env.id !== 'default' ? (
            <Button
              size="xs"
              variant="light"
              leftSection={<IconRefresh size={14} />}
              onClick={async () => {
                await attempt(() => syncEnvironment(env.id), t['env.sync-done'])
                onChanged()
              }}
            >
              {t['env.sync']}
            </Button>
          ) : null}
        </Group>
      </Card>
      {env.kind === 'github' ? (
        <Card withBorder>
          <Text fw={600}>{t['env.form-pat']}</Text>
          <Text size="sm" c="dimmed" mb="xs">
            {env.has_pat ? t['env.form-has-pat'] : t['env.form-no-pat']}
          </Text>
          {editingPat ? (
            <Stack gap="xs">
              <PasswordInput
                placeholder={t['env.form-pat-ph']}
                description={t['env.form-pat-help']}
                value={pat}
                onChange={(e) => setPat(e.currentTarget.value)}
              />
              <Group gap="xs">
                <Button
                  size="xs"
                  disabled={!pat.trim()}
                  onClick={async () => {
                    const res = await attempt(() => saveEnvironmentPat(env.id, pat.trim()), t['env.form-pat-saved'])
                    if (res !== undefined) {
                      setPat('')
                      setEditingPat(false)
                      onChanged()
                    }
                  }}
                >
                  {t['env.form-pat-change']}
                </Button>
                <Button size="xs" variant="default" onClick={() => setEditingPat(false)}>
                  {t['env.form-cancel']}
                </Button>
              </Group>
            </Stack>
          ) : (
            <Group gap="xs">
              <Button size="xs" variant="light" onClick={() => setEditingPat(true)}>
                {t['env.form-pat-change']}
              </Button>
              {env.has_pat ? (
                <ConfirmButton
                  size="xs"
                  variant="light"
                  color="red"
                  label={t['env.form-pat-delete']}
                  title={t['env.form-pat-delete']}
                  message={t['env.form-pat-help']}
                  confirmLabel={t['env.form-pat-delete']}
                  onConfirm={async () => {
                    const res = await attempt(() => deleteEnvironmentPat(env.id), t['env.form-pat-deleted'])
                    if (res !== undefined) onChanged()
                  }}
                />
              ) : null}
            </Group>
          )}
        </Card>
      ) : null}
    </Stack>
  )
}

function EnvSessionsTasks({ env }: { env: EnvironmentRow }) {
  const t = useEnvStrings()
  const sessions = useLoad(() => listChatSessions(), [])
  const tasks = useLoad(() => get<{ tasks: TaskRow[] }>('/api/tasks?limit=50'), [], 8000)
  const mine = (sessions.data ?? []).filter((s) => s.environment_id === env.id && s.status !== 'archived')
  const myTasks = (tasks.data?.tasks ?? []).filter((tk) => tk.environment_id === env.id)
  return (
    <Stack>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t['env.detail-recent-sessions']}
        </Text>
        {!sessions.data ? (
          <Loading error={sessions.error} />
        ) : mine.length ? (
          <Table highlightOnHover>
            <Table.Tbody>
              {mine.map((s) => (
                <Table.Tr key={s.kite_session_id} className="clickable" onClick={() => go(`/chat/${encodeURIComponent(s.kite_session_id)}`)}>
                  <Table.Td>
                    <Text size="sm">{s.title || s.kite_session_id}</Text>
                    <Text size="xs" c="dimmed">
                      {s.kind ?? 'chat'} · {s.status ?? 'active'}
                    </Text>
                  </Table.Td>
                  <Table.Td w={120}>
                    <SessionMoves sessionId={s.kite_session_id} />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : (
          <Empty>{t['env.detail-no-sessions']}</Empty>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t['env.detail-recent-tasks']}
        </Text>
        {!tasks.data ? (
          <Loading error={tasks.error} />
        ) : myTasks.length ? (
          <Table highlightOnHover>
            <Table.Tbody>
              {myTasks.map((tk) => (
                <Table.Tr key={tk.task_id} className="clickable" onClick={() => go(`/tasks/${encodeURIComponent(tk.task_id)}`)}>
                  <Table.Td>
                    <Text size="sm" lineClamp={2}>
                      {tk.goal || tk.task_id}
                    </Text>
                    <Text size="xs" c="dimmed">
                      {tk.task_id} · {tk.created}
                    </Text>
                  </Table.Td>
                  <Table.Td w={90}>
                    <StateBadge state={tk.state} />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        ) : (
          <Empty>{t['env.detail-no-tasks']}</Empty>
        )}
      </Card>
    </Stack>
  )
}

function SessionMoves({ sessionId }: { sessionId: string }) {
  const t = useEnvStrings()
  const { data } = useLoad(() => listSessionMoves(sessionId), [sessionId])
  const moves: SessionMove[] = data ?? []
  if (!moves.length) return <Text size="xs" c="dimmed">—</Text>
  return (
    <Text size="xs" c="dimmed" title={t['env.detail-moves']}>
      {moves.map((m) => `${m.from_env ?? '?'} → ${m.to_env ?? '?'} (${fmtTime(m.moved_at)})`).join('; ')}
    </Text>
  )
}

function EnvContext({ env }: { env: EnvironmentRow }) {
  const t = useEnvStrings()
  const repoKey = envRepoLabel(env) ?? env.path ?? env.id
  const exp = useLoad(() => getExperience(repoKey), [repoKey])
  const git = useEnvGit(env)
  const cleanLabel = git.clean === true ? t['env.clean'] : git.clean === false ? t['env.dirty'] : t['env.clean-unknown']
  return (
    <Stack>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t['env.detail-git-info']}
        </Text>
        <KeyValue
          rows={[
            [t['env.branch'], git.branch ?? '—'],
            [t['env.head'], git.head ?? '—'],
            [t['env.clean'], cleanLabel],
            [t['env.last-sync'], envLastSync(env) ? fmtTime(envLastSync(env)) : t['env.never-synced']],
          ]}
        />
        {git.commits ? (
          <div>
            <Text size="sm" fw={600} mt="sm">
              {t['env.detail-recent-commits']}
            </Text>
            <Code block>{git.commits}</Code>
          </div>
        ) : null}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t['env.detail-experience']}
        </Text>
        {!exp.data ? (
          <Loading error={exp.error} />
        ) : exp.data.has_doc ? (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              v{exp.data.version ?? '?'} · {exp.data.updated_at ?? ''}
            </Text>
            <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
              {(exp.data.content ?? '').slice(0, 2000)}
            </Text>
            <Button size="xs" variant="light" onClick={() => go(`/environments/${encodeURIComponent(env.id)}/lessons`)}>
              {t['env.view-lessons']}
            </Button>
          </Stack>
        ) : (
          <Empty>{t['env.detail-experience-none']}</Empty>
        )}
      </Card>
    </Stack>
  )
}

function EnvLessons({ env }: { env: EnvironmentRow }) {
  const t = useEnvStrings()
  const repoKey = envRepoLabel(env) ?? env.path ?? env.id
  const exp = useLoad(() => getExperience(repoKey), [repoKey])
  const [diff, setDiff] = useState<string | null>(null)
  const doc: ExperienceDoc | undefined = exp.data
  return (
    <Stack>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t['env.detail-experience']}
        </Text>
        {!doc ? (
          <Loading error={exp.error} />
        ) : doc.has_doc ? (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              v{doc.version ?? '?'} · {doc.updated_at ?? ''}
            </Text>
            <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
              {doc.content ?? ''}
            </Text>
          </Stack>
        ) : (
          <Empty>{t['env.detail-experience-none']}</Empty>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t['env.detail-pending']}
        </Text>
        {!doc ? (
          <Loading error={exp.error} />
        ) : doc.pending?.id ? (
          <Stack gap="xs">
            <Text size="sm">
              v{doc.pending.version ?? '?'} · {doc.pending.created_at ?? ''}
            </Text>
            {diff !== null ? <Code block>{diff || '—'}</Code> : null}
            <Group gap="xs">
              <Button
                size="xs"
                variant="light"
                onClick={async () => {
                  const d = await attempt(() => getExperienceDiff(doc.pending!.id!))
                  if (d !== undefined) setDiff(d)
                }}
              >
                {t['env.detail-diff']}
              </Button>
              <Button
                size="xs"
                color="teal"
                onClick={async () => {
                  const res = await attempt(() => approveExperience(doc.pending!.id!))
                  if (res !== undefined) exp.reload()
                }}
              >
                {t['env.detail-approve']}
              </Button>
              <Button
                size="xs"
                variant="light"
                color="red"
                onClick={async () => {
                  const res = await attempt(() => rejectExperience(doc.pending!.id!))
                  if (res !== undefined) exp.reload()
                }}
              >
                {t['env.detail-reject']}
              </Button>
            </Group>
          </Stack>
        ) : (
          <Empty>{t['env.detail-pending-none']}</Empty>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t['env.detail-lessons']}
        </Text>
        <Text size="sm" c="dimmed">
          {num(envLessonCount(env))} {t['env.lessons']}
        </Text>
        {envLessonCount(env) === 0 || envLessonCount(env) === null ? <Empty>{t['env.detail-lessons-none']}</Empty> : null}
      </Card>
    </Stack>
  )
}
