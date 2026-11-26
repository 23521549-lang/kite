import { Badge, Button, Card, FileInput, Grid, Group, Select, Stack, Table, Text, TextInput } from '@mantine/core'
import { IconTrash, IconUpload } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import {
  deleteKnowledge,
  envDisplayName,
  getKnowledge,
  getKnowledgeMarkdown,
  listEnvironments,
  listKnowledge,
  reconvertKnowledge,
  uploadKnowledge,
  type EnvironmentRow,
  type KnowledgeDoc,
} from '../api'
import { ConfirmButton, Empty, KeyValue, Loading, Markdown, Page } from '../components/common'
import { go } from '../router'
import { useT, type DictKey } from '../i18n'
import { attempt, fmtTime, notifyError, useLoad } from '../util'

const ALLOWED_EXTS = ['.md', '.txt', '.docx', '.pdf', '.pptx']
const MAX_FILE_BYTES = 20 * 1024 * 1024

const STATUS: Record<KnowledgeDoc['status'], { label: DictKey; color: string }> = {
  uploaded: { label: 'knowledge.status.uploaded', color: 'blue' },
  converting: { label: 'knowledge.status.converting', color: 'yellow' },
  ready: { label: 'knowledge.status.ready', color: 'teal' },
  failed: { label: 'knowledge.status.failed', color: 'red' },
}

function StatusBadge({ status }: { status: KnowledgeDoc['status'] }) {
  const t = useT()
  const s = STATUS[status] ?? { label: status, color: 'gray' }
  return (
    <Badge color={s.color} variant="light" size="sm">
      {t(s.label)}
    </Badge>
  )
}

