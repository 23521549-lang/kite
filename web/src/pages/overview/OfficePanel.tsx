// Văn phòng Kite ở trung tâm trang Tổng quan: 3D (tải lười) + fallback 2D bằng thẻ worker.
//
// Interface đúng hợp đồng Gói 9: worker (a) import dùng với
// `{ office, workers, webgl, small, onPick }`. `workers` (WorkerRow đầy đủ)
// phục vụ thẻ 2D — `office.workers` (WorkerView) không đủ trường cho logic
// `workerState()` + danh sách model + chi tiết lỗi khởi động.
import { Badge, Card, Group, SegmentedControl, SimpleGrid, Text } from '@mantine/core'
import { Suspense, lazy, useMemo, useState } from 'react'
import type { WorkerRow } from '../../api'
import { Dot, Empty, Loading } from '../../components/common'
import { fmt, useOverviewStrings } from '../../i18n/overview'
import type { OfficeLabels, OfficeState, Pick } from '../../office/scene'
import { go } from '../../router'
import { short, workerState } from '../../util'

// three.js chỉ tải khi hiển thị 3D — giữ nguyên pattern lazy import, không
// import three.js tĩnh ở đây (Office3D tự import scene của nó).
const Office3D = lazy(() => import('../../office/Office3D'))

const MODE_KEY = 'kite.overview.mode'

function savedMode(): '3d' | '2d' {
  try {
    const value = localStorage.getItem(MODE_KEY)
    if (value === '2d' || value === '3d') return value
  } catch {
    /* no storage: the default */
  }
  // Phones start with the cards: five desks on a small screen are hard to read.
  return window.matchMedia?.('(max-width: 640px)').matches ? '2d' : '3d'
}

export default function OfficePanel({
  office,
  workers,
  webgl,
  small,
  onPick,
}: {
  office: OfficeState | null
  workers: WorkerRow[]
  webgl: boolean
  small: boolean
  onPick: (pick: Pick) => void
}) {
  const s = useOverviewStrings()
  const [mode, setMode] = useState<'3d' | '2d'>(() => (webgl ? savedMode() : '2d'))
  // Nhãn trong scene 3D theo ngôn ngữ hiện tại — đổi ngôn ngữ thì `update()`
  // vẽ lại nhãn (key cache trong scene có cả chuỗi nhãn nên không bị sót).
  const labels = useMemo<OfficeLabels>(
    () => ({
      moreTasks: (n) => fmt(s['overview.office.more-tasks'], { n }),
      openTask: s['overview.office.open-task'],
      aria: s['overview.office.aria'],
      queueTip: s['overview.office.queue-tip'],
      gatewayTip: s['overview.office.gateway-tip'],
      gatewayUp: s['overview.office.gateway-up'],
      gatewayDown: s['overview.office.gateway-down'],
      workerTip: s['overview.office.worker-tip'],
      runningSuffix: (running, slots) => fmt(s['overview.office.running-suffix'], { running, slots }),
      leadTip: s['overview.office.lead-tip'],
      leadName: s['overview.office.lead-name'],
      answering: s['overview.office.answering'],
      waiting: s['overview.office.waiting'],
      queueName: s['overview.office.queue-name'],
      queueWaiting: (n) => fmt(s['overview.office.queue-waiting'], { n }),
      queueEmpty: s['overview.office.queue-empty'],
    }),
    [s],
  )
  return (
    <Card withBorder p="xs">
      <Group justify="space-between" px={6} pb={6}>
        <Text fw={600}>{s['overview.office.title']}</Text>
        <SegmentedControl
          size="xs"
          data={[
            { value: '3d', label: '3D', disabled: !webgl },
            { value: '2d', label: '2D' },
          ]}
          value={mode}
          onChange={(v) => {
            const next = v === '3d' ? '3d' : '2d'
            setMode(next)
            try {
              localStorage.setItem(MODE_KEY, next)
            } catch {
              /* the choice lasts for this visit only */
            }
          }}
        />
      </Group>
      {!office ? (
        <Loading />
      ) : mode === '3d' ? (
        <Suspense fallback={<Loading label={s['overview.office.loading']} />}>
          <Office3D state={office} labels={labels} onPick={onPick} height={small ? 360 : 500} />
        </Suspense>
      ) : (
        <WorkerCards workers={workers} />
      )}
      {!webgl ? (
        <Text size="xs" c="dimmed" px={6} pt={6}>
          {s['overview.office.no-webgl']}
        </Text>
      ) : null}
    </Card>
  )
}

// Thẻ worker 2D — giữ nguyên logic cũ (nhãn + màu từ `workerState()`, danh
// sách model, dòng chi tiết lỗi khởi động); chỉ đổi chuỗi UI sang i18n.
function WorkerCards({ workers }: { workers: WorkerRow[] }) {
  const s = useOverviewStrings()
  if (!workers.length) return <Empty>{s['overview.workers.empty']}</Empty>
  return (
    <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} p={6}>
      {workers.map((w) => {
        const st = workerState(w)
        return (
          <Card key={w.id} withBorder className="clickable" onClick={() => go('/workers')}>
            <Group justify="space-between">
              <Group gap={8}>
                <Dot color={st.color} />
                <Text fw={600}>{w.id}</Text>
              </Group>
              <Badge variant="light" color={st.color}>
                {st.label}
              </Badge>
            </Group>
            <Text size="sm" c="dimmed" mt={4}>
              {w.type} · {w.loaded_model ? fmt(s['overview.worker.loading-model'], { model: w.loaded_model }) : w.models.slice(0, 3).join(', ')}
            </Text>
            {w.alive && w.busy ? (
              <Text size="sm" mt={4}>
                {fmt(s['overview.worker.running'], { running: w.running, slots: w.slots ?? w.concurrency })}
              </Text>
            ) : null}
            {w.start?.detail ? (
              <Text size="xs" c="yellow" mt={4}>
                {short(w.start.detail, 140)}
              </Text>
            ) : null}
          </Card>
        )
      })}
    </SimpleGrid>
  )
}
