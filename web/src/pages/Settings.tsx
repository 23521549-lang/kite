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
import { useState } from 'react'
import { get, post, qs } from '../api'
import { ConfirmButton, Empty, JsonView, KeyValue, Loading, Page } from '../components/common'
import { loadMe, useKite } from '../store'
import { ago, attempt, fmtTime, useLoad } from '../util'

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
                  ['Phiên bản', `${s.version?.commit ?? '?'} · ${s.version?.subject ?? ''}`],
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
          <Doctor />
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
      {result ? (
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

function Doctor() {
  const doctor = useLoad(() => get<Record<string, any>>('/api/system/doctor'), [])
  const d = doctor.data
  return (
    <Card withBorder>
      <Group justify="space-between" mb="xs">
        <Title order={5}>Sức khỏe</Title>
        <Button size="xs" variant="subtle" onClick={doctor.reload}>
          Kiểm tra lại
        </Button>
      </Group>
      {!d ? (
        <Loading error={doctor.error} label="Đang kiểm tra…" />
      ) : (
        <Stack gap="xs">
          <Badge color={d.fail ? 'red' : d.warn ? 'yellow' : 'teal'} variant="light">
            {d.fail ? `${d.fail} lỗi` : d.warn ? `${d.warn} cảnh báo` : 'ổn'}
          </Badge>
          {(d.problems ?? []).map((p: any, i: number) => (
            <Alert key={i} color={p.status === 'lỗi' ? 'red' : 'yellow'} title={p.check} py={6}>
              <Text size="sm">{p.detail}</Text>
              {p.fix ? (
                <Text size="xs" mt={4}>
                  Cách sửa: <Code>{p.fix}</Code>
                </Text>
              ) : null}
            </Alert>
          ))}
          {d.gpu_quota ? <Text size="sm">Quota GPU: {typeof d.gpu_quota === 'string' ? d.gpu_quota : JSON.stringify(d.gpu_quota)}</Text> : null}
          <Text size="xs" c="dimmed">
            Ổn: {(d.ok ?? []).join(', ')}
          </Text>
        </Stack>
      )}
    </Card>
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
    await attempt(() => post('/api/system/restart', { services, force }), 'Đã khởi động lại')
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
