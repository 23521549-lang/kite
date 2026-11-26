// Connect tab (Gói 5): the tool store — MCP servers, skills and API platforms.
// Every new MCP/skill goes through scan → owner approval → enable. Enabling is
// blocked in the UI unless the latest scan passed and the lock matches; the
// back end enforces the same rule. Tools here serve the lead agent only and
// never enter a worker sandbox.
import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  List,
  Modal,
  PasswordInput,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core'
import { IconPlus, IconShieldCheck } from '@tabler/icons-react'
import { useState } from 'react'
import {
  createConnectMcp,
  createConnectSkill,
  deleteConnectMcp,
  deleteConnectSkill,
  disableConnectMcp,
  disableConnectSkill,
  enableConnectMcp,
  enableConnectSkill,
  getConnectInventory,
  getConnectMcpScan,
  getConnectSkillScan,
  scanConnectMcp,
  scanConnectSkill,
  testConnectMcp,
  type ConnectMcpItem,
  type ConnectMcpKind,
  type ConnectPlatformItem,
  type ConnectSkillItem,
  type ConnectStatus,
  type McpTestResult,
  type ScanReport,
} from '../api'
import { ConfirmButton, Empty, Loading, Page } from '../components/common'
import { useT, type DictKey, type TFunction } from '../i18n'
import { attempt, fmtTime, notifyError, notifyOk, useLoad } from '../util'

type T = TFunction

const STATUS_COLOR: Record<ConnectStatus, string> = {
  enabled: 'teal',
  disabled: 'gray',
  pending_scan: 'yellow',
  pending_approval: 'blue',
}

const STATUS_KEYS: Record<ConnectStatus, DictKey> = {
  enabled: 'connect.status.enabled',
  disabled: 'connect.status.disabled',
  pending_scan: 'connect.status.pending-scan',
  pending_approval: 'connect.status.pending-approval',
}

const VERDICT_KEYS: Record<ScanReport['verdict'], DictKey> = {
  pass: 'connect.verdict.pass',
  fail: 'connect.verdict.fail',
  wait: 'connect.verdict.wait',
}

const KIND_KEYS: Record<ConnectMcpKind, DictKey> = {
  builtin: 'connect.kind.builtin',
  source: 'connect.kind.source',
  package: 'connect.kind.package',
  remote: 'connect.kind.remote',
}

function verdictColor(v: ScanReport['verdict']): string {
  return v === 'pass' ? 'teal' : v === 'fail' ? 'red' : 'yellow'
}

type Item = ConnectMcpItem | ConnectSkillItem

function canEnable(item: Item): { ok: boolean; reason?: DictKey } {
  if (item.lock_mismatch) return { ok: false, reason: 'connect.enable.blocked-lock' }
  if (item.last_scan?.verdict !== 'pass') return { ok: false, reason: 'connect.enable.blocked-scan' }
  return { ok: true }
}

