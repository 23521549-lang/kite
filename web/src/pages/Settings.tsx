import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Group,
  NumberInput,
  PasswordInput,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core'
import { useEffect, useState } from 'react'
import { get, post, qs } from '../api'
import { ConfirmButton, Empty, JsonView, KeyValue, Loading, Page } from '../components/common'
import { loadMe, useKite } from '../store'
import { ago, attempt, fmtTime, notifyOk, useLoad } from '../util'
import { useT, type DictKey } from '../i18n'

export default function Settings() {
  const t = useT()
  const settings = useLoad(() => get<Record<string, any>>('/api/settings'), [])
  const s = settings.data
  return (
    <Page title={t('settings.title')}>
      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Stack>
          <Account />
          <TwoFactor />
          <Sessions />
        </Stack>
        <Stack>
          <Card withBorder>
            <Title order={5} mb="xs">
              {t('settings.machine')}
            </Title>
            {!s ? (
              <Loading error={settings.error} />
            ) : (
              <KeyValue
                rows={[
                  [t('settings.machine.version'), [s.version?.commit || '?', s.version?.subject].filter(Boolean).join(' · ')],
                  [t('settings.machine.date'), fmtTime(s.version?.date)],
                  [t('settings.machine.gateway'), `${s.gateway.host}:${s.gateway.port}`],
                  [t('settings.machine.worker-address'), s.gateway.public_url ?? t('settings.machine.worker-address-unset')],
                  [t('settings.machine.state-dir'), <Code key="d">{s.state_dir}</Code>],
                  [
                    t('settings.machine.ntfy'),
                    s.ntfy ? (
                      <Group key="n" gap="xs">
                        <Badge color="teal" variant="light">
                          {t('settings.machine.ntfy-on')}
                        </Badge>
                        <Button size="compact-xs" variant="light" onClick={() => attempt(() => post('/api/settings/ntfy/test'), t('settings.machine.ntfy-sent'))}>
                          {t('settings.machine.ntfy-test')}
                        </Button>
                      </Group>
                    ) : (
                      t('settings.machine.ntfy-off')
                    ),
                  ],
                ]}
              />
            )}
          </Card>
          <Update />
          <Cleanup />
          <Restart />
        </Stack>
      </SimpleGrid>
    </Page>
  )
}

