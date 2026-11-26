// The Kite office at the center of the Overview page: 3D (lazy-loaded) + a 2D fallback of worker cards.
//
// Interface per the Gói 9 contract: worker (a) imports this with
// `{ office, workers, webgl, small, onPick }`. `workers` (full WorkerRow)
// serves the 2D cards — `office.workers` (WorkerView) lacks the fields for the
// `workerState()` logic + model list + startup error detail.
import { Badge, Card, Group, SegmentedControl, SimpleGrid, Text } from '@mantine/core'
import { Suspense, lazy, useMemo, useState } from 'react'
import type { WorkerRow } from '../../api'
import { Dot, Empty, Loading } from '../../components/common'
import { useLanguage, useT } from '../../i18n'
import type { OfficeLabels, OfficeState, Pick } from '../../office/scene'
import { go } from '../../router'
import { short, workerState } from '../../util'

// three.js only loads when 3D shows — keep the lazy-import pattern, no static
// three.js import here (Office3D imports its own scene).
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
  const t = useT()
  const { language } = useLanguage()
  const [mode, setMode] = useState<'3d' | '2d'>(() => (webgl ? savedMode() : '2d'))
  // Labels inside the 3D scene follow the current language — a language change makes
  // `update()` repaint the labels (the scene cache key includes the label strings, so none is missed).
  const labels = useMemo<OfficeLabels>(
    () => ({
      moreTasks: (n) => t('overview.office.more-tasks', { n }),
      openTask: t('overview.office.open-task'),
      aria: t('overview.office.aria'),
      queueTip: t('overview.office.queue-tip'),
      gatewayTip: t('overview.office.gateway-tip'),
      gatewayUp: t('overview.office.gateway-up'),
      gatewayDown: t('overview.office.gateway-down'),
      workerTip: t('overview.office.worker-tip'),
      runningSuffix: (running, slots) => t('overview.office.running-suffix', { running, slots }),
      leadTip: t('overview.office.lead-tip'),
      leadName: t('overview.office.lead-name'),
      answering: t('overview.office.answering'),
      waiting: t('overview.office.waiting'),
      queueName: t('overview.office.queue-name'),
      queueWaiting: (n) => t('overview.office.queue-waiting', { n }),
      queueEmpty: t('overview.office.queue-empty'),
    }),
    [language],
  )
  return (
    <Card withBorder p="xs">
      <Group justify="space-between" px={6} pb={6}>
        <Text fw={600}>{t('overview.office.title')}</Text>
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
        <Suspense fallback={<Loading label={t('overview.office.loading')} />}>
          <Office3D state={office} labels={labels} onPick={onPick} height={small ? 360 : 500} />
        </Suspense>
      ) : (
        <WorkerCards workers={workers} />
      )}
      {!webgl ? (
        <Text size="xs" c="dimmed" px={6} pt={6}>
          {t('overview.office.no-webgl')}
        </Text>
      ) : null}
    </Card>
  )
}

// 2D worker cards — keep the old logic (label + color from `workerState()`, model
// list, startup error detail line); only the UI strings move to i18n.
function WorkerCards({ workers }: { workers: WorkerRow[] }) {
  const t = useT()
  if (!workers.length) return <Empty>{t('overview.workers.empty')}</Empty>
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
              {w.type} · {w.loaded_model ? t('overview.worker.loading-model', { model: w.loaded_model }) : w.models.slice(0, 3).join(', ')}
            </Text>
            {w.alive && w.busy ? (
              <Text size="sm" mt={4}>
                {t('overview.worker.running', { running: w.running, slots: w.slots ?? w.concurrency })}
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
