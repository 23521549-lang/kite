// Kite's own Chromium (on the server), shown on a canvas: its screen arrives as JPEG frames over the
// WebSocket, mouse and keyboard go back the same way. Only kaggle.com opens in it.
import { ActionIcon, Alert, Box, Button, Group, Select, Stack, Switch, Text, Tooltip } from '@mantine/core'
import { IconArrowLeft, IconRefresh } from '@tabler/icons-react'
import { useEffect, useRef, useState } from 'react'
import { wsUrl } from '../api'
import { useT } from '../i18n'

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
  const t = useT()
  // The WebSocket handlers below close over the first render: keep the
  // current translator in a ref so a language switch also translates later notes.
  const tRef = useRef(t)
  tRef.current = t
  const canvas = useRef<HTMLCanvasElement>(null)
  const socket = useRef<WebSocket | null>(null)
  const size = useRef({ w: 1280, h: 800 })
  const lastMove = useRef(0)
  const [status, setStatus] = useState<Status | null>(null)
  const [note, setNote] = useState(t('workers.kaggle.login-hint'))
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
      setNote(e.code === 4502 ? tRef.current('workers.kaggle.gateway-down') : tRef.current('workers.kaggle.disconnected'))
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
        if (m.blocked) setNote(tRef.current('workers.kaggle.blocked-outside', { blocked: m.blocked }))
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
        <Tooltip label={t('workers.kaggle.back')}>
          <ActionIcon variant="default" onClick={() => send({ t: 'nav', a: 'back' })} disabled={!status?.open}>
            <IconArrowLeft size={16} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label={t('workers.kaggle.reload')}>
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
          {t('workers.kaggle.open-notebook')}
        </Button>
        <Switch
          size="sm"
          label={t('workers.kaggle.auto-click')}
          disabled={!current}
          checked={!!current?.auto}
          onChange={(e) => {
            const on = e.currentTarget.checked
            if (on && !confirm(t('workers.kaggle.auto-click-confirm'))) return
            send({ t: 'auto', worker: selected, on })
          }}
        />
        <Box style={{ flex: 1 }} />
        <Button size="xs" variant="default" disabled={!status?.open} onClick={() => send({ t: 'close' })}>
          {t('workers.kaggle.close-browser')}
        </Button>
        <Button
          size="xs"
          variant="subtle"
          color="red"
          onClick={() => {
            if (confirm(t('workers.kaggle.forget-login-confirm'))) send({ t: 'logout' })
          }}
        >
          {t('workers.kaggle.forget-login')}
        </Button>
      </Group>
      <Text size="xs" c="dimmed" truncate>
        {status?.open
          ? `${status.busy ? `${t('workers.kaggle.clicking')} ` : ''}${status.title ?? ''} — ${status.url ?? ''}`
          : connected
            ? t('workers.kaggle.browser-off')
            : t('workers.kaggle.connecting')}
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
