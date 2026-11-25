import { Badge, Button, Card, FileInput, Grid, Group, Select, Stack, Table, Text, TextInput } from '@mantine/core'
import { IconTrash, IconUpload } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import {
  deleteKnowledge,
  getKnowledge,
  getKnowledgeMarkdown,
  listKnowledge,
  reconvertKnowledge,
  uploadKnowledge,
  type KnowledgeDoc,
} from '../api'
import { ConfirmButton, Empty, KeyValue, Loading, Markdown, Page } from '../components/common'
import { go } from '../router'
import { attempt, fmtTime, notifyError, useLoad } from '../util'

const STR = {
  title: 'Kiến thức',
  uploadTitle: 'Thêm tài liệu',
  fileLabel: 'File',
  filePlaceholder: 'Chọn file .md, .txt, .docx, .pdf hoặc .pptx',
  scopeLabel: 'Phạm vi',
  scopeGlobal: 'Toàn cục',
  scopeEnvironment: 'Gắn môi trường',
  envIdLabel: 'ID môi trường',
  envIdPlaceholder: 'vd. kite',
  envIdHint: 'Chưa có danh sách môi trường — nhập ID môi trường (Gói 2 sẽ thay bằng ô chọn).',
  tagsLabel: 'Thẻ',
  tagsPlaceholder: 'vd. thanh-toan, bao-cao (phân tách bằng dấu phẩy)',
  uploadButton: 'Tải lên',
  uploadedOk: 'Đã tải tài liệu lên',
  errNoFile: 'Chưa chọn file.',
  errTooBig: 'File quá 20 MB — Kite chỉ nhận file tối đa 20 MB.',
  errFormat: 'Kite chưa nhận định dạng này — chỉ nhận .md, .txt, .docx, .pdf, .pptx.',
  errEnvId: 'Gắn môi trường thì phải nhập ID môi trường.',
  listTitle: 'Tài liệu',
  filterScopeLabel: 'Phạm vi',
  filterScopeAll: 'Tất cả',
  filterTagLabel: 'Lọc theo thẻ',
  filterTagPlaceholder: 'vd. thanh-toan',
  emptyList: 'Chưa có tài liệu nào.',
  pickDoc: 'Chọn một tài liệu để xem.',
  statusUploaded: 'Đã tải lên',
  statusConverting: 'Đang chuyển',
  statusReady: 'Sẵn sàng',
  statusFailed: 'Không đọc được',
  colFile: 'File',
  colScope: 'Phạm vi',
  colTags: 'Thẻ',
  colStatus: 'Trạng thái',
  colConverter: 'Công cụ chuyển',
  converterUnknown: '—',
  detailConverter: 'Công cụ chuyển',
  detailConvertedAt: 'Chuyển xong lúc',
  detailError: 'Lỗi chuyển đổi',
  detailUploadedNote: 'Tài liệu vừa tải lên, đang chờ chuyển sang Markdown.',
  detailConvertingNote: 'Đang chuyển bằng Docling…',
  detailFailedNote: 'Kite không đọc được tài liệu này (cả MarkItDown lẫn Docling đều không chuyển được).',
  detailNoMarkdown: 'Chưa có nội dung Markdown.',
  reconvertButton: 'Chuyển lại bằng Docling',
  reconvertingButton: 'Đang chuyển bằng Docling…',
  reconvertedOk: 'Đã bắt đầu chuyển lại bằng Docling',
  deleteButton: 'Xóa',
  deleteTitle: 'Xóa tài liệu này?',
  deleteMessage: 'Xóa cả file gốc, bản Markdown và lịch sử chuyển đổi. Không khôi phục được.',
  deleteConfirm: 'Xóa',
  deletedOk: 'Đã xóa tài liệu',
} as const

const ALLOWED_EXTS = ['.md', '.txt', '.docx', '.pdf', '.pptx']
const MAX_FILE_BYTES = 20 * 1024 * 1024

const STATUS: Record<KnowledgeDoc['status'], { label: string; color: string }> = {
  uploaded: { label: STR.statusUploaded, color: 'blue' },
  converting: { label: STR.statusConverting, color: 'yellow' },
  ready: { label: STR.statusReady, color: 'teal' },
  failed: { label: STR.statusFailed, color: 'red' },
}

function StatusBadge({ status }: { status: KnowledgeDoc['status'] }) {
  const s = STATUS[status] ?? { label: status, color: 'gray' }
  return (
    <Badge color={s.color} variant="light" size="sm">
      {s.label}
    </Badge>
  )
}

