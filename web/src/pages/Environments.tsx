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
  Textarea,
  TextInput,
} from '@mantine/core'
import { IconBrandGithub, IconFolder, IconPlus, IconRefresh, IconTrash } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import {
  approveExperience,
  approvePendingLesson,
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
  getEnvContext,
  getExperience,
  getExperienceDiff,
  getRepoMemory,
  getRepoStatus,
  listChatSessions,
  listEnvironments,
  listSessionMoves,
  rebuildCodemap,
  rejectExperience,
  rejectPendingLesson,
  saveEnvDescription,
  saveEnvironmentPat,
  syncEnvironment,
  type EnvContextData,
  type EnvironmentRow,
  type ExperienceDoc,
  type KiteChatSession,
  type RepoMemory,
  type RepoStatus,
  type SessionMove,
  type TaskRow,
} from '../api'
import { ConfirmButton, Empty, KeyValue, Loading, Page, StateBadge } from '../components/common'
import { useT } from '../i18n'
import { go } from '../router'
import { ago, attempt, fmtTime, num, useLoad, usd } from '../util'
import { get } from '../api'

const DEFAULT_ENV: EnvironmentRow = { id: 'default', name: 'Default', kind: 'default' }

function withDefault(envs: EnvironmentRow[]): EnvironmentRow[] {
  if (envs.some((e) => e.id === 'default')) return envs
  return [DEFAULT_ENV, ...envs]
}

export default function Environments({ parts }: { parts: string[] }) {
  const t = useT()
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
  if (!envs) return <Page title={t('env.title')}>{<Loading error={error} />}</Page>
  if (id && !env)
    return (
      <Page title={t('env.title')}>
        <Alert color="yellow">{t('env.empty')}</Alert>
        <Button mt="md" variant="light" onClick={() => go('/environments')}>
          {t('env.detail-back')}
        </Button>
      </Page>
    )
  if (env) return <EnvDetail key={env.id} env={env} tab={tab} onChanged={reload} />
  return (
    <Page
      title={t('env.title')}
      actions={
        <Button leftSection={<IconPlus size={16} />} onClick={() => go('/environments/new')}>
          {t('env.new')}
        </Button>
      }
    >
      <Text size="sm" c="dimmed" mb="md">
        {t('env.subtitle')}
      </Text>
      {parts[0] === 'new' ? <AddEnvironment onAdded={(e) => go(`/environments/${encodeURIComponent(e.id)}`)} onCancel={() => go('/environments')} /> : null}
      {envs.length <= 1 ? <Empty>{t('env.empty')}</Empty> : null}
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
  const t = useT()
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
              {isDefault ? t('env.default-name') : envDisplayName(env)}
            </Text>
            <Badge variant="light" size="sm">
              {env.kind === 'github' ? t('env.kind-github') : env.kind === 'local' ? t('env.kind-local') : t('env.kind-default')}
            </Badge>
          </Group>
          {isDefault ? (
            <Text size="sm" c="dimmed">
              {t('env.default-desc')}
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
            title={t('env.delete-title')}
            message={t('env.delete-message')}
            confirmLabel={t('env.delete')}
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
              {t('env.branch')}: <Code>{env.branch ?? '—'}</Code>
            </Text>
            <Text size="sm">
              {t('env.head')}: <Code>{env.head ? env.head.slice(0, 8) : '—'}</Code>
            </Text>
            {env.clean === true ? (
              <Badge color="teal" size="sm">
                {t('env.clean')}
              </Badge>
            ) : env.clean === false ? (
              <Badge color="orange" size="sm">
                {t('env.dirty')}
              </Badge>
            ) : (
              <Badge color="gray" size="sm">
                {t('env.clean-unknown')}
              </Badge>
            )}
          </Group>
          <Text size="sm" c="dimmed">
            {t('env.last-sync')}:{' '}
            {envLastSync(env) ? ago(envLastSync(env)) : t('env.never-synced')}
          </Text>
          <Text size="sm" c="dimmed">
            {num(shownSessions)} {t('env.sessions')} · {num(shownTasks)} {t('env.tasks')} · {num(envLessonCount(env))}{' '}
            {t('env.lessons')}
            {env.spend_month_usd !== null && env.spend_month_usd !== undefined ? ` · ${t('env.spend-month')}: ${usd(env.spend_month_usd)}` : ''}
          </Text>
        </Stack>
      ) : null}
      <Group gap="xs" mt="md">
        <Button size="xs" onClick={openChat}>
          {t('env.open-chat')}
        </Button>
        {!isDefault ? (
          <Button
            size="xs"
            variant="light"
            leftSection={<IconRefresh size={14} />}
            loading={syncing}
            onClick={async () => {
              setSyncing(true)
              await attempt(() => syncEnvironment(env.id), t('env.sync-done'))
              setSyncing(false)
              onChanged()
            }}
          >
            {t('env.sync')}
          </Button>
        ) : null}
        <Button size="xs" variant="light" onClick={() => go(`/environments/${encodeURIComponent(env.id)}/sessions`)}>
          {t('env.view-sessions')}
        </Button>
        <Button size="xs" variant="light" onClick={() => go(`/tasks?env=${encodeURIComponent(env.id)}`)}>
          {t('env.view-tasks')}
        </Button>
        <Button size="xs" variant="light" onClick={() => go(`/environments/${encodeURIComponent(env.id)}/lessons`)}>
          {t('env.view-lessons')}
        </Button>
      </Group>
    </Card>
  )
}