function Account() {
  const t = useT()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [again, setAgain] = useState('')
  const [code, setCode] = useState('')
  const me = useKite((s) => s.me)
  return (
    <Card withBorder>
      <Title order={5}>{t('settings.account.title', { username: me?.username ?? '' })}</Title>
      <Text size="sm" c="dimmed" mb="xs">
        {t('settings.account.note')}
      </Text>
      <Stack gap="xs">
        <PasswordInput label={t('settings.account.current')} value={current} onChange={(e) => setCurrent(e.currentTarget.value)} autoComplete="current-password" />
        <PasswordInput label={t('settings.account.new')} value={next} onChange={(e) => setNext(e.currentTarget.value)} autoComplete="new-password" />
        <PasswordInput
          label={t('settings.account.again')}
          value={again}
          onChange={(e) => setAgain(e.currentTarget.value)}
          error={again && again !== next ? t('settings.account.mismatch') : undefined}
          autoComplete="new-password"
        />
        <TextInput label={t('settings.account.code')} value={code} onChange={(e) => setCode(e.currentTarget.value)} inputMode="numeric" maxLength={6} />
        <Group>
          <Button
            disabled={!current || next.length < 12 || next !== again || code.length !== 6}
            onClick={async () => {
              const res = await attempt(() => post('/api/auth/password', { current, new: next, code }), t('settings.account.changed'))
              if (res) {
                setCurrent('')
                setNext('')
                setAgain('')
                setCode('')
              }
            }}
          >
            {t('settings.account.change')}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

function TwoFactor() {
  const t = useT()
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [pending, setPending] = useState<{ secret: string; uri: string } | null>(null)
  const [confirmCode, setConfirmCode] = useState('')
  return (
    <Card withBorder>
      <Title order={5}>{t('settings.totp.title')}</Title>
      <Text size="sm" c="dimmed" mb="xs">
        {t('settings.totp.note-prefix')} <Code>kite web admin --reset</Code>.
      </Text>
      {!pending ? (
        <Stack gap="xs">
          <PasswordInput label={t('settings.totp.password')} value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
          <TextInput label={t('settings.totp.current-code')} value={code} onChange={(e) => setCode(e.currentTarget.value)} maxLength={6} inputMode="numeric" />
          <Group>
            <Button
              variant="light"
              disabled={!password || code.length !== 6}
              onClick={async () => {
                const res = await attempt(() => post<{ secret: string; uri: string }>('/api/auth/totp/begin', { password, code }))
                if (res) {
                  setPending(res)
                  setPassword('')
                  setCode('')
                }
              }}
            >
              {t('settings.totp.create')}
            </Button>
          </Group>
        </Stack>
      ) : (
        <Stack gap="xs">
          <Text size="sm">{t('settings.totp.add')}</Text>
          <Group gap="xs">
            <Code style={{ fontSize: 14 }}>{pending.secret}</Code>
            <CopyButton value={pending.secret}>
              {({ copied, copy }) => (
                <Button size="compact-xs" variant="light" onClick={copy}>
                  {copied ? t('settings.totp.copied') : t('settings.totp.copy')}
                </Button>
              )}
            </CopyButton>
          </Group>
          <TextInput label={t('settings.totp.new-code')} value={confirmCode} onChange={(e) => setConfirmCode(e.currentTarget.value)} maxLength={6} inputMode="numeric" />
          <Group>
            <Button
              disabled={confirmCode.length !== 6}
              onClick={async () => {
                const res = await attempt(() => post('/api/auth/totp/confirm', { code: confirmCode }), t('settings.totp.confirmed'))
                if (res) {
                  setPending(null)
                  setConfirmCode('')
                }
              }}
            >
              {t('settings.totp.confirm')}
            </Button>
            <Button variant="default" onClick={() => setPending(null)}>
              {t('common.cancel')}
            </Button>
          </Group>
        </Stack>
      )}
    </Card>
  )
}

function Sessions() {
  const t = useT()
  const me = useKite((s) => s.me)
  const sessions = (me?.sessions ?? []) as Record<string, any>[]
  return (
    <Card withBorder>
      <Group justify="space-between" mb="xs">
        <Title order={5}>{t('settings.sessions.title')}</Title>
        <ConfirmButton
          size="xs"
          variant="light"
          color="red"
          label={t('settings.sessions.logout-others')}
          title={t('settings.sessions.logout-others-title')}
          confirmLabel={t('settings.sessions.logout')}
          onConfirm={async () => {
            await attempt(() => post('/api/auth/logout-others'), t('settings.sessions.logged-out'))
            loadMe()
          }}
        />
      </Group>
      {sessions.length ? (
        <Table>
          <Table.Tbody>
            {sessions.map((s, i) => (
              <Table.Tr key={i}>
                <Table.Td>
                  <Text size="sm">{s.ip}</Text>
                  <Text size="xs" c="dimmed" lineClamp={1}>
                    {s.agent}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {ago(s.last_seen)}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <Empty>{t('settings.sessions.empty')}</Empty>
      )}
    </Card>
  )
}

function Update() {
  const t = useT()
  const plan = useLoad(() => get<Record<string, any>>('/api/system/update'), [])
  const [result, setResult] = useState<Record<string, any> | null>(null)
  const p = plan.data
  return (
    <Card withBorder>
      <Group justify="space-between" mb="xs">
        <Title order={5}>{t('settings.update.title')}</Title>
        <Button size="xs" variant="subtle" onClick={plan.reload}>
          {t('settings.update.check')}        </Button>
      </Group>
      {!p ? (
        <Loading error={plan.error} />
      ) : p.deploy === 'compose' ? (
        <StackUpdate plan={p} reload={plan.reload} onResult={setResult} />
      ) : p.error ? (
        <Alert color="red">{p.error_code === 'no_upstream' ? t('settings.update.no-upstream') : p.error}</Alert>
      ) : p.behind ? (
        <Stack gap="xs">
          <Text size="sm">{t('settings.update.behind', { count: p.behind })}</Text>
          <Code block>{(p.commits ?? []).join('\n')}</Code>
          {p.dirty?.length ? <Alert color="yellow">{t('settings.update.dirty', { files: p.dirty.join(', ') })}</Alert> : null}
          <Group>
            <ConfirmButton
              label={t('settings.update.button')}
              title={t('settings.update.title-confirm')}
              message={t('settings.update.message')}
              confirmLabel={t('settings.update.button')}
              onConfirm={async () => {
                const res = await attempt(() => post('/api/system/update'))
                if (res) {
                  setResult(res)
                  plan.reload()
                }
              }}
            />
          </Group>
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          {t('settings.update.up-to-date', { head: p.head })}
        </Text>
      )}
      {result?.queued ? (
        <Alert color="blue" mt="sm">
          {t('settings.update.queued')}
        </Alert>
      ) : result ? (
        <Stack gap="xs" mt="sm">
          <Alert color={result.updated ? 'teal' : 'red'}>
            {result.updated
              ? t('settings.update.updated', { tests: result.tests })
              : result.error_code === 'no_upstream'
                ? t('settings.update.no-upstream')
                : (result.error ?? result.note)}
            {result.ui_error ? <Text size="sm" c="red">{t('settings.update.ui-error', { error: result.ui_error })}</Text> : null}
            {result.updated ? <Text size="sm">{t('settings.update.reload')}</Text> : null}
          </Alert>
          <JsonView data={result} maxHeight={200} />
        </Stack>
      ) : null}
    </Card>
  )
}

const STACK_STATE: Record<string, DictKey> = {
  idle: 'settings.update.state.idle',
  checking: 'settings.update.state.checking',
  updating: 'settings.update.state.updating',
  rolling_back: 'settings.update.state.rolling-back',
  restarting: 'settings.update.state.restarting',
}
const STACK_RESULT: Record<string, DictKey> = {
  updated: 'settings.update.result.updated',
  up_to_date: 'settings.update.result.up-to-date',
  busy: 'settings.update.result.busy',
  rolled_back: 'settings.update.result.rolled-back',
  failed: 'settings.update.result.failed',
  restarted: 'settings.update.result.restarted',
}

/** The Compose deployment: kite-stack on the host pulls the image, checks it and keeps the old one on failure. */
function StackUpdate({
  plan,
  reload,
  onResult,
}: {
  plan: Record<string, any>
  reload: () => void
  onResult: (r: Record<string, any>) => void
}) {
  const t = useT()
  const st = plan.stack ?? {}
  const last = st.last ?? {}
  const working = st.state && st.state !== 'idle' && st.state !== 'unknown'
  useEffect(() => {
    if (!working) return
    const id = window.setInterval(reload, 5000)
    return () => window.clearInterval(id)
  }, [working, reload])
  return (
    <Stack gap="xs">
      {plan.error ? <Alert color="yellow">{plan.error}</Alert> : null}
      <Text size="sm">
        {t('settings.update.running')} <Code>{plan.head || '?'}</Code>
        {st.current?.created ? ` (build ${fmtTime(st.current.created)})` : ''}
        {plan.behind ? (
          <>
            {' '}
            · {t('settings.update.has-new')} <Code>{plan.target}</Code>
          </>
        ) : null}
      </Text>
      <Text size="sm" c="dimmed">
        {t('settings.update.auto-note', { state: st.auto === false ? t('settings.update.auto-off') : t('settings.update.auto-on') })}
        {st.state ? ` · ${STACK_STATE[st.state] ? t(STACK_STATE[st.state]) : st.state}` : ''}
      </Text>
      {last.result ? (
        <Alert color={last.result === 'rolled_back' || last.result === 'failed' ? 'red' : 'gray'}>
          {t('settings.update.last', { time: fmtTime(last.at), result: STACK_RESULT[last.result] ? t(STACK_RESULT[last.result]) : last.result })}
          {last.message ? ` — ${last.message}` : ''}
        </Alert>
      ) : null}
      <Group>
        <ConfirmButton
          label={t('settings.update.update-now')}
          title={t('settings.update.title-confirm')}
          message={t('settings.update.stack-message')}
          confirmLabel={t('settings.update.button')}
          onConfirm={async () => {
            const res = await attempt(() => post('/api/system/update'))
            if (res) {
              onResult(res)
              reload()
            }
          }}
        />
      </Group>
    </Stack>
  )
}

function Cleanup() {
  const t = useT()
  const [days, setDays] = useState<number>(7)
  const plan = useLoad(() => get<Record<string, any>>(`/api/system/cleanup${qs({ days })}`), [days])
  return (
    <Card withBorder>
      <Title order={5} mb="xs">
        {t('settings.cleanup.title')}
      </Title>
      <Group align="flex-end" gap="xs">
        <NumberInput label={t('settings.cleanup.older')} value={days} onChange={(v) => setDays(Number(v) || 7)} min={1} max={365} w={140} />
        <ConfirmButton
          variant="light"
          color="red"
          label={t('settings.cleanup.button')}
          title={t('settings.cleanup.title-confirm')}
          message={t('settings.cleanup.message')}
          confirmLabel={t('settings.cleanup.button')}
          onConfirm={async () => {
            await attempt(() => post('/api/system/cleanup', { days }), t('settings.cleanup.done'))
            plan.reload()
          }}
        />
      </Group>
      {plan.data ? (
        <Stack gap={4} mt="sm">
          {plan.data.folders ? (
            <>
              <Text size="sm">{t('settings.cleanup.preview', { folders: plan.data.folders, size: plan.data.size })}</Text>
              {(plan.data.biggest ?? []).map((b: string) => (
                <Text key={b} size="xs" c="dimmed">
                  <Code>{b}</Code>
                </Text>
              ))}
            </>
          ) : (
            <Text size="sm" c="dimmed">
              {t('settings.cleanup.empty')}
            </Text>
          )}
        </Stack>
      ) : (
        <Loading error={plan.error} />
      )}
    </Card>
  )
}

function Restart() {
  const t = useT()
  const restart = async (services: string[], force = false) => {
    const res = await attempt(() => post<Record<string, any>>('/api/system/restart', { services, force }))
    if (res) notifyOk(res.queued ? t('settings.restart.requested') : t('settings.restart.done'))
  }
  return (
    <Card withBorder>
      <Title order={5} mb="xs">
        {t('settings.restart.title')}
      </Title>
      <Text size="sm" c="dimmed" mb="xs">
        {t('settings.restart.note')}
      </Text>
      <Group gap="xs">
        <Button size="xs" variant="light" onClick={() => restart(['kite-gateway'])}>
          kite-gateway
        </Button>
        <Button size="xs" variant="light" onClick={() => restart(['kite-workflow-worker'])}>
          kite-workflow-worker
        </Button>
      </Group>
    </Card>
  )
}
