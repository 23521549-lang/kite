// React side of the 3D office: makes the scene once, hands it the latest state, and turns the live
// gateway events into dots flying to the worker that answered.
import { Box, Paper, Text } from '@mantine/core'
import { useEffect, useRef, useState } from 'react'
import { fmt, useOverviewStrings } from '../i18n/overview'
import { useKite } from '../store'
import { OfficeScene, type OfficeLabels, type OfficeState, type Pick } from './scene'

export default function Office3D({
  state,
  labels,
  onPick,
  height,
}: {
  state: OfficeState
  labels: OfficeLabels
  onPick: (pick: Pick) => void
  height: number
}) {
  const s = useOverviewStrings()
  const holder = useRef<HTMLDivElement>(null)
  const scene = useRef<OfficeScene | null>(null)
  const pick = useRef(onPick)
  pick.current = onPick
  const [tip, setTip] = useState<{ text: string; x: number; y: number } | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    const el = holder.current
    if (!el) return
    try {
      scene.current = new OfficeScene(el, {
        onPick: (p) => pick.current(p),
        onHover: (text, x, y) => {
          if (!text) return setTip(null)
          const rect = el.getBoundingClientRect()
          setTip({ text, x: x - rect.left, y: y - rect.top })
        },
        labels,
      })
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e))
    }
    return () => {
      scene.current?.dispose()
      scene.current = null
    }
    // Labels lúc khởi tạo là đủ — đổi ngôn ngữ sau đó đi qua `update(state, labels)` bên dưới.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    scene.current?.update(state, labels)
  }, [state, labels])

  // Gateway requests from the live events: newest first in the store, so walk the new ones oldest first.
  const seen = useRef(Date.now())
  useEffect(
    () =>
      useKite.subscribe((s) => {
        const fresh = s.events.filter((e) => e.at > seen.current && e.file === 'gateway')
        if (!s.events.length) return
        seen.current = Math.max(seen.current, s.events[0].at)
        for (const e of fresh.reverse()) {
          const r = e.record
          if (r.worker) scene.current?.pulse(String(r.worker), r.tag === 'chat', !r.error && (r.outcome ?? 'ok') === 'ok')
        }
      }),
    [],
  )

  if (failed)
    return (
      <Text c="dimmed" size="sm">
        {fmt(s['overview.office.failed'], { error: failed })}
      </Text>
    )
  return (
    <Box pos="relative" h={height} style={{ borderRadius: 8, overflow: 'hidden' }} onMouseLeave={() => setTip(null)}>
      <div ref={holder} style={{ position: 'absolute', inset: 0 }} />
      {tip ? (
        <Paper
          shadow="md"
          px={8}
          py={4}
          radius="sm"
          withBorder
          style={{
            position: 'absolute',
            left: Math.min(tip.x + 14, (holder.current?.clientWidth ?? 600) - 260),
            top: tip.y + 14,
            maxWidth: 260,
            pointerEvents: 'none',
            zIndex: 2,
          }}
        >
          <Text size="xs">{tip.text}</Text>
        </Paper>
      ) : null}
      <Text size="xs" c="dimmed" style={{ position: 'absolute', left: 10, bottom: 6, pointerEvents: 'none' }}>
        {s['overview.office.hint']}
      </Text>
    </Box>
  )
}
