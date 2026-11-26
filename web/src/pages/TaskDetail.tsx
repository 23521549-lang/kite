import { Alert, Badge, Button, Card, Group, Stack, Tabs, Text, Timeline } from '@mantine/core'
import { get, post } from '../api'
import { ConfirmButton, DiffView, Empty, JsonView, KeyValue, Loading, StateBadge } from '../components/common'
import { go } from '../router'
import { useT } from '../i18n'
import { useState } from 'react'
import { attempt, fmtTime, short, useLoad } from '../util'

export type Detail = {
  log: Record<string, any>
  status: Record<string, any>
  result?: Record<string, any>
  journal: { runs: any[]; trace: any[]; requests: Record<string, any>; actions: any[]; notes: any[] }
  environment_id?: string | null
  chat_session_id?: string | null
  last_apply_error?: { code: string; head: string; failed_files: string[]; at: number; task_id: string } | null
}

export default function TaskDetail({ taskId, onChanged }: { taskId: string; onChanged: () => void }) {
  const t = useT()
  const [rerunning, setRerunning] = useState(false)
  const { data, error, reload } = useLoad(
    () => get<Detail>(`/api/tasks/${encodeURIComponent(taskId)}`),
    [taskId],
    6000,
  )
  const patchText = useLoad(async () => {
    try {
      const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/patch`, { credentials: 'same-origin' })
      return res.ok ? await res.text() : ''
    } catch {
      return ''
    }
  }, [taskId, data?.status?.workflow])
  if (!data) return <Loading error={error} />
  const s = data.status ?? {}
  const log = data.log ?? {}
  const result = data.result ?? {}
  const running = s.workflow === 'RUNNING'
  const done = s.workflow === 'COMPLETED'
  const id = encodeURIComponent(taskId)
  const act = async (path: string, body: unknown = {}, ok?: string) => {
    await attempt(() => post(`/api/tasks/${id}/${path}`, body), ok)
    reload()
    onChanged()
  }
  // Rerun only exists for an apply conflict: it creates a new task and navigates to it
  // (no reload of the old detail).
  const rerun = async () => {
    if (rerunning) return
    setRerunning(true)
    try {
      const res = await attempt(() => post<{ task_id: string }>(`/api/tasks/${id}/rerun`, {}), t('tasks.detail.rerun-created'))
      if (res?.task_id) {
        onChanged()
        go(`/tasks/${encodeURIComponent(res.task_id)}`)
      }
    } finally {
      setRerunning(false)
    }
  }
  const applyError = data.last_apply_error
  const showApplyError = applyError?.code === 'apply_conflict'
  const patch = patchText.data ?? ''
  return (
    <Stack>
      <Card withBorder>
        <Group justify="space-between" align="flex-start">
          <div style={{ minWidth: 0, flex: 1 }}>
            <Text fw={600}>{taskId}</Text>
            <Group gap={4} mt={4}>
              <Text size="xs" c="dimmed">
                {t('tasks.origin.env')}
              </Text>
              {data.environment_id ? (
                <Badge
                  size="xs"
                  variant="light"
                  style={{ cursor: 'pointer' }}
                  title={t('tasks.origin.filter-env')}
                  onClick={() => go(`/tasks?env=${encodeURIComponent(data.environment_id as string)}`)}
                >
                  {short(data.environment_id, 20)}
                </Badge>
              ) : (
                <Text size="xs" c="dimmed">
                  {t('tasks.origin.unknown')}
                </Text>
              )}
              <Text size="xs" c="dimmed">
                →
              </Text>
              <Text size="xs" c="dimmed">
                {t('tasks.origin.session')}
              </Text>
              {data.chat_session_id ? (
                <Badge
                  size="xs"
                  variant="light"
                  style={{ cursor: 'pointer' }}
                  title={t('tasks.origin.filter-session')}
                  onClick={() => go(`/tasks?session=${encodeURIComponent(data.chat_session_id as string)}`)}
                >
                  {short(data.chat_session_id, 20)}
                </Badge>
              ) : (
                <Text size="xs" c="dimmed">
                  {t('tasks.origin.unknown')}
                </Text>
              )}
              <Text size="xs" c="dimmed">
                →
              </Text>
              <Text size="xs" c="dimmed">
                {t('tasks.origin.task')}
              </Text>
            </Group>
            <Text size="sm" c="dimmed">
              {s.now ?? s.step ?? s.waiting_for ?? ''}
            </Text>
          </div>
          <Group gap={6}>
            {s.queue_state ? <StateBadge state={s.queue_state} /> : null}
            {s.workflow ? (
              <Badge variant="outline" size="sm">
                {s.workflow}
              </Badge>
            ) : null}
          </Group>
        </Group>
        {s.error ? (
          <Alert color="yellow" mt="sm">
            {t('tasks.detail.temporal-error', { error: short(s.error, 200) })}
          </Alert>
        ) : null}
        <Group gap="xs" mt="md">
          {done && patch ? (
            <ConfirmButton
              color="teal"
              size="xs"
              label={t('tasks.detail.apply-patch')}
              title={t('tasks.detail.apply-title')}
              message={<Text size="sm">{t('tasks.detail.apply-message')}</Text>}
              confirmLabel={t('tasks.detail.apply')}
              onConfirm={() => act('apply', {}, t('tasks.detail.applied'))}
            />
          ) : null}
          {done && patch ? (
            <ConfirmButton
              size="xs"
              variant="light"
              label={t('tasks.detail.commit')}
              title={t('tasks.detail.commit-title')}
              askText
              textLabel={t('tasks.detail.commit-label')}
              confirmLabel={t('tasks.detail.commit-button')}
              onConfirm={(message) => act('commit', { message }, t('tasks.detail.committed'))}
            />
          ) : null}
          {running ? (
            <>
              <Button size="xs" variant="light" onClick={() => act('pause', {}, t('tasks.detail.paused'))}>
                {t('tasks.detail.pause')}
              </Button>
              <Button size="xs" variant="light" onClick={() => act('resume', {}, t('tasks.detail.resumed'))}>
                {t('tasks.detail.resume')}
              </Button>
              <ConfirmButton
                size="xs"
                variant="light"
                label={t('tasks.detail.instruct')}
                title={t('tasks.detail.instruct-title')}
                askText
                textLabel={t('tasks.detail.instruct-label')}
                confirmLabel={t('tasks.detail.instruct-send')}
                onConfirm={(text) => act('instruct', { text }, t('tasks.detail.instructed'))}
              />
              <ConfirmButton
                size="xs"
                color="red"
                variant="light"
                label={t('tasks.detail.cancel')}
                title={t('tasks.detail.cancel-title')}
                message={t('tasks.detail.cancel-message')}
                confirmLabel={t('tasks.detail.cancel')}
                onConfirm={() => act('cancel', {}, t('tasks.detail.cancelled'))}
              />
            </>
          ) : null}
          {!running ? (
            <>
              <ConfirmButton
                size="xs"
                variant="subtle"
                color="teal"
                label={t('tasks.detail.good')}
                title={t('tasks.detail.good-title')}
                askText
                textLabel={t('tasks.detail.good-reason')}
                textDefault="ok"
                confirmLabel={t('tasks.detail.record')}
                onConfirm={(reason) => act('note', { verdict: 'good', reason }, t('tasks.detail.recorded'))}
              />
              <ConfirmButton
                size="xs"
                variant="subtle"
                color="red"
                label={t('tasks.detail.bad')}
                title={t('tasks.detail.bad-title')}
                askText
                textLabel={t('tasks.detail.bad-reason')}
                confirmLabel={t('tasks.detail.record')}
                onConfirm={(reason) => act('note', { verdict: 'bad', reason }, t('tasks.detail.recorded'))}
              />
            </>
          ) : null}
        </Group>
      </Card>
      {showApplyError && applyError ? (
        <Alert color="red" title={t('tasks.detail.apply-error-title')}>
          <Stack gap="xs">
            <Text size="sm">{t('tasks.detail.apply-error-head', { head: short(applyError.head, 80) })}</Text>
            <Text size="sm">{t('tasks.detail.apply-error-files')}</Text>
            <Stack gap={0}>
              {applyError.failed_files.map((f) => (
                <Text key={f} size="sm" ff="monospace">
                  {f}
                </Text>
              ))}
            </Stack>
            <Text size="sm">{t('tasks.detail.apply-error-choose')}</Text>
            <Group gap="xs">
              <Button size="xs" onClick={rerun} loading={rerunning}>
                {t('tasks.detail.rerun')}
              </Button>
              <Button size="xs" variant="light" onClick={() => act('apply-error/dismiss', {}, t('tasks.detail.dismissed'))}>
                {t('tasks.detail.dismiss-patch')}
              </Button>
            </Group>
            <Text size="sm">
              {t('tasks.detail.apply-error-manual')}
            </Text>
          </Stack>
        </Alert>
      ) : null}
      <Tabs defaultValue="progress" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="progress">{t('tasks.detail.tab-progress')}</Tabs.Tab>
          <Tabs.Tab value="result">{t('tasks.detail.tab-result')}</Tabs.Tab>
          <Tabs.Tab value="diff">{t('tasks.detail.tab-diff')}</Tabs.Tab>
          <Tabs.Tab value="journal">{t('tasks.detail.tab-journal')}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="progress" pt="sm">
          <Card withBorder>
            <KeyValue
              rows={[
                [t('tasks.detail.elapsed'), log.elapsed ?? s.elapsed ?? '—'],
                [t('tasks.detail.doing'), log.now ?? s.now ?? (running ? s.step : '—') ?? '—'],
                ...(s.waiting_for ? ([[t('tasks.detail.waiting'), s.waiting_for]] as [string, string][]) : []),
                ...(log.fast_path ? ([[t('tasks.detail.fast-path'), short(log.fast_path, 200)]] as [string, string][]) : []),
              ]}
            />
            {(log.progress ?? []).length ? (
              <Timeline active={(log.progress ?? []).length} bulletSize={12} lineWidth={2} mt="md">
                {(log.progress as string[]).map((line, i) => (
                  <Timeline.Item key={i}>
                    <Text size="sm">{line}</Text>
                  </Timeline.Item>
                ))}
              </Timeline>
            ) : (
              <Empty>{log.note ?? t('tasks.detail.no-steps')}</Empty>
            )}
          </Card>
        </Tabs.Panel>
        <Tabs.Panel value="result" pt="sm">
          <Card withBorder>
            {done ? (
              <Stack>
                <KeyValue
                  rows={[
                    [t('tasks.detail.status'), result.status ?? '—'],
                    [t('tasks.detail.worker'), result.worker ?? '—'],
                    [t('tasks.detail.repo'), result.repo_path ?? '—'],
                    ...(log.result
                      ? ([
                          [t('tasks.detail.self-test'), log.result.verified ? t('tasks.detail.self-test-pass') : t('tasks.detail.self-test-fail')],
                          [t('tasks.detail.duration'), log.result.wall_seconds ? t('tasks.detail.duration-seconds', { seconds: Math.round(log.result.wall_seconds) }) : '—'],
                          [t('tasks.detail.changed-files'), (log.result.changed_files ?? []).join(', ') || '—'],
                        ] as [string, string][])
                      : []),
                  ]}
                />
                {result.summary ? (
                  <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                    {result.summary}
                  </Text>
                ) : null}
              </Stack>
            ) : (
              <Empty>{t('tasks.detail.not-done')}</Empty>
            )}
          </Card>
        </Tabs.Panel>
        <Tabs.Panel value="diff" pt="sm">
          <Card withBorder p={0}>
            {patch ? <DiffView patch={patch} /> : <Empty>{t('tasks.detail.no-patch')}</Empty>}
          </Card>
        </Tabs.Panel>
        <Tabs.Panel value="journal" pt="sm">
          <Stack>
            <Card withBorder>
              <KeyValue
                rows={[
                  [t('tasks.detail.requests'), String(data.journal.requests?.count ?? 0)],
                  [t('tasks.detail.tokens'), `${data.journal.requests?.prompt_tokens ?? 0} / ${data.journal.requests?.completion_tokens ?? 0}`],
                  [t('tasks.detail.model-time'), t('tasks.detail.model-seconds', { seconds: data.journal.requests?.model_seconds ?? 0 })],
                  [t('tasks.detail.errors'), (data.journal.requests?.errors ?? []).length ? short((data.journal.requests.errors as string[]).join('; '), 300) : t('tasks.detail.no-errors')],
                ]}
              />
            </Card>
            {data.journal.notes.length ? (
              <Card withBorder>
                <Text fw={600} mb={4}>
                  {t('tasks.detail.notes')}
                </Text>
                {data.journal.notes.map((n, i) => (
                  <Text key={i} size="sm">
                    {fmtTime(n.at ?? n.ts)} · {n.verdict}: {n.reason}
                  </Text>
                ))}
              </Card>
            ) : null}
            <Card withBorder>
              <Text fw={600} mb={4}>
                {t('tasks.detail.runs')}
              </Text>
              {data.journal.runs.length ? <JsonView data={data.journal.runs} /> : <Empty>{t('tasks.detail.no-runs')}</Empty>}
            </Card>
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  )
}