function AddEnvironment({ onAdded, onCancel }: { onAdded: (e: EnvironmentRow) => void; onCancel: () => void }) {
  const t = useT()
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
        {t('env.add-title')}
      </Text>
      <Stack gap="xs">
        <TextInput label={t('env.form-name')} placeholder={t('env.form-name-ph')} value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <Select
          label={t('env.form-kind')}
          data={[
            { value: 'github', label: t('env.kind-github') },
            { value: 'local', label: t('env.kind-local') },
          ]}
          value={kind}
          onChange={(v) => setKind(v === 'local' ? 'local' : 'github')}
          allowDeselect={false}
        />
        <TextInput
          label={t('env.form-path')}
          placeholder={t('env.form-path-ph')}
          description={kind === 'github' ? t('env.form-path-desc-github') : t('env.form-path-desc-local')}
          value={path}
          onChange={(e) => setPath(e.currentTarget.value)}
          leftSection={<IconFolder size={16} />}
        />
        {kind === 'github' ? (
          <>
            <TextInput
              label={t('env.form-url')}
              placeholder={t('env.form-url-ph')}
              value={url}
              onChange={(e) => setUrl(e.currentTarget.value)}
              leftSection={<IconBrandGithub size={16} />}
            />
            <Text size="xs" c="dimmed">
              {t('env.form-pat-after')}
            </Text>
          </>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onCancel}>
            {t('env.form-cancel')}
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
                t('env.form-added'),
              )
              setBusy(false)
              if (res) onAdded(res)
            }}
          >
            {t('env.form-add')}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

