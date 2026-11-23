import { Alert, Button, Center, Paper, PasswordInput, PinInput, Stack, Text, TextInput, Title } from '@mantine/core'
import { useState } from 'react'
import { post, type Me } from '../api'
import { loadMe } from '../store'

export default function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await post<Pick<Me, 'username' | 'csrf'>>('/api/auth/login', { username: username.trim(), password, code })
      setPassword('')
      setCode('')
      await loadMe()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setCode('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Center mih="100vh" p="md">
      <Paper withBorder p="xl" w={380} radius="lg">
        <form
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <Stack>
            <div>
              <Title order={2}>Kite</Title>
              <Text c="dimmed" size="sm">
                Đăng nhập bằng mật khẩu và mã 2FA trong app xác thực.
              </Text>
            </div>
            <TextInput label="Tên đăng nhập" value={username} onChange={(e) => setUsername(e.currentTarget.value)} autoComplete="username" required />
            <PasswordInput label="Mật khẩu" value={password} onChange={(e) => setPassword(e.currentTarget.value)} autoComplete="current-password" required />
            <div>
              <Text size="sm" fw={500} mb={4}>
                Mã 2FA
              </Text>
              <PinInput length={6} type="number" oneTimeCode value={code} onChange={setCode} onComplete={() => undefined} />
            </div>
            {error ? <Alert color="red">{error}</Alert> : null}
            <Button type="submit" loading={busy} disabled={!username || !password || code.length !== 6}>
              Đăng nhập
            </Button>
          </Stack>
        </form>
      </Paper>
    </Center>
  )
}