export default function Connect() {
  const t = useT()
  const inv = useLoad(() => getConnectInventory(), [])
  const [addingMcp, setAddingMcp] = useState(false)
  const [addingSkill, setAddingSkill] = useState(false)
  const [report, setReport] = useState<{ name: string; report: ScanReport | null } | null>(null)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [tests, setTests] = useState<Record<string, McpTestResult>>({})

  if (!inv.data) return <Loading error={inv.error} />
  const { mcp, skills, platforms } = inv.data
  const reload = () => inv.reload()
  const setBusyKey = (key: string, value: boolean) => setBusy((s) => ({ ...s, [key]: value }))
  const isBusy = (key: string) => !!busy[key]

  const runScan = async (kind: 'mcp' | 'skill', item: Item) => {
    const key = `scan-${item.id}`
    setBusyKey(key, true)
    try {
      const rep = kind === 'mcp' ? await scanConnectMcp(item.id) : await scanConnectSkill(item.id)
      notifyOk(t('connect.scanned.ok'))
      setReport({ name: item.name, report: rep })
      reload()
    } catch (e) {
      notifyError(e)
    } finally {
      setBusyKey(key, false)
    }
  }

  const openReport = async (kind: 'mcp' | 'skill', item: Item) => {
    if (item.last_scan) return setReport({ name: item.name, report: item.last_scan })
    const key = `view-${item.id}`
    setBusyKey(key, true)
    try {
      const rep = kind === 'mcp' ? await getConnectMcpScan(item.id) : await getConnectSkillScan(item.id)
      setReport({ name: item.name, report: rep })
    } catch (e) {
      notifyError(e)
    } finally {
      setBusyKey(key, false)
    }
  }

  const runTest = async (item: ConnectMcpItem) => {
    const key = `test-${item.id}`
    setBusyKey(key, true)
    try {
      const res = await testConnectMcp(item.id)
      setTests((s) => ({ ...s, [item.id]: res }))
    } catch (e) {
      notifyError(e)
    } finally {
      setBusyKey(key, false)
    }
  }

  const toggleEnable = async (kind: 'mcp' | 'skill', item: Item) => {
    const key = `enable-${item.id}`
    setBusyKey(key, true)
    try {
      if (item.enabled) {
        const res = await attempt<ConnectMcpItem | ConnectSkillItem>(
          () => (kind === 'mcp' ? disableConnectMcp(item.id) : disableConnectSkill(item.id)),
          t('connect.disabled.ok'),
        )
        if (res) reload()
      } else {
        const res = await attempt<ConnectMcpItem | ConnectSkillItem>(
          () => (kind === 'mcp' ? enableConnectMcp(item.id) : enableConnectSkill(item.id)),
          t('connect.enabled.ok'),
        )
        if (res) reload()
      }
    } finally {
      setBusyKey(key, false)
    }
  }

  const remove = async (kind: 'mcp' | 'skill', item: Item) => {
    const res = await attempt(() => (kind === 'mcp' ? deleteConnectMcp(item.id) : deleteConnectSkill(item.id)), t('connect.removed.ok'))
    if (res) reload()
  }

  return (
    <Page
      title={t('connect.title')}
      actions={
        <Group gap="xs">
          <Button variant="default" size="xs" onClick={() => setAddingSkill(true)} leftSection={<IconPlus size={14} />}>
            {t('connect.add-skill')}
          </Button>
          <Button size="xs" onClick={() => setAddingMcp(true)} leftSection={<IconPlus size={14} />}>
            {t('connect.add-mcp')}
          </Button>
          <Button variant="default" size="xs" onClick={reload}>
            {t('connect.reload')}
          </Button>
        </Group>
      }
    >
      <Text size="sm" c="dimmed">
        {t('connect.subtitle')}
      </Text>
      <Alert color="blue" icon={<IconShieldCheck size={18} />}>
        {t('connect.lead-only')}
      </Alert>

      <Title order={3}>{t('connect.inventory.mcp')}</Title>
      {mcp.length ? (
        <SimpleGrid cols={{ base: 1, md: 2 }}>
          {mcp.map((item) => (
            <McpCard
              key={item.id}
              t={t}
              item={item}
              test={tests[item.id]}
              isBusy={isBusy}
              onScan={() => runScan('mcp', item)}
              onView={() => openReport('mcp', item)}
              onTest={() => runTest(item)}
              onToggle={() => toggleEnable('mcp', item)}
              onRemove={() => remove('mcp', item)}
            />
          ))}
        </SimpleGrid>
      ) : (
        <Empty>{t('connect.inventory.empty-mcp')}</Empty>
      )}

      <Title order={3}>{t('connect.inventory.skills')}</Title>
      {skills.length ? (
        <SimpleGrid cols={{ base: 1, md: 2 }}>
          {skills.map((item) => (
            <SkillCard
              key={item.id}
              t={t}
              item={item}
              isBusy={isBusy}
              onScan={() => runScan('skill', item)}
              onView={() => openReport('skill', item)}
              onToggle={() => toggleEnable('skill', item)}
              onRemove={() => remove('skill', item)}
            />
          ))}
        </SimpleGrid>
      ) : (
        <Empty>{t('connect.inventory.empty-skills')}</Empty>
      )}

      <Title order={3}>{t('connect.inventory.platforms')}</Title>
      {platforms.length ? (
        <SimpleGrid cols={{ base: 1, md: 2 }}>
          {platforms.map((p) => (
            <PlatformCard key={p.id} t={t} item={p} />
          ))}
        </SimpleGrid>
      ) : (
        <Empty>{t('connect.inventory.empty-platforms')}</Empty>
      )}
      <Alert color="yellow" title={t('connect.platform-checklist.title')}>
        <List size="sm" spacing={2}>
          <List.Item>{t('connect.platform-checklist.scope')}</List.Item>
          <List.Item>{t('connect.platform-checklist.secret')}</List.Item>
          <List.Item>{t('connect.platform-checklist.official')}</List.Item>
        </List>
      </Alert>

      <Title order={3}>{t('connect.package-limits.title')}</Title>
      <Alert color="orange" title={t('connect.package-warning')}>
        <List size="sm" spacing={2}>
          <List.Item>{t('connect.package-limits.limit-1')}</List.Item>
          <List.Item>{t('connect.package-limits.limit-2')}</List.Item>
          <List.Item>{t('connect.package-limits.limit-3')}</List.Item>
          <List.Item>{t('connect.package-limits.limit-4')}</List.Item>
          <List.Item>{t('connect.package-limits.limit-5')}</List.Item>
        </List>
      </Alert>

      {addingMcp ? <AddMcpModal t={t} onClose={() => setAddingMcp(false)} onAdded={(rep, name) => { setAddingMcp(false); setReport({ name, report: rep }); reload() }} /> : null}
      {addingSkill ? <AddSkillModal t={t} onClose={() => setAddingSkill(false)} onAdded={(rep, name) => { setAddingSkill(false); setReport({ name, report: rep }); reload() }} /> : null}
      {report ? <ScanReportModal t={t} name={report.name} report={report.report} onClose={() => setReport(null)} /> : null}
    </Page>
  )
}

