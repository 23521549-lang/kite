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

export default function Settings() {
  const settings = useLoad(() => get<Record<string, any>>('/api/settings'), [])
  const s = settings.data
  return (
    <Page title="Cài đặt">
      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Stack>
          <Account />
          <TwoFactor />
          <Sessions />
        </Stack>
        <Stack>
          <Card withBorder>
            <Title order={5} mb="xs">
              Kite trên máy này
            </Title>
            {!s ? (
              <Loading error={settings.error} />
            ) : (
              <KeyValue
                rows={[
                  ['Phiên bản', [s.version?.commit || '?', s.version?.subject].filter(Boolean).join(' · ')],
                  ['Ngày', fmtTime(s.version?.date)],
                  ['Gateway', `${s.gateway.host}:${s.gateway.port}`],
                  ['Địa chỉ cho worker', s.gateway.public_url ?? 'chưa đặt'],
                  ['Thư mục dữ liệu', <Code key="d">{s.state_dir}</Code>],
                  [
                    'Thông báo ntfy',
                    s.ntfy ? (
                      <Group key="n" gap="xs">
                        <Badge color="teal" variant="light">
                          đã cài
                        </Badge>
                        <Button size="compact-xs" variant="light" onClick={() => attempt(() => post('/api/settings/ntfy/test'), 'Đã gửi thông báo thử')}>
                          Gửi thử
                        </Button>
                      </Group>
                    ) : (
                      'chưa cài (kite notify setup)'
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
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [again, setAgain] = useState('')
  const [code, setCode] = useState('')
  const me = useKite((s) => s.me)
  return (
    <Card withBorder>
      <Title order={5}>Tài khoản {me?.username}</Title>
      <Text size="sm" c="dimmed" mb="xs">
        Đổi mật khẩu (ít nhất 12 ký tự). Các trình duyệt khác bị đăng xuất.
      </Text>
      <Stack gap="xs">
        <PasswordInput label="Mật khẩu hiện tại" value={current} onChange={(e) => setCurrent(e.currentTarget.value)} autoComplete="current-password" />
        <PasswordInput label="Mật khẩu mới" value={next} onChange={(e) => setNext(e.currentTarget.value)} autoComplete="new-password" />
        <PasswordInput
          label="Nhập lại mật khẩu mới"
          value={again}
          onChange={(e) => setAgain(e.currentTarget.value)}
          error={again && again !== next ? 'không khớp' : undefined}
          autoComplete="new-password"
        />
        <TextInput label="Mã 2FA" value={code} onChange={(e) => setCode(e.currentTarget.value)} inputMode="numeric" maxLength={6} />
        <Group>
          <Button
            disabled={!current || next.length < 12 || next !== again || code.length !== 6}
            onClick={async () => {
              const res = await attempt(() => post('/api/auth/password', { current, new: next, code }), 'Đã đổi mật khẩu')
              if (res) {
                setCurrent('')
                setNext('')
                setAgain('')
                setCode('')
              }
            }}
          >
            Đổi mật khẩu
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

function TwoFactor() {
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [pending, setPending] = useState<{ secret: string; uri: string } | null>(null)
  const [confirmCode, setConfirmCode] = useState('')
  return (
    <Card withBorder>
      <Title order={5}>Đổi app 2FA</Title>
      <Text size="sm" c="dimmed" mb="xs">
        Dùng khi đổi điện thoại. Mất điện thoại thì trên máy Kite chạy <Code>kite web admin --reset</Code>.
      </Text>
      {!pending ? (
        <Stack gap="xs">
          <PasswordInput label="Mật khẩu" value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
          <TextInput label="Mã 2FA hiện tại" value={code} onChange={(e) => setCode(e.currentTarget.value)} maxLength={6} inputMode="numeric" />
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
              Tạo khóa mới
            </Button>
          </Group>
        </Stack>
      ) : (
        <Stack gap="xs">
          <Text size="sm">Thêm khóa này vào app xác thực (chọn "Nhập khóa thiết lập"), rồi gõ mã 6 số app hiện:</Text>
          <Group gap="xs">
            <Code style={{ fontSize: 14 }}>{pending.secret}</Code>
            <CopyButton value={pending.secret}>
              {({ copied, copy }) => (
                <Button size="compact-xs" variant="light" onClick={copy}>
                  {copied ? 'Đã chép' : 'Chép'}
                </Button>
              )}
            </CopyButton>
          </Group>
          <TextInput label="Mã từ app mới" value={confirmCode} onChange={(e) => setConfirmCode(e.currentTarget.value)} maxLength={6} inputMode="numeric" />
          <Group>
            <Button
              disabled={confirmCode.length !== 6}
              onClick={async () => {
                const res = await attempt(() => post('/api/auth/totp/confirm', { code: confirmCode }), 'Đã đổi app 2FA')
                if (res) {
                  setPending(null)
                  setConfirmCode('')
                }
              }}
            >
              Xác nhận
            </Button>
            <Button variant="default" onClick={() => setPending(null)}>
              Thôi
            </Button>
          </Group>
        </Stack>
      )}
    </Card>
  )
}

function Sessions() {
  const me = useKite((s) => s.me)
  const sessions = (me?.sessions ?? []) as Record<string, any>[]
  return (
    <Card withBorder>
      <Group justify="space-between" mb="xs">
        <Title order={5}>Các nơi đang đăng nhập</Title>
        <ConfirmButton
          size="xs"
          variant="light"
          color="red"
          label="Đăng xuất nơi khác"
          title="Đăng xuất mọi trình duyệt khác?"
          confirmLabel="Đăng xuất"
          onConfirm={async () => {
            await attempt(() => post('/api/auth/logout-others'), 'Đã đăng xuất các nơi khác')
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
        <Empty>Không có.</Empty>
      )}
    </Card>
  )
}

function Update() {
  const plan = useLoad(() => get<Record<string, any>>('/api/system/update'), [])
  const [result, setResult] = useState<Record<string, any> | null>(null)
  const p = plan.data
  return (
    <Card withBorder>
      <Group justify="space-between" mb="xs">
        <Title order={5}>Cập nhật Kite</Title>
        <Button size="xs" variant="subtle" onClick={plan.reload}>
          Kiểm tra
        </Button>
      </Group>
      {!p ? (
        <Loading error={plan.error} />
      ) : p.deploy === 'compose' ? (
        <StackUpdate plan={p} reload={plan.reload} onResult={setResult} />
      ) : p.error ? (
        <Alert color="red">{p.error}</Alert>
      ) : p.behind ? (
        <Stack gap="xs">
          <Text size="sm">{p.behind} commit mới:</Text>
          <Code block>{(p.commits ?? []).join('\n')}</Code>
          {p.dirty?.length ? <Alert color="yellow">Repo Kite trên máy có file đang sửa: {p.dirty.join(', ')}</Alert> : null}
          <Group>
            <ConfirmButton
              label="Cập nhật"
              title="Cập nhật Kite?"
              message="Kite kéo bản mới, cài thư viện mới nếu có, chạy test (khoảng 1 phút), build lại giao diện, rồi khởi động lại dịch vụ. Test lỗi thì quay lại bản cũ. Không làm khi có task đang chạy."
              confirmLabel="Cập nhật"
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
          Đang là bản mới nhất ({p.head}).
        </Text>
      )}
      {result?.queued ? (
        <Alert color="blue" mt="sm">
          Đã gửi yêu cầu. kite-stack trên máy tải image mới, kiểm tra rồi chạy (vài phút); image lỗi thì tự quay về
          bản cũ. Xong thì tải lại trang.
        </Alert>
      ) : result ? (
        <Stack gap="xs" mt="sm">
          <Alert color={result.updated ? 'teal' : 'red'}>
            {result.updated ? `Đã cập nhật; test: ${result.tests}` : result.error ?? result.note}
            {result.ui_error ? <Text size="sm" c="red">Giao diện: {result.ui_error}</Text> : null}
            {result.updated ? <Text size="sm">Tải lại trang để dùng giao diện mới.</Text> : null}
          </Alert>
          <JsonView data={result} maxHeight={200} />
        </Stack>
      ) : null}
    </Card>
  )
}

const STACK_STATE: Record<string, string> = {
  idle: 'chờ',
  checking: 'đang kiểm tra bản mới',
  updating: 'đang cập nhật',
  rolling_back: 'đang quay về bản cũ',
  restarting: 'đang khởi động lại',
}
const STACK_RESULT: Record<string, string> = {
  updated: 'đã cập nhật',
  up_to_date: 'đang là bản mới nhất',
  busy: 'hoãn: có task hoặc lần đo đang chạy',
  rolled_back: 'bản mới lỗi, đã quay về bản cũ',
  failed: 'lỗi',
  restarted: 'đã khởi động lại',
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
        Đang chạy <Code>{plan.head || '?'}</Code>
        {st.current?.created ? ` (build ${fmtTime(st.current.created)})` : ''}
        {plan.behind ? (
          <>
            {' '}
            · có bản mới <Code>{plan.target}</Code>
          </>
        ) : null}
      </Text>
      <Text size="sm" c="dimmed">
        Tự cập nhật: {st.auto === false ? 'tắt' : 'bật'} (kite-stack kiểm tra nhãn stable mỗi 10 phút)
        {st.state ? ` · ${STACK_STATE[st.state] ?? st.state}` : ''}
      </Text>
      {last.result ? (
        <Alert color={last.result === 'rolled_back' || last.result === 'failed' ? 'red' : 'gray'}>
          Lần cuối ({fmtTime(last.at)}): {STACK_RESULT[last.result] ?? last.result}
          {last.message ? ` — ${last.message}` : ''}
        </Alert>
      ) : null}
      <Group>
        <ConfirmButton
          label="Cập nhật ngay"
          title="Cập nhật Kite?"
          message="kite-stack tải image mới nhất đã qua CI, chạy nó rồi kiểm tra từng dịch vụ. Dịch vụ nào không khỏe thì quay về image cũ. Không làm khi có task hoặc lần đo đang chạy."
          confirmLabel="Cập nhật"
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
  const [days, setDays] = useState<number>(7)
  const plan = useLoad(() => get<Record<string, any>>(`/api/system/cleanup${qs({ days })}`), [days])
  return (
    <Card withBorder>
      <Title order={5} mb="xs">
        Dọn ổ đĩa
      </Title>
      <Group align="flex-end" gap="xs">
        <NumberInput label="Cũ hơn (ngày)" value={days} onChange={(v) => setDays(Number(v) || 7)} min={1} max={365} w={140} />
        <ConfirmButton
          variant="light"
          color="red"
          label="Dọn"
          title="Xóa các bản sao repo, thư viện, bản đồ code và lần đo cũ?"
          message="Kết quả và nhật ký giữ lại."
          confirmLabel="Dọn"
          onConfirm={async () => {
            await attempt(() => post('/api/system/cleanup', { days }), 'Đã dọn')
            plan.reload()
          }}
        />
      </Group>
      {plan.data ? <JsonView data={plan.data} maxHeight={180} /> : <Loading error={plan.error} />}
    </Card>
  )
}

function Restart() {
  const restart = async (services: string[], force = false) => {
    const res = await attempt(() => post<Record<string, any>>('/api/system/restart', { services, force }))
    if (res) notifyOk(res.queued ? 'Đã gửi yêu cầu khởi động lại' : 'Đã khởi động lại')
  }
  return (
    <Card withBorder>
      <Title order={5} mb="xs">
        Khởi động lại dịch vụ
      </Title>
      <Text size="sm" c="dimmed" mb="xs">
        Cần sau khi sửa worker. Không làm được khi có task đang chạy.
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
