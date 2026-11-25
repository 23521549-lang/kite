import { Alert, Badge, Button, Card, Group, Stack, Tabs, Text, Timeline } from '@mantine/core'
import { get, post } from '../api'
import { ConfirmButton, DiffView, Empty, JsonView, KeyValue, Loading, StateBadge } from '../components/common'
import { attempt, fmtTime, short, useLoad } from '../util'

export type Detail = {
  log: Record<string, any>
  status: Record<string, any>
  result?: Record<string, any>
  journal: { runs: any[]; trace: any[]; requests: Record<string, any>; actions: any[]; notes: any[] }
}

export default function TaskDetail({ taskId, onChanged }: { taskId: string; onChanged: () => void }) {
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
  const patch = patchText.data ?? ''
  return (
    <Stack>
      <Card withBorder>
        <Group justify="space-between" align="flex-start">
          <div style={{ minWidth: 0, flex: 1 }}>
            <Text fw={600}>{taskId}</Text>
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
            Không đọc được trạng thái từ Temporal: {short(s.error, 200)}
          </Alert>
        ) : null}
        <Group gap="xs" mt="md">
          {done && patch ? (
            <ConfirmButton
              color="teal"
              size="xs"
              label="Áp vào repo"
              title="Áp patch"
              message={<Text size="sm">Áp patch vào working tree của repo gốc (không commit, không push).</Text>}
              confirmLabel="Áp"
              onConfirm={() => act('apply', {}, 'Đã áp patch')}
            />
          ) : null}
          {done && patch ? (
            <ConfirmButton
              size="xs"
              variant="light"
              label="Commit…"
              title="Commit đúng các file của patch đã áp"
              askText
              textLabel="Commit message"
              confirmLabel="Commit"
              onConfirm={(message) => act('commit', { message }, 'Đã commit (chưa push)')}
            />
          ) : null}
          {running ? (
            <>
              <Button size="xs" variant="light" onClick={() => act('pause', {}, 'Đã gửi lệnh tạm dừng')}>
                Tạm dừng
              </Button>
              <Button size="xs" variant="light" onClick={() => act('resume', {}, 'Đã gửi lệnh tiếp tục')}>
                Tiếp tục
              </Button>
              <ConfirmButton
                size="xs"
                variant="light"
                label="Gửi chỉ dẫn…"
                title="Gửi thêm chỉ dẫn cho task đang chạy"
                askText
                textLabel="Chỉ dẫn"
                confirmLabel="Gửi"
                onConfirm={(text) => act('instruct', { text }, 'Đã gửi chỉ dẫn')}
              />
              <ConfirmButton
                size="xs"
                color="red"
                variant="light"
                label="Hủy task"
                title="Hủy task?"
                message="Các bước đang chạy dừng trong vài giây, worker được trả lại."
                confirmLabel="Hủy task"
                onConfirm={() => act('cancel', {}, 'Đã hủy')}
              />
            </>
          ) : null}
          {!running ? (
            <>
              <ConfirmButton
                size="xs"
                variant="subtle"
                color="teal"
                label="👍 Tốt"
                title="Ghi nhận: kết quả tốt"
                askText
                textLabel="Lý do (không bắt buộc)"
                textDefault="ok"
                confirmLabel="Ghi"
                onConfirm={(reason) => act('note', { verdict: 'good', reason }, 'Đã ghi')}
              />
              <ConfirmButton
                size="xs"
                variant="subtle"
                color="red"
                label="👎 Chưa tốt"
                title="Ghi nhận: kết quả chưa tốt"
                askText
                textLabel="Sai ở đâu? Kite rút kinh nghiệm cho repo này"
                confirmLabel="Ghi"
                onConfirm={(reason) => act('note', { verdict: 'bad', reason }, 'Đã ghi')}
              />
            </>
          ) : null}
        </Group>
      </Card>
      <Tabs defaultValue="progress" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="progress">Tiến độ</Tabs.Tab>
          <Tabs.Tab value="result">Kết quả</Tabs.Tab>
          <Tabs.Tab value="diff">Diff</Tabs.Tab>
          <Tabs.Tab value="journal">Nhật ký</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="progress" pt="sm">
          <Card withBorder>
            <KeyValue
              rows={[
                ['Đã chạy', log.elapsed ?? s.elapsed ?? '—'],
                ['Đang làm', log.now ?? s.now ?? (running ? s.step : '—') ?? '—'],
                ...(s.waiting_for ? ([['Đang chờ', s.waiting_for]] as [string, string][]) : []),
                ...(log.fast_path ? ([['Đường nhanh', short(log.fast_path, 200)]] as [string, string][]) : []),
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
              <Empty>{log.note ?? 'Chưa có bước nào.'}</Empty>
            )}
          </Card>
        </Tabs.Panel>
        <Tabs.Panel value="result" pt="sm">
          <Card withBorder>
            {done ? (
              <Stack>
                <KeyValue
                  rows={[
                    ['Trạng thái', result.status ?? '—'],
                    ['Worker', result.worker ?? '—'],
                    ['Repo', result.repo_path ?? '—'],
                    ...(log.result
                      ? ([
                          ['Kite tự test', log.result.verified ? 'qua' : 'chưa qua'],
                          ['Thời gian', log.result.wall_seconds ? `${Math.round(log.result.wall_seconds)} giây` : '—'],
                          ['File đổi', (log.result.changed_files ?? []).join(', ') || '—'],
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
              <Empty>Task chưa xong.</Empty>
            )}
          </Card>
        </Tabs.Panel>
        <Tabs.Panel value="diff" pt="sm">
          <Card withBorder p={0}>
            {patch ? <DiffView patch={patch} /> : <Empty>Chưa có patch.</Empty>}
          </Card>
        </Tabs.Panel>
        <Tabs.Panel value="journal" pt="sm">
          <Stack>
            <Card withBorder>
              <KeyValue
                rows={[
                  ['Request tới model', String(data.journal.requests?.count ?? 0)],
                  ['Token vào / ra', `${data.journal.requests?.prompt_tokens ?? 0} / ${data.journal.requests?.completion_tokens ?? 0}`],
                  ['Thời gian model', `${data.journal.requests?.model_seconds ?? 0} giây`],
                  ['Lỗi', (data.journal.requests?.errors ?? []).length ? short((data.journal.requests.errors as string[]).join('; '), 300) : 'không'],
                ]}
              />
            </Card>
            {data.journal.notes.length ? (
              <Card withBorder>
                <Text fw={600} mb={4}>
                  Nhận xét
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
                Các lần chạy
              </Text>
              {data.journal.runs.length ? <JsonView data={data.journal.runs} /> : <Empty>Chưa có.</Empty>}
            </Card>
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  )
}