function StatusBadge({ t, status }: { t: T; status: ConnectStatus }) {
  return (
    <Badge color={STATUS_COLOR[status]} variant="light">
      {t(STATUS_KEYS[status])}
    </Badge>
  )
}

function ScanLine({ t, item }: { t: T; item: Item }) {
  const scan = item.last_scan
  if (!scan)
    return (
      <Text size="sm" c="dimmed">
        {t('connect.scan.none')}
      </Text>
    )
  return (
    <Group gap="xs">
      <Badge color={verdictColor(scan.verdict)} variant="light">
        {t(VERDICT_KEYS[scan.verdict])}
      </Badge>
      <Text size="sm" c="dimmed">
        {t('connect.scan.last')}: {fmtTime(scan.scanned_at)} · {scan.scanner} {scan.scanner_version}
      </Text>
    </Group>
  )
}

type CardActions = {
  onScan: () => void
  onView: () => void
  onToggle: () => void
  onRemove: () => void
  isBusy: (key: string) => boolean
}

function ActionRow({ t, item, onScan, onView, onToggle, onRemove, isBusy, onTest }: CardActions & { t: T; item: Item; onTest?: () => void }) {
  const gate = canEnable(item)
  const enableDisabled = !item.enabled && !gate.ok
  return (
    <Stack gap="xs" mt="sm">
      {enableDisabled && gate.reason ? (
        <Text size="xs" c="orange">
          {gate.reason ? t(gate.reason) : null}
        </Text>
      ) : null}
      {item.lock_mismatch ? (
        <Text size="xs" c="red">
          {t('connect.lock.mismatch')}
        </Text>
      ) : null}
      <Group gap="xs">
        <Button size="xs" variant="light" loading={isBusy(`scan-${item.id}`)} onClick={onScan}>
          {t('connect.scan.run')}
        </Button>
        <Button size="xs" variant="default" loading={isBusy(`view-${item.id}`)} onClick={onView}>
          {t('connect.scan.view')}
        </Button>
        {onTest ? (
          <Button size="xs" variant="default" loading={isBusy(`test-${item.id}`)} onClick={onTest}>
            {t('connect.test')}
          </Button>
        ) : null}
        <Button size="xs" variant="light" loading={isBusy(`enable-${item.id}`)} disabled={enableDisabled} onClick={onToggle}>
          {item.enabled ? t('connect.disable') : t('connect.enable')}
        </Button>
        <ConfirmButton
          size="xs"
          variant="subtle"
          color="red"
          label={t('connect.remove')}
          title={t('connect.remove.title')}
          message={t('connect.remove.message')}
          confirmLabel={t('connect.remove.confirm')}
          onConfirm={onRemove}
        />
      </Group>
    </Stack>
  )
}

