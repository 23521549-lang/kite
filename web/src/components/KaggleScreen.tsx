// Kite's own Chromium (on the server), shown on a canvas: its screen arrives as JPEG frames over the
// WebSocket, mouse and keyboard go back the same way. Only kaggle.com opens in it.
import { ActionIcon, Alert, Box, Button, Group, Select, Stack, Switch, Text, Tooltip } from '@mantine/core'
import { IconArrowLeft, IconRefresh } from '@tabler/icons-react'
import { useEffect, useRef, useState } from 'react'
import { wsUrl } from '../api'

type Status = {
  open: boolean
  url?: string
  title?: string
  blocked?: string
  busy?: boolean
  error?: string
  workers: { id: string; kernel: string; auto: boolean }[]
}

const BUTTONS = ['left', 'middle', 'right']

export default function KaggleScreen({ worker }: { worker?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const socket = useRef<WebSocket | null>(null)
  const size = useRef({ w: 1280, h: 800 })
  const lastMove = useRef(0)
  const [status, setStatus] = useState<Status | null>(null)
  const [note, setNote] = useState('Đăng nhập Kaggle bằng email và mật khẩu. Bật worker: Save Version → Save & Run All → Save.')
  const [connected, setConnected] = useState(false)
  const [selected, setSelected] = useState<string | null>(worker ?? null)

  const send = (message: Record<string, unknown>) => {
    const ws = socket.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message))
  }

  useEffect(() => {
    const ws = new WebSocket(wsUrl('/api/browser/ws'))
    socket.current = ws
    const image = new Image()
    image.onload = () => {
      const c = canvas.current
      if (!c) return
      const { w, h } = size.current
      if (c.width !== w || c.height !== h) {
        c.width = w
        c.height = h
      }
      c.getContext('2d')?.drawImage(image, 0, 0, w, h)
    }
    ws.onopen = () => setConnected(true)
    ws.onclose = (e) => {
      setConnected(false)
      setNote(e.code === 4502 ? 'Gateway không trả lời: kiểm tra dịch vụ kite-gateway.' : 'Mất kết nối với trình duyệt Kite.')
    }
    ws.onmessage = (e) => {
      let m: any
      try {
        m = JSON.parse(e.data)
      } catch {
        return
      }
      if (m.t === 'frame') {
        size.current = { w: m.w || size.current.w, h: m.h || size.current.h }
        image.src = `data:image/jpeg;base64,${m.d}`
      } else if (m.t === 'note') setNote(m.s)
      else if (m.t === 'status') {
        setStatus(m)
        if (m.blocked) setNote(`Đã chặn trang ngoài kaggle.com: ${m.blocked}`)
        if (m.error) setNote(m.error)
        if (!m.open) {
          const c = canvas.current
          const g = c?.getContext('2d')
          if (c && g) {
            g.fillStyle = '#000'
            g.fillRect(0, 0, c.width, c.height)
          }
        }
      }
    }
    return () => ws.close()
  }, [])

  useEffect(() => {
    if (!selected && status?.workers.length) setSelected(status.workers[0].id)
  }, [status, selected])

  const pos = (e: { clientX: number; clientY: number }) => {
    const c = canvas.current!
    const r = c.getBoundingClientRect()
    return { x: ((e.clientX - r.left) * size.current.w) / r.width, y: ((e.clientY - r.top) * size.current.h) / r.height }
  }
  const printable = (e: React.KeyboardEvent) => e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey
  const current = status?.workers.find((w) => w.id === selected)

  return (
    <Stack gap="xs">
      <Group gap="xs" wrap="wrap">
        <Tooltip label="Lùi">
          <ActionIcon variant="default" onClick={() => send({ t: 'nav', a: 'back' })} disabled={!status?.open}>
            <IconArrowLeft size={16} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Tải lại">
          <ActionIcon variant="default" onClick={() => send({ t: 'nav', a: 'reload' })} disabled={!status?.open}>
            <IconRefresh size={16} />
          </ActionIcon>
        </Tooltip>
        <Select
          size="xs"
          w={170}
          placeholder="Worker"
          data={(status?.workers ?? []).map((w) => ({ value: w.id, label: w.id }))}
          value={selected}
          onChange={setSelected}
          allowDeselect={false}
        />
        <Button
          size="xs"
          disabled={!selected || !connected}
          onClick={() => {
            send({ t: 'open', worker: selected })
            canvas.current?.focus()
          }}
        >
          Mở notebook
        </Button>
        <Switch
          size="sm"
          label="Kite tự bấm"
          disabled={!current}
          checked={!!current?.auto}
          onChange={(e) => {
            const on = e.currentTarget.checked
            if (on && !confirm('Bật "Kite tự bấm"? Điều khoản Kaggle cấm thao tác tự động ngoài API; bạn chịu rủi ro tài khoản.')) return
            send({ t: 'auto', worker: selected, on })
          }}
        />
        <Box style={{ flex: 1 }} />
        <Button size="xs" variant="default" disabled={!status?.open} onClick={() => send({ t: 'close' })}>
          Tắt trình duyệt
        </Button>
        <Button
          size="xs"
          variant="subtle"
          color="red"
          onClick={() => {
            if (confirm('Xóa phiên đăng nhập Kaggle đã lưu trên máy Kite?')) send({ t: 'logout' })
          }}
        >
          Quên đăng nhập
        </Button>
      </Group>
      <Text size="xs" c="dimmed" truncate>
        {status?.open
          ? `${status.busy ? '[Kite đang bấm] ' : ''}${status.title ?? ''} — ${status.url ?? ''}`
          : connected
            ? 'Trình duyệt Kite đang tắt: bấm "Mở notebook"'
            : 'Đang nối…'}
      </Text>
      {note ? (
        <Alert color="yellow" py={6}>
          {note}
        </Alert>
      ) : null}
      <canvas
        ref={canvas}
        className="screen"
        tabIndex={0}
        width={1280}
        height={800}
        onMouseMove={(e) => {
          const t = performance.now()
          if (t - lastMove.current < 40) return
          lastMove.current = t
          send({ t: 'mouse', a: 'move', ...pos(e) })
        }}
        onMouseDown={(e) => {
          e.preventDefault()
          canvas.current?.focus()
          send({ t: 'mouse', a: 'down', b: BUTTONS[e.button] ?? 'left', c: e.detail || 1, ...pos(e) })
        }}
        onMouseUp={(e) => send({ t: 'mouse', a: 'up', b: BUTTONS[e.button] ?? 'left', c: e.detail || 1, ...pos(e) })}
        onWheel={(e) => send({ t: 'mouse', a: 'wheel', dx: e.deltaX, dy: e.deltaY, ...pos(e) })}
        onContextMenu={(e) => e.preventDefault()}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === 'v') return
          e.preventDefault()
          send(printable(e) ? { t: 'text', s: e.key } : { t: 'key', a: 'down', k: e.key })
        }}
        onKeyUp={(e) => {
          if (!printable(e)) {
            e.preventDefault()
            send({ t: 'key', a: 'up', k: e.key })
          }
        }}
        onPaste={(e) => {
          e.preventDefault()
          send({ t: 'text', s: e.clipboardData.getData('text') })
        }}
      />
    </Stack>
  )
}