export default function Knowledge({ parts }: { parts: string[] }) {
  const t = useT()
  const selected = parts[0] ?? null
  const [scopeFilter, setScopeFilter] = useState<string | null>(null)
  const [tagFilter, setTagFilter] = useState('')
  const { data, error, reload } = useLoad(
    () => listKnowledge({ scope: scopeFilter ?? undefined, tag: tagFilter.trim() || undefined }),
    [scopeFilter, tagFilter],
  )
  const docs = data?.documents ?? []
  const doc = docs.find((d) => d.doc_id === selected) ?? null
  return (
    <Page title={t('knowledge.title')}>
      <Grid>
        <Grid.Col span={{ base: 12, md: 5 }}>
          <Stack>
            <UploadCard onUploaded={reload} />
            <Card withBorder>
              <Group grow align="flex-end" mb="xs">
                <Select
                  label={t('knowledge.filter.scope-label')}
                  data={[
                    { value: 'global', label: t('knowledge.scope.global') },
                    { value: 'environment', label: t('knowledge.scope.environment') },
                  ]}
                  value={scopeFilter}
                  onChange={setScopeFilter}
                  placeholder={t('knowledge.filter.scope-all')}
                  clearable
                />
                <TextInput
                  label={t('knowledge.filter.tag-label')}
                  placeholder={t('knowledge.filter.tag-placeholder')}
                  value={tagFilter}
                  onChange={(e) => setTagFilter(e.currentTarget.value)}
                />
              </Group>
              {!data ? (
                <Loading error={error} />
              ) : docs.length ? (
                <Table highlightOnHover>
                  <Table.Tbody>
                    {docs.map((d) => (
                      <Table.Tr
                        key={d.doc_id}
                        className="clickable"
                        onClick={() => go(`/knowledge/${d.doc_id}`)}
                        style={d.doc_id === selected ? { background: 'var(--mantine-color-default-hover)' } : undefined}
                      >
                        <Table.Td>
                          <Text fw={600} size="sm">
                            {d.filename}
                          </Text>
                          <Text size="xs" c="dimmed">
                            {d.scope === 'global' ? t('knowledge.scope.global') : `${t('knowledge.scope.environment')}: ${d.environment_id ?? ''}`}
                            {d.tags.length ? ` · ${d.tags.join(', ')}` : ''}
                          </Text>
                        </Table.Td>
                        <Table.Td w={120}>
                          <StatusBadge status={d.status} />
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              ) : (
                <Empty>{t('knowledge.list.empty')}</Empty>
              )}
            </Card>
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 7 }}>
          {doc ? <KnowledgeDetail key={doc.doc_id} doc={doc} onChanged={reload} /> : <Empty>{t('knowledge.detail.pick-doc')}</Empty>}
        </Grid.Col>
      </Grid>
    </Page>
  )
}

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const t = useT()
  const [file, setFile] = useState<File | null>(null)
  const [scope, setScope] = useState<string>('global')
  const [environmentId, setEnvironmentId] = useState('')
  const [tags, setTags] = useState('')
  const [uploading, setUploading] = useState(false)
  // Environment list for the scope picker (Gói 2 contract). A failed load
  // degrades to an empty list — the picker then has nothing to choose and
  // the validation below still blocks an environment-scoped upload.
  const { data: envs } = useLoad(() => listEnvironments().catch(() => [] as EnvironmentRow[]), [])

  const upload = async () => {
    if (!file) {
      notifyError(t('knowledge.upload.err-no-file'))
      return
    }
    const name = file.name.toLowerCase()
    if (!ALLOWED_EXTS.some((ext) => name.endsWith(ext))) {
      notifyError(t('knowledge.upload.err-format'))
      return
    }
    if (file.size > MAX_FILE_BYTES) {
      notifyError(t('knowledge.upload.err-too-big'))
      return
    }
    if (scope === 'environment' && !environmentId.trim()) {
      notifyError(t('knowledge.upload.err-env-id'))
      return
    }
    const form = new FormData()
    form.append('file', file)
    form.append('scope', scope)
    if (scope === 'environment') form.append('environment_id', environmentId.trim())
    if (tags.trim()) form.append('tags', tags.trim())
    setUploading(true)
    try {
      const res = await attempt(() => uploadKnowledge(form), t('knowledge.upload.uploaded-ok'))
      if (res) {
        setFile(null)
        setTags('')
        setEnvironmentId('')
        onUploaded()
        go(`/knowledge/${res.doc_id}`)
      }
    } finally {
      setUploading(false)
    }
  }

  return (
    <Card withBorder>
      <Text fw={600} mb="xs">
        {t('knowledge.upload.title')}
      </Text>
      <Stack gap="xs">
        <FileInput
          label={t('knowledge.upload.file-label')}
          placeholder={t('knowledge.upload.file-placeholder')}
          accept={ALLOWED_EXTS.join(',')}
          value={file}
          onChange={setFile}
          clearable
        />
        <Select
          label={t('knowledge.upload.scope-label')}
          data={[
            { value: 'global', label: t('knowledge.scope.global') },
            { value: 'environment', label: t('knowledge.scope.environment') },
          ]}
          value={scope}
          onChange={(v) => setScope(v ?? 'global')}
        />
        {scope === 'environment' ? (
          <Select
            label={t('knowledge.upload.env-id-label')}
            placeholder={t('knowledge.upload.env-select-placeholder')}
            data={(envs ?? []).map((e) => ({ value: e.id, label: envDisplayName(e) }))}
            value={environmentId || null}
            onChange={(v) => setEnvironmentId(v ?? '')}
            searchable
          />
        ) : null}
        <TextInput label={t('knowledge.upload.tags-label')} placeholder={t('knowledge.upload.tags-placeholder')} value={tags} onChange={(e) => setTags(e.currentTarget.value)} />
        <Group>
          <Button leftSection={<IconUpload size={16} />} disabled={!file || uploading} loading={uploading} onClick={upload}>
            {t('knowledge.upload.button')}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

function KnowledgeDetail({ doc, onChanged }: { doc: KnowledgeDoc; onChanged: () => void }) {
  const t = useT()
  const [converting, setConverting] = useState(doc.status === 'converting')
  // Poll the detail every 3 seconds while converting, until it finishes.
  const loaded = useLoad(() => getKnowledge(doc.doc_id), [doc.doc_id, converting], converting ? 3000 : undefined)
  const shown = loaded.data ?? doc
  useEffect(() => {
    if (loaded.data) setConverting(loaded.data.status === 'converting')
  }, [loaded.data])
  const md = useLoad(async () => (shown.status === 'ready' ? getKnowledgeMarkdown(doc.doc_id) : ''), [doc.doc_id, shown.status])

  return (
    <Card withBorder>
      <Stack>
        <Group justify="space-between" align="flex-start" wrap="wrap">
          <Text fw={600}>{shown.filename}</Text>
          <StatusBadge status={shown.status} />
        </Group>
        <KeyValue
          rows={[
            [t('knowledge.detail.scope'), shown.scope === 'global' ? t('knowledge.scope.global') : `${t('knowledge.scope.environment')}: ${shown.environment_id ?? ''}`],
            [t('knowledge.detail.tags'), shown.tags.length ? shown.tags.join(', ') : t('knowledge.detail.unknown')],
            [t('knowledge.detail.converter'), shown.converter ?? t('knowledge.detail.unknown')],
            [t('knowledge.detail.converted-at'), fmtTime(shown.converted_at)],
          ]}
        />
        {shown.status === 'failed' ? (
          <Text size="sm" c="red">
            {t('knowledge.detail.failed-note')}
            {shown.error ? ` (${shown.error})` : ''}
          </Text>
        ) : null}
        {shown.status === 'uploaded' ? <Text size="sm" c="dimmed">{t('knowledge.detail.uploaded-note')}</Text> : null}
        {shown.status === 'converting' ? <Loading label={t('knowledge.detail.converting-note')} /> : null}
        {shown.status === 'ready' ? (
          !md.data ? (
            <Loading error={md.error} />
          ) : md.data ? (
            <Markdown text={md.data} />
          ) : (
            <Empty>{t('knowledge.detail.no-markdown')}</Empty>
          )
        ) : null}
        <Group>
          <Button
            variant="light"
            disabled={converting}
            onClick={async () => {
              const res = await attempt(() => reconvertKnowledge(doc.doc_id), t('knowledge.detail.reconverted-ok'))
              if (res) {
                setConverting(true)
                loaded.reload()
              }
            }}
          >
            {converting ? t('knowledge.detail.reconverting') : t('knowledge.detail.reconvert')}
          </Button>
          <ConfirmButton
            size="sm"
            variant="subtle"
            color="red"
            label={<IconTrash size={14} />}
            title={t('knowledge.detail.delete-title')}
            message={t('knowledge.detail.delete-message')}
            confirmLabel={t('knowledge.detail.delete-confirm')}
            onConfirm={async () => {
              await attempt(() => deleteKnowledge(doc.doc_id), t('knowledge.detail.deleted-ok'))
              onChanged()
              go('/knowledge')
            }}
          />
        </Group>
      </Stack>
    </Card>
  )
}