function McpCard({ t, item, test, isBusy, onScan, onView, onTest, onToggle, onRemove }: CardActions & { t: T; item: ConnectMcpItem; test?: McpTestResult; onTest: () => void }) {
  const builtin = item.kind === 'builtin'
  const rows: [string, string][] = []
  if (item.source) rows.push([t('connect.source'), item.source])
  if (item.command) rows.push([t('connect.command'), [item.command, ...(item.args ?? [])].join(' ')])
  if (item.package_name) rows.push([t('connect.add-mcp.package'), `${item.package_name}@${item.version ?? '?'}`])
  if (item.url) rows.push([t('connect.url'), item.url])
  return (
    <Card withBorder>
      <Group justify="space-between">
        <div>
          <Text fw={600}>{item.name}</Text>
          <Text size="xs" c="dimmed">
            {t(KIND_KEYS[item.kind])} · {item.id}
          </Text>
        </div>
        <StatusBadge t={t} status={item.status} />
      </Group>
      <Stack gap={2} mt="xs">
        {rows.map(([k, v]) => (
          <Text key={k} size="sm">
            <Text span c="dimmed">
              {k}:
            </Text>{' '}
            <Code>{v}</Code>
          </Text>
        ))}
        <Text size="sm">
          <Text span c="dimmed">
            {t('connect.tools')}:
          </Text>{' '}
          {item.tools?.length ? item.tools.join(', ') : t('connect.tools.none')}
        </Text>
        {item.lock ? (
          <Text size="sm">
            <Text span c="dimmed">
              {t('connect.lock')}:
            </Text>{' '}
            {item.lock.version}
            {item.lock_mismatch ? '' : ` · ${t('connect.lock.ok')}`}
          </Text>
        ) : null}
        <ScanLine t={t} item={item} />
      </Stack>
      {test ? (
        <Alert mt="xs" color={test.ok ? 'teal' : 'red'} title={test.ok ? t('connect.test.ok') : t('connect.test.fail')}>
          <Text size="sm">{test.detail}</Text>
          {test.tools?.length ? (
            <Text size="xs" mt={4}>
              {t('connect.tools')}: {test.tools.join(', ')}
            </Text>
          ) : null}
        </Alert>
      ) : null}
      {builtin ? null : <ActionRow t={t} item={item} onScan={onScan} onView={onView} onTest={onTest} onToggle={onToggle} onRemove={onRemove} isBusy={isBusy} />}
    </Card>
  )
}

function SkillCard({ t, item, isBusy, onScan, onView, onToggle, onRemove }: CardActions & { t: T; item: ConnectSkillItem }) {
  return (
    <Card withBorder>
      <Group justify="space-between">
        <div>
          <Text fw={600}>{item.name}</Text>
          <Text size="xs" c="dimmed">
            {t('connect.kind.skill')} · {item.id}
          </Text>
        </div>
        <StatusBadge t={t} status={item.status} />
      </Group>
      <Stack gap={2} mt="xs">
        {item.path ? (
          <Text size="sm">
            <Text span c="dimmed">
              {t('connect.add-skill.path')}:
            </Text>{' '}
            <Code>{item.path}</Code>
          </Text>
        ) : null}
        <ScanLine t={t} item={item} />
      </Stack>
      <ActionRow t={t} item={item} onScan={onScan} onView={onView} onToggle={onToggle} onRemove={onRemove} isBusy={isBusy} />
    </Card>
  )
}