// ---------------------------------------------------------------- detail
function EnvDetail({ env, tab, onChanged }: { env: EnvironmentRow; tab: string; onChanged: () => void }) {
  const t = useT()
  const active = ['overview', 'sessions', 'context', 'lessons'].includes(tab) ? tab : 'overview'
  return (
    <Page
      title={env.id === 'default' ? t('env.default-name') : envDisplayName(env)}
      actions={
        <Button variant="light" onClick={() => go('/environments')}>
          {t('env.detail-back')}
        </Button>
      }
    >
      <Tabs value={active} onChange={(v) => v && go(`/environments/${encodeURIComponent(env.id)}${v === 'overview' ? '' : `/${v}`}`)}>
        <Tabs.List>
          <Tabs.Tab value="overview">{t('env.detail-overview')}</Tabs.Tab>
          <Tabs.Tab value="sessions">{t('env.detail-sessions-tasks')}</Tabs.Tab>
          <Tabs.Tab value="context">{t('env.detail-context')}</Tabs.Tab>
          <Tabs.Tab value="lessons">{t('env.detail-lessons')}</Tabs.Tab>
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
  const t = useT()
  const [pat, setPat] = useState('')
  const [editingPat, setEditingPat] = useState(false)
  const repoLabel = envRepoLabel(env)
  const git = useEnvGit(env)
  const cleanLabel = git.clean === true ? t('env.clean') : git.clean === false ? t('env.dirty') : t('env.clean-unknown')
  return (
    <Stack>
      <Card withBorder>
        <KeyValue
          rows={[
            [t('env.branch'), git.branch ?? '—'],
            [t('env.head'), git.head ?? '—'],
            ...(repoLabel ? ([['Repo', repoLabel]] as [string, string][]) : []),
            ...(env.path ? ([[t('env.detail-path'), env.path]] as [string, string][]) : []),
            ...(env.url ? ([['URL', env.url]] as [string, string][]) : []),
            [t('env.last-sync'), envLastSync(env) ? fmtTime(envLastSync(env)) : t('env.never-synced')],
            [t('env.clean'), cleanLabel],
          ]}
        />
        {git.commits ? (
          <div>
            <Text size="sm" fw={600} mt="sm">
              {t('env.detail-recent-commits')}
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
            {t('env.detail-open-env-chat')}
          </Button>
          {env.id !== 'default' ? (
            <Button
              size="xs"
              variant="light"
              leftSection={<IconRefresh size={14} />}
              onClick={async () => {
                await attempt(() => syncEnvironment(env.id), t('env.sync-done'))
                onChanged()
              }}
            >
              {t('env.sync')}
            </Button>
          ) : null}
        </Group>
      </Card>
      {env.kind === 'github' ? (
        <Card withBorder>
          <Text fw={600}>{t('env.form-pat')}</Text>
          <Text size="sm" c="dimmed" mb="xs">
            {env.has_pat ? t('env.form-has-pat') : t('env.form-no-pat')}
          </Text>
          {editingPat ? (
            <Stack gap="xs">
              <PasswordInput
                placeholder={t('env.form-pat-ph')}
                description={t('env.form-pat-help')}
                value={pat}
                onChange={(e) => setPat(e.currentTarget.value)}
              />
              <Group gap="xs">
                <Button
                  size="xs"
                  disabled={!pat.trim()}
                  onClick={async () => {
                    const res = await attempt(() => saveEnvironmentPat(env.id, pat.trim()), t('env.form-pat-saved'))
                    if (res !== undefined) {
                      setPat('')
                      setEditingPat(false)
                      onChanged()
                    }
                  }}
                >
                  {t('env.form-pat-change')}
                </Button>
                <Button size="xs" variant="default" onClick={() => setEditingPat(false)}>
                  {t('env.form-cancel')}
                </Button>
              </Group>
            </Stack>
          ) : (
            <Group gap="xs">
              <Button size="xs" variant="light" onClick={() => setEditingPat(true)}>
                {t('env.form-pat-change')}
              </Button>
              {env.has_pat ? (
                <ConfirmButton
                  size="xs"
                  variant="light"
                  color="red"
                  label={t('env.form-pat-delete')}
                  title={t('env.form-pat-delete')}
                  message={t('env.form-pat-help')}
                  confirmLabel={t('env.form-pat-delete')}
                  onConfirm={async () => {
                    const res = await attempt(() => deleteEnvironmentPat(env.id), t('env.form-pat-deleted'))
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
  const t = useT()
  const sessions = useLoad(() => listChatSessions(), [])
  const tasks = useLoad(() => get<{ tasks: TaskRow[] }>('/api/tasks?limit=50'), [], 8000)
  const mine = (sessions.data ?? []).filter((s) => s.environment_id === env.id && s.status !== 'archived')
  const myTasks = (tasks.data?.tasks ?? []).filter((tk) => tk.environment_id === env.id)
  return (
    <Stack>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.detail-recent-sessions')}
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
          <Empty>{t('env.detail-no-sessions')}</Empty>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.detail-recent-tasks')}
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
          <Empty>{t('env.detail-no-tasks')}</Empty>
        )}
      </Card>
    </Stack>
  )
}

function SessionMoves({ sessionId }: { sessionId: string }) {
  const t = useT()
  const { data } = useLoad(() => listSessionMoves(sessionId), [sessionId])
  const moves: SessionMove[] = data ?? []
  if (!moves.length) return <Text size="xs" c="dimmed">—</Text>
  return (
    <Text size="xs" c="dimmed" title={t('env.detail-moves')}>
      {moves.map((m) => `${m.from_env ?? '?'} → ${m.to_env ?? '?'} (${fmtTime(m.moved_at)})`).join('; ')}
    </Text>
  )
}

function EnvContext({ env }: { env: EnvironmentRow }) {
  const t = useT()
  const repoKey = envRepoLabel(env) ?? env.path ?? env.id
  const exp = useLoad(() => getExperience(repoKey), [repoKey])
  const git = useEnvGit(env)
  const cleanLabel = git.clean === true ? t('env.clean') : git.clean === false ? t('env.dirty') : t('env.clean-unknown')
  const isDefault = env.id === 'default'
  // The context endpoint is the single source for the 4 Điểm 9 blocks.
  // Poll while a codemap rebuild is running (same pattern as Knowledge's
  // converting poll); the flag lives in state so the interval follows it.
  const [building, setBuilding] = useState(false)
  const ctx = useLoad(() => getEnvContext(env.id), [env.id, building], building ? 3000 : undefined)
  const ctxData: EnvContextData | undefined = ctx.data
  useEffect(() => {
    if (ctxData) setBuilding(ctxData.codemap.status === 'building')
  }, [ctxData])
  // Description editing: null means "not edited yet" — the shown value then
  // comes from the server (context endpoint first, env row as fallback).
  const [desc, setDesc] = useState<string | null>(null)
  const [descSaved, setDescSaved] = useState(false)
  const shownDesc = desc ?? ctxData?.description ?? env.description ?? ''
  const codemap = ctxData?.codemap
  const agentsMd = ctxData?.agents_md
  return (
    <Stack>
      {!isDefault ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {t('env.context-description')}
          </Text>
          <Text size="sm" c="dimmed" mb="xs">
            {t('env.context-description-hint')}
          </Text>
          <Textarea
            value={shownDesc}
            onChange={(e) => {
              setDesc(e.currentTarget.value)
              setDescSaved(false)
            }}
            placeholder={t('env.context-description-placeholder')}
            autosize
            minRows={3}
            maxLength={2000}
          />
          <Group gap="xs" mt="xs">
            <Button
              size="xs"
              disabled={shownDesc === (ctxData?.description ?? env.description ?? '')}
              onClick={async () => {
                const res = await attempt(() => saveEnvDescription(env.id, shownDesc))
                if (res !== undefined) {
                  setDesc(null)
                  setDescSaved(true)
                  ctx.reload()
                }
              }}
            >
              {t('env.context-description-save')}
            </Button>
            {descSaved ? (
              <Text size="xs" c="dimmed">
                {t('env.context-description-saved')}
              </Text>
            ) : null}
          </Group>
        </Card>
      ) : null}
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.context-codemap')}
        </Text>
        {!ctxData ? (
          <Loading error={ctx.error} />
        ) : !codemap || codemap.status === 'none' ? (
          <Empty>{t('env.context-codemap-none')}</Empty>
        ) : codemap.status === 'not_built' ? (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              {t('env.context-codemap-not-built')}
            </Text>
            <CodemapRebuildButton envId={env.id} building={building} disabled={!codemap.graphify_available} onRebuilt={() => ctx.reload()} />
            {!codemap.graphify_available ? (
              <Text size="sm" c="dimmed">
                {t('env.context-codemap-unavailable')}
              </Text>
            ) : null}
          </Stack>
        ) : codemap.status === 'building' ? (
          <Loading label={t('env.context-codemap-building')} />
        ) : codemap.status === 'failed' ? (
          <Stack gap="xs">
            <Text size="sm" c="red">
              {t('env.context-codemap-failed')} {codemap.error ?? '—'}
            </Text>
            <CodemapRebuildButton envId={env.id} building={building} disabled={!codemap.graphify_available} onRebuilt={() => ctx.reload()} />
          </Stack>
        ) : codemap.status === 'unavailable' ? (
          <Text size="sm" c="dimmed">
            {t('env.context-codemap-unavailable')}
          </Text>
        ) : (
          <Stack gap="xs">
            <KeyValue
              rows={[
                [t('env.context-codemap-files'), String(codemap.files ?? 0)],
                [t('env.context-codemap-symbols'), String(codemap.symbols.total ?? 0)],
                [t('env.context-codemap-built-at'), codemap.built_at ? fmtTime(codemap.built_at) : '—'],
              ]}
            />
            {codemap.top_files?.length ? (
              <div>
                <Text size="sm" fw={600} mt="xs">
                  {t('env.context-codemap-top-files')}
                </Text>
                <Code block>
                  {codemap.top_files.map((f) => `${f.path} (${f.symbols})`).join('\n')}
                </Code>
              </div>
            ) : null}
            <Group gap="xs">
              <CodemapRebuildButton envId={env.id} building={building} disabled={!codemap.graphify_available} onRebuilt={() => ctx.reload()} />
            </Group>
          </Stack>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.context-agents')}
        </Text>
        {!ctxData ? (
          <Loading error={ctx.error} />
        ) : !agentsMd?.present ? (
          <Empty>{t('env.context-agents-none')}</Empty>
        ) : (
          <Stack gap="xs">
            {agentsMd.truncated ? (
              <Text size="sm" c="dimmed">
                {t('env.context-agents-truncated', { n: agentsMd.chars })}
              </Text>
            ) : null}
            <Code block>{agentsMd.content}</Code>
          </Stack>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.context-preview')}
        </Text>
        <Text size="sm" c="dimmed" mb="xs">
          {t('env.context-preview-hint')}
        </Text>
        {!ctxData ? (
          <Loading error={ctx.error} />
        ) : (
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {ctxData.preview}
          </Text>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.detail-git-info')}
        </Text>
        <KeyValue
          rows={[
            [t('env.branch'), git.branch ?? '—'],
            [t('env.head'), git.head ?? '—'],
            [t('env.clean'), cleanLabel],
            [t('env.last-sync'), envLastSync(env) ? fmtTime(envLastSync(env)) : t('env.never-synced')],
          ]}
        />
        {git.commits ? (
          <div>
            <Text size="sm" fw={600} mt="sm">
              {t('env.detail-recent-commits')}
            </Text>
            <Code block>{git.commits}</Code>
          </div>
        ) : null}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.detail-experience')}
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
              {t('env.view-lessons')}
            </Button>
          </Stack>
        ) : (
          <Empty>{t('env.detail-experience-none')}</Empty>
        )}
      </Card>
    </Stack>
  )
}

function CodemapRebuildButton({
  envId,
  building,
  disabled,
  onRebuilt,
}: {
  envId: string
  building: boolean
  disabled: boolean
  onRebuilt: () => void
}) {
  const t = useT()
  return (
    <Button
      size="xs"
      variant="light"
      disabled={disabled || building}
      onClick={async () => {
        const res = await attempt(() => rebuildCodemap(envId))
        if (res !== undefined) onRebuilt()
      }}
    >
      {building ? t('env.context-codemap-building') : t('env.context-codemap-rebuild')}
    </Button>
  )
}

function EnvLessons({ env }: { env: EnvironmentRow }) {
  const t = useT()
  const repoKey = envRepoLabel(env) ?? env.path ?? env.id
  const exp = useLoad(() => getExperience(repoKey), [repoKey])
  // The approved lessons + this repo's pending lessons come from
  // /api/repos/memory (N10): the backend filters pending by repo, so what
  // shows here belongs to this environment only. No path (Default) → the
  // call resolves null and the tab falls back to the count.
  const mem = useLoad(
    () => (env.path ? getRepoMemory(env.path) : Promise.resolve(null as RepoMemory | null)),
    [env.path],
  )
  const lessons = mem.data?.lessons ?? []
  const pendingLessons = (mem.data?.pending ?? []).filter((p) => p.kind === 'lesson')
  const [diff, setDiff] = useState<string | null>(null)
  const doc: ExperienceDoc | undefined = exp.data
  return (
    <Stack>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.detail-experience')}
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
          <Empty>{t('env.detail-experience-none')}</Empty>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.detail-pending')}
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
                {t('env.detail-diff')}
              </Button>
              <Button
                size="xs"
                color="teal"
                onClick={async () => {
                  const res = await attempt(() => approveExperience(doc.pending!.id!))
                  if (res !== undefined) exp.reload()
                }}
              >
                {t('env.detail-approve')}
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
                {t('env.detail-reject')}
              </Button>
            </Group>
          </Stack>
        ) : (
          <Empty>{t('env.detail-pending-none')}</Empty>
        )}
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          {t('env.detail-lessons')}
        </Text>
        <Text size="sm" c="dimmed">
          {num(envLessonCount(env))} {t('env.lessons')}
        </Text>
        {mem.loading ? (
          <Loading />
        ) : mem.error ? (
          <Loading error={mem.error} />
        ) : lessons.length ? (
          <Stack gap={4} mt="xs">
            {lessons.map((line, i) => (
              <Text key={i} size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                {line}
              </Text>
            ))}
          </Stack>
        ) : (
          <Empty>{t('env.detail-lessons-none')}</Empty>
        )}
      </Card>
      {env.path ? (
        <Card withBorder>
          <Text fw={600} mb="xs">
            {t('env.detail-lessons-pending')}
          </Text>
          {mem.loading ? (
            <Loading />
          ) : mem.error ? (
            <Loading error={mem.error} />
          ) : pendingLessons.length ? (
            <Stack gap="xs">
              {pendingLessons.map((p) => (
                <div key={p.id}>
                  <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                    {p.text}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {p.id}
                    {p.task_id ? ` · ${p.task_id}` : ''}
                  </Text>
                  <Group gap="xs" mt={4}>
                    <Button
                      size="xs"
                      color="teal"
                      onClick={async () => {
                        const res = await attempt(
                          () => approvePendingLesson(p.id),
                          t('env.detail-lessons-approved'),
                        )
                        if (res !== undefined) mem.reload()
                      }}
                    >
                      {t('env.detail-approve')}
                    </Button>
                    <Button
                      size="xs"
                      variant="light"
                      color="red"
                      onClick={async () => {
                        const res = await attempt(
                          () => rejectPendingLesson(p.id),
                          t('env.detail-lessons-rejected'),
                        )
                        if (res !== undefined) mem.reload()
                      }}
                    >
                      {t('env.detail-reject')}
                    </Button>
                  </Group>
                </div>
              ))}
              <Text size="xs" c="dimmed">
                {t('env.detail-lessons-pending-hint')}
              </Text>
            </Stack>
          ) : (
            <Empty>{t('env.detail-lessons-pending-none')}</Empty>
          )}
        </Card>
      ) : null}
    </Stack>
  )
}