export default function Knowledge({ parts }: { parts: string[] }) {
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
    <Page title={STR.title}>
      <Grid>
        <Grid.Col span={{ base: 12, md: 5 }}>
          <Stack>
            <UploadCard onUploaded={reload} />
            <Card withBorder>
              <Group grow align="flex-end" mb="xs">
                <Select
                  label={STR.filterScopeLabel}
                  data={[
                    { value: 'global', label: STR.scopeGlobal },
                    { value: 'environment', label: STR.scopeEnvironment },
                  ]}
                  value={scopeFilter}
                  onChange={setScopeFilter}
                  placeholder={STR.filterScopeAll}
                  clearable
                />
                <TextInput
                  label={STR.filterTagLabel}
                  placeholder={STR.filterTagPlaceholder}
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
                            {d.scope === 'global' ? STR.scopeGlobal : `${STR.scopeEnvironment}: ${d.environment_id ?? ''}`}
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
                <Empty>{STR.emptyList}</Empty>
              )}
            </Card>
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 7 }}>
          {doc ? <KnowledgeDetail key={doc.doc_id} doc={doc} onChanged={reload} /> : <Empty>{STR.pickDoc}</Empty>}
        </Grid.Col>
      </Grid>
    </Page>
  )
}

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [scope, setScope] = useState<string>('global')
  const [environmentId, setEnvironmentId] = useState('')
  const [tags, setTags] = useState('')
  const [uploading, setUploading] = useState(false)

  const upload = async () => {
    if (!file) {
      notifyError(STR.errNoFile)
      return
    }
    const name = file.name.toLowerCase()
    if (!ALLOWED_EXTS.some((ext) => name.endsWith(ext))) {
      notifyError(STR.errFormat)
      return
    }
    if (file.size > MAX_FILE_BYTES) {
      notifyError(STR.errTooBig)
      return
    }
    if (scope === 'environment' && !environmentId.trim()) {
      notifyError(STR.errEnvId)
      return
    }
    const form = new FormData()
    form.append('file', file)
    form.append('scope', scope)
    if (scope === 'environment') form.append('environment_id', environmentId.trim())
    if (tags.trim()) form.append('tags', tags.trim())
    setUploading(true)
    try {
      const res = await attempt(() => uploadKnowledge(form), STR.uploadedOk)
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
        {STR.uploadTitle}
      </Text>
      <Stack gap="xs">
        <FileInput
          label={STR.fileLabel}
          placeholder={STR.filePlaceholder}
          accept={ALLOWED_EXTS.join(',')}
          value={file}
          onChange={setFile}
          clearable
        />
        <Select
          label={STR.scopeLabel}
          data={[
            { value: 'global', label: STR.scopeGlobal },
            { value: 'environment', label: STR.scopeEnvironment },
          ]}
          value={scope}
          onChange={(v) => setScope(v ?? 'global')}
        />
        {scope === 'environment' ? (
          <TextInput
            label={STR.envIdLabel}
            placeholder={STR.envIdPlaceholder}
            description={STR.envIdHint}
            value={environmentId}
            onChange={(e) => setEnvironmentId(e.currentTarget.value)}
          />
        ) : null}
        <TextInput label={STR.tagsLabel} placeholder={STR.tagsPlaceholder} value={tags} onChange={(e) => setTags(e.currentTarget.value)} />
        <Group>
          <Button leftSection={<IconUpload size={16} />} disabled={!file || uploading} loading={uploading} onClick={upload}>
            {STR.uploadButton}
          </Button>
        </Group>
      </Stack>
    </Card>
  )
}

function KnowledgeDetail({ doc, onChanged }: { doc: KnowledgeDoc; onChanged: () => void }) {
  const [converting, setConverting] = useState(doc.status === 'converting')
  // Poll chi tiết mỗi 3 giây trong lúc đang chuyển, tới khi xong.
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
            [STR.colScope, shown.scope === 'global' ? STR.scopeGlobal : `${STR.scopeEnvironment}: ${shown.environment_id ?? ''}`],
            [STR.colTags, shown.tags.length ? shown.tags.join(', ') : STR.converterUnknown],
            [STR.detailConverter, shown.converter ?? STR.converterUnknown],
            [STR.detailConvertedAt, fmtTime(shown.converted_at)],
          ]}
        />
        {shown.status === 'failed' ? (
          <Text size="sm" c="red">
            {STR.detailFailedNote}
            {shown.error ? ` (${shown.error})` : ''}
          </Text>
        ) : null}
        {shown.status === 'uploaded' ? <Text size="sm" c="dimmed">{STR.detailUploadedNote}</Text> : null}
        {shown.status === 'converting' ? <Loading label={STR.detailConvertingNote} /> : null}
        {shown.status === 'ready' ? (
          !md.data ? (
            <Loading error={md.error} />
          ) : md.data ? (
            <Markdown text={md.data} />
          ) : (
            <Empty>{STR.detailNoMarkdown}</Empty>
          )
        ) : null}
        <Group>
          <Button
            variant="light"
            disabled={converting}
            onClick={async () => {
              const res = await attempt(() => reconvertKnowledge(doc.doc_id), STR.reconvertedOk)
              if (res) {
                setConverting(true)
                loaded.reload()
              }
            }}
          >
            {converting ? STR.reconvertingButton : STR.reconvertButton}
          </Button>
          <ConfirmButton
            size="sm"
            variant="subtle"
            color="red"
            label={<IconTrash size={14} />}
            title={STR.deleteTitle}
            message={STR.deleteMessage}
            confirmLabel={STR.deleteConfirm}
            onConfirm={async () => {
              await attempt(() => deleteKnowledge(doc.doc_id), STR.deletedOk)
              onChanged()
              go('/knowledge')
            }}
          />
        </Group>
      </Stack>
    </Card>
  )
}