function PlatformCard({ t, item }: { t: T; item: ConnectPlatformItem }) {
  return (
    <Card withBorder>
      <Group justify="space-between">
        <div>
          <Text fw={600}>{item.name}</Text>
          <Text size="xs" c="dimmed">
            {t('connect.kind.platform')}
            {item.kind ? ` · ${item.kind}` : ''}
          </Text>
        </div>
        <StatusBadge t={t} status={item.status} />
      </Group>
      {item.detail ? (
        <Text size="sm" mt="xs" c="dimmed">
          {item.detail}
        </Text>
      ) : null}
    </Card>
  )
}

function ScanReportModal({ t, name, report, onClose }: { t: T; name: string; report: ScanReport | null; onClose: () => void }) {
  return (
    <Modal opened onClose={onClose} title={`${t('connect.scan.view')}: ${name}`} size="lg">
      {!report ? (
        <Empty>{t('connect.scan.none')}</Empty>
      ) : (
        <Stack>
          <Group gap="xs">
            <Badge color={verdictColor(report.verdict)} variant="light">
              {t(VERDICT_KEYS[report.verdict])}
            </Badge>
            <Text size="sm" c="dimmed">
              {t('connect.scan.scanner')}: {report.scanner} {report.scanner_version}
            </Text>
            <Text size="sm" c="dimmed">
              {t('connect.scan.scanned-at')}: {fmtTime(report.scanned_at)}
            </Text>
          </Group>
          {report.verdict === 'wait' ? (
            <Alert color="yellow" title={t('connect.wait.reason')}>
              {report.wait_reason ?? t('connect.wait.quarantine')}
            </Alert>
          ) : null}
          <Title order={4}>{t('connect.scan.findings')}</Title>
          {report.findings.length ? (
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t('connect.scan.rule')}</Table.Th>
                  <Table.Th>{t('connect.scan.severity')}</Table.Th>
                  <Table.Th>{t('connect.scan.message')}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {report.findings.map((f, i) => (
                  <Table.Tr key={i}>
                    <Table.Td>
                      <Code>{f.rule}</Code>
                    </Table.Td>
                    <Table.Td>
                      <Badge color={f.severity === 'critical' || f.severity === 'high' ? 'red' : f.severity === 'medium' ? 'orange' : 'gray'} variant="light">
                        {f.severity}
                      </Badge>
                    </Table.Td>
                    <Table.Td>{f.message}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          ) : (
            <Empty>{t('connect.scan.findings.none')}</Empty>
          )}
          <Group justify="flex-end">
            <Button onClick={onClose}>{t('connect.cancel')}</Button>
          </Group>
        </Stack>
      )}
    </Modal>
  )
}

function AddMcpModal({ t, onClose, onAdded }: { t: T; onClose: () => void; onAdded: (rep: ScanReport, name: string) => void }) {
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'source' | 'package' | 'remote'>('source')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [packageName, setPackageName] = useState('')
  const [version, setVersion] = useState('')
  const [url, setUrl] = useState('')
  const [auth, setAuth] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!name.trim()) return notifyError(new Error(t('connect.add-mcp.name')))
    if (kind === 'source' && !command.trim()) return notifyError(new Error(t('connect.add-mcp.command')))
    if (kind === 'package' && (!packageName.trim() || !version.trim())) return notifyError(new Error(`${t('connect.add-mcp.package')} / ${t('connect.add-mcp.version')}`))
    if (kind === 'remote' && !url.trim()) return notifyError(new Error(t('connect.add-mcp.url')))
    setBusy(true)
    try {
      const created = await createConnectMcp({
        name: name.trim(),
        kind,
        command: kind === 'source' ? command.trim() : undefined,
        args: kind === 'source' && args.trim() ? args.split('\n').map((a) => a.trim()).filter(Boolean) : undefined,
        package_name: kind === 'package' ? packageName.trim() : undefined,
        version: kind === 'package' ? version.trim() : undefined,
        url: kind === 'remote' ? url.trim() : undefined,
        auth_header: kind === 'remote' && auth.trim() ? auth.trim() : undefined,
      })
      const rep = await scanConnectMcp(created.id)
      onAdded(rep, created.name)
    } catch (e) {
      notifyError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal opened onClose={onClose} title={t('connect.add-mcp.title')} size="lg">
      <Stack>
        <TextInput label={t('connect.add-mcp.name')} placeholder={t('connect.add-mcp.name.placeholder')} value={name} onChange={(e) => setName(e.currentTarget.value)} required />
        <Select
          label={t('connect.add-mcp.kind')}
          data={[
            { value: 'source', label: t('connect.kind.source') },
            { value: 'package', label: t('connect.kind.package') },
            { value: 'remote', label: t('connect.kind.remote') },
          ]}
          value={kind}
          onChange={(v) => setKind((v as 'source' | 'package' | 'remote') ?? 'source')}
          allowDeselect={false}
        />
        {kind === 'source' ? (
          <>
            <TextInput label={t('connect.add-mcp.command')} placeholder={t('connect.add-mcp.command.placeholder')} value={command} onChange={(e) => setCommand(e.currentTarget.value)} required />
            <Textarea label={t('connect.add-mcp.args')} value={args} onChange={(e) => setArgs(e.currentTarget.value)} autosize minRows={2} />
          </>
        ) : null}
        {kind === 'package' ? (
          <>
            <TextInput label={t('connect.add-mcp.package')} placeholder={t('connect.add-mcp.package.placeholder')} value={packageName} onChange={(e) => setPackageName(e.currentTarget.value)} required />
            <TextInput label={t('connect.add-mcp.version')} placeholder={t('connect.add-mcp.version.placeholder')} value={version} onChange={(e) => setVersion(e.currentTarget.value)} required />
            <Alert color="orange" title={t('connect.package-warning')}>
              <List size="sm" spacing={2}>
                <List.Item>{t('connect.package-limits.limit-1')}</List.Item>
                <List.Item>{t('connect.package-limits.limit-2')}</List.Item>
                <List.Item>{t('connect.package-limits.limit-3')}</List.Item>
                <List.Item>{t('connect.package-limits.limit-4')}</List.Item>
                <List.Item>{t('connect.package-limits.limit-5')}</List.Item>
              </List>
            </Alert>
          </>
        ) : null}
        {kind === 'remote' ? (
          <>
            <TextInput label={t('connect.add-mcp.url')} placeholder={t('connect.add-mcp.url.placeholder')} value={url} onChange={(e) => setUrl(e.currentTarget.value)} required />
            <PasswordInput label={t('connect.add-mcp.auth')} placeholder={t('connect.add-mcp.auth.placeholder')} value={auth} onChange={(e) => setAuth(e.currentTarget.value)} autoComplete="off" />
          </>
        ) : null}
        <Text size="xs" c="dimmed">
          {t('connect.added-next')}
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            {t('connect.cancel')}
          </Button>
          <Button loading={busy} onClick={submit}>
            {t('connect.add-mcp.submit')}
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}

function AddSkillModal({ t, onClose, onAdded }: { t: T; onClose: () => void; onAdded: (rep: ScanReport, name: string) => void }) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    if (!name.trim()) return notifyError(new Error(t('connect.add-skill.name')))
    setBusy(true)
    try {
      const created = await createConnectSkill({ name: name.trim(), path: path.trim() || undefined })
      const rep = await scanConnectSkill(created.id)
      onAdded(rep, created.name)
    } catch (e) {
      notifyError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal opened onClose={onClose} title={t('connect.add-skill.title')} size="lg">
      <Stack>
        <TextInput label={t('connect.add-skill.name')} value={name} onChange={(e) => setName(e.currentTarget.value)} required />
        <TextInput label={t('connect.add-skill.path')} placeholder={t('connect.add-skill.path.placeholder')} value={path} onChange={(e) => setPath(e.currentTarget.value)} />
        <Text size="xs" c="dimmed">
          {t('connect.added-next')}
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            {t('connect.cancel')}
          </Button>
          <Button loading={busy} onClick={submit}>
            {t('connect.add-skill.submit')}
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}
