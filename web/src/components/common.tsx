import {
  Alert,
  Badge,
  Box,
  Button,
  Code,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
  Textarea,
  Title,
  type ButtonProps,
} from '@mantine/core'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useMemo, useState, type ReactNode } from 'react'
import { taskState } from '../util'

export function Page({ title, actions, children }: { title: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <Stack gap="md">
      <Group justify="space-between" align="center" wrap="wrap">
        <Title order={2}>{title}</Title>
        {actions ? <Group gap="xs">{actions}</Group> : null}
      </Group>
      {children}
    </Stack>
  )
}

export function Loading({ error, label }: { error?: string | null; label?: string }) {
  if (error) return <Alert color="red" title="Không tải được">{error}</Alert>
  return (
    <Group gap="xs" c="dimmed">
      <Loader size="sm" />
      <Text size="sm">{label ?? 'Đang tải…'}</Text>
    </Group>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <Text c="dimmed" size="sm" py="sm">
      {children}
    </Text>
  )
}

export function JsonView({ data, maxHeight = 360 }: { data: unknown; maxHeight?: number }) {
  const text = useMemo(() => (typeof data === 'string' ? data : JSON.stringify(data, null, 2)), [data])
  return (
    <ScrollArea.Autosize mah={maxHeight} type="auto">
      <Code block className="pre">
        {text}
      </Code>
    </ScrollArea.Autosize>
  )
}

marked.setOptions({ gfm: true, breaks: true })

export function Markdown({ text }: { text: string }) {
  const html = useMemo(() => {
    const raw = marked.parse(text ?? '', { async: false }) as string
    return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true }, FORBID_TAGS: ['style', 'form', 'input'] })
  }, [text])
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />
}

export function DiffView({ patch, maxHeight = 520 }: { patch: string; maxHeight?: number }) {
  const lines = useMemo(() => patch.split('\n'), [patch])
  return (
    <ScrollArea.Autosize mah={maxHeight} type="auto">
      <pre className="diff">
        {lines.map((line, i) => {
          const kind = line.startsWith('+++') || line.startsWith('---')
            ? 'file'
            : line.startsWith('@@')
              ? 'hunk'
              : line.startsWith('diff ')
                ? 'head'
                : line.startsWith('+')
                  ? 'add'
                  : line.startsWith('-')
                    ? 'del'
                    : ''
          return (
            <div key={i} className={kind ? `d-${kind}` : undefined}>
              {line || ' '}
            </div>
          )
        })}
      </pre>
    </ScrollArea.Autosize>
  )
}

export function StateBadge({ state }: { state: string }) {
  const s = taskState(state)
  return (
    <Badge color={s.color} variant="light" size="sm">
      {s.label}
    </Badge>
  )
}

export function Dot({ color, title }: { color: string; title?: string }) {
  return <span className="dot" title={title} style={{ background: `var(--mantine-color-${color}-6)` }} />
}

/** A button that asks before it acts. With `askText`, the dialog also takes a text (a commit message…). */
export function ConfirmButton({
  label,
  title,
  message,
  confirmLabel = 'Đồng ý',
  onConfirm,
  askText,
  textLabel,
  textDefault = '',
  color,
  ...props
}: {
  label: ReactNode
  title: string
  message?: ReactNode
  confirmLabel?: string
  onConfirm: (text: string) => Promise<unknown> | unknown
  askText?: boolean
  textLabel?: string
  textDefault?: string
} & ButtonProps) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [text, setText] = useState(textDefault)
  return (
    <>
      <Button
        color={color}
        {...props}
        onClick={() => {
          setText(textDefault)
          setOpen(true)
        }}
      >
        {label}
      </Button>
      <Modal opened={open} onClose={() => setOpen(false)} title={title} centered>
        <Stack>
          {message ? <Box>{message}</Box> : null}
          {askText ? (
            <Textarea label={textLabel} value={text} onChange={(e) => setText(e.currentTarget.value)} autosize minRows={2} />
          ) : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setOpen(false)}>
              Thôi
            </Button>
            <Button
              color={color}
              loading={busy}
              disabled={askText && !text.trim()}
              onClick={async () => {
                setBusy(true)
                try {
                  await onConfirm(text)
                  setOpen(false)
                } finally {
                  setBusy(false)
                }
              }}
            >
              {confirmLabel}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  )
}

/** Simple bar chart in SVG: one bar per item, the value on hover. */
export function Bars({
  items,
  height = 120,
  color = 'indigo',
  format = (v: number) => String(v),
}: {
  items: { label: string; value: number }[]
  height?: number
  color?: string
  format?: (v: number) => string
}) {
  if (!items.length) return <Empty>Chưa có số liệu.</Empty>
  const max = Math.max(...items.map((i) => i.value), 1)
  const w = 100 / items.length
  return (
    <Box>
      <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" width="100%" height={height} role="img">
        {items.map((item, i) => {
          const h = (item.value / max) * (height - 4)
          return (
            <rect
              key={item.label}
              x={i * w + w * 0.12}
              y={height - h}
              width={w * 0.76}
              height={Math.max(h, item.value ? 1 : 0)}
              rx={0.6}
              fill={`var(--mantine-color-${color}-5)`}
            >
              <title>{`${item.label}: ${format(item.value)}`}</title>
            </rect>
          )
        })}
      </svg>
      <Group justify="space-between" gap={0}>
        <Text size="xs" c="dimmed">
          {items[0].label}
        </Text>
        <Text size="xs" c="dimmed">
          cao nhất {format(max)}
        </Text>
        <Text size="xs" c="dimmed">
          {items[items.length - 1].label}
        </Text>
      </Group>
    </Box>
  )
}

export function KeyValue({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <Stack gap={4}>
      {rows.map(([k, v], i) => (
        <Group key={i} gap="xs" wrap="nowrap" align="flex-start">
          <Text size="sm" c="dimmed" w={150} style={{ flexShrink: 0 }}>
            {k}
          </Text>
          <Box style={{ minWidth: 0, flex: 1 }}>{typeof v === 'string' || typeof v === 'number' ? <Text size="sm">{v}</Text> : v}</Box>
        </Group>
      ))}
    </Stack>
  )
}
