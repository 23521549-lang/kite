// Strings for the Environments pages (Gói 2). Pattern follows Gói 9's
// useOverviewStrings: two dicts with the same key set (a missing key on one
// side is a compile error), language read from localStorage key
// `kite-language` (default `vi`). Gói 0(b) will merge this module into the
// global i18n layer later; the `env.*` keys stay unchanged.

import { useSyncExternalStore } from 'react'

export type Language = 'vi' | 'en'

export function getLanguage(): Language {
  try {
    return localStorage.getItem('kite-language') === 'en' ? 'en' : 'vi'
  } catch {
    return 'vi'
  }
}

function subscribeLanguage(onChange: () => void): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key === 'kite-language') onChange()
  }
  window.addEventListener('storage', onStorage)
  window.addEventListener('kite-language-change', onChange)
  return () => {
    window.removeEventListener('storage', onStorage)
    window.removeEventListener('kite-language-change', onChange)
  }
}

export function useLanguage(): Language {
  return useSyncExternalStore(subscribeLanguage, getLanguage, () => 'vi' as Language)
}

const vi = {
  'env.title': 'Môi trường',
  'env.subtitle': 'Mỗi môi trường là một repo Kite làm việc trên đó. Một môi trường có nhiều session chat.',
  'env.empty': 'Chưa có môi trường nào ngoài Default.',
  'env.new': 'Thêm môi trường',
  'env.default-name': 'Mặc định',
  'env.default-desc': 'Không gắn repo nào. Chat chung, giao task nhỏ không cần code repo.',
  'env.kind-default': 'Mặc định',
  'env.kind-github': 'Repo GitHub (Kite clone)',
  'env.kind-local': 'Thư mục có sẵn',
  'env.branch': 'Nhánh',
  'env.head': 'HEAD',
  'env.clean': 'sạch',
  'env.dirty': 'có thay đổi chưa commit',
  'env.clean-unknown': 'chưa rõ trạng thái',
  'env.last-sync': 'Đồng bộ lần cuối',
  'env.never-synced': 'chưa đồng bộ',
  'env.sessions': 'session',
  'env.tasks': 'task',
  'env.lessons': 'bài học',
  'env.spend-month': 'Chi tháng này',
  'env.open-chat': 'Mở chat mới trong môi trường này',
  'env.sync': 'Đồng bộ',
  'env.sync-done': 'Đã đồng bộ',
  'env.view-sessions': 'Xem session',
  'env.view-tasks': 'Xem task',
  'env.view-lessons': 'Xem bài học',
  'env.delete': 'Xóa môi trường',
  'env.delete-title': 'Xóa môi trường này?',
  'env.delete-message': 'Chỉ xóa môi trường khỏi Kite; repo trên ổ đĩa và lịch sử session/task không bị xóa.',
  'env.delete-blocked': 'Không xóa được: còn session hoặc task đang hoạt động trong môi trường này.',
  'env.add-title': 'Thêm môi trường',
  'env.form-name': 'Tên hiển thị',
  'env.form-name-ph': 'vd: Kite',
  'env.form-kind': 'Loại',
  'env.form-url': 'URL repo GitHub',
  'env.form-url-ph': 'https://github.com/owner/repo',
  'env.form-path': 'Đường dẫn thư mục trên máy Kite',
  'env.form-path-ph': '/home/ubuntu/sweetpaw',
  'env.form-path-desc-github': 'Thư mục repo trên máy Kite (repo Git đã có ít nhất một commit).',
  'env.form-path-desc-local': 'Thư mục repo đã có sẵn trên máy Kite.',
  'env.form-pat-after': 'Nhập PAT sau khi tạo, ở trang chi tiết của môi trường.',
  'env.form-branch': 'Nhánh',
  'env.form-branch-ph': 'main',
  'env.form-pat': 'Token GitHub (PAT)',
  'env.form-pat-ph': 'ghp_… hoặc github_pat_…',
  'env.form-pat-help':
    'Mỗi môi trường một PAT riêng, chỉ quyền đọc nội dung repo đó (fine-grained, Contents read-only, có hạn). PAT đã lưu không bao giờ hiển thị lại.',
  'env.form-has-pat': 'Đã lưu PAT cho môi trường này.',
  'env.form-no-pat': 'Chưa có PAT. Repo công khai không cần PAT; repo riêng cần PAT để clone/pull.',
  'env.form-pat-change': 'Thay PAT',
  'env.form-pat-delete': 'Xóa PAT',
  'env.form-pat-saved': 'Đã lưu PAT',
  'env.form-pat-deleted': 'Đã xóa PAT',
  'env.form-add': 'Thêm',
  'env.form-cancel': 'Thôi',
  'env.form-added': 'Đã thêm môi trường',
  'env.detail-overview': 'Tổng quan',
  'env.detail-path': 'Đường dẫn',
  'env.detail-sessions-tasks': 'Session & Task',
  'env.detail-context': 'Ngữ cảnh',
  'env.detail-lessons': 'Bài học',
  'env.detail-git-info': 'Thông tin git',
  'env.detail-recent-commits': 'Commit gần đây',
  'env.detail-recent-sessions': 'Session gần đây',
  'env.detail-recent-tasks': 'Task gần đây',
  'env.detail-no-sessions': 'Chưa có session nào trong môi trường này.',
  'env.detail-no-tasks': 'Chưa có task nào trong môi trường này.',
  'env.detail-experience': 'Tài liệu kinh nghiệm đã duyệt',
  'env.detail-experience-none': 'Chưa có tài liệu kinh nghiệm cho repo này.',
  'env.detail-pending': 'Đề xuất đang chờ duyệt',
  'env.detail-pending-none': 'Không có đề xuất nào đang chờ.',
  'env.detail-approve': 'Duyệt',
  'env.detail-reject': 'Bỏ',
  'env.detail-diff': 'Xem diff',
  'env.detail-lessons-none': 'Chưa có bài học nào.',
  'env.detail-moves': 'Lịch sử di chuyển session',
  'env.detail-open-env-chat': 'Mở chat mới trong môi trường này',
  'env.detail-back': 'Tất cả môi trường',
  'env.origin-session': 'Session',
  'env.origin-environment': 'Môi trường lúc tạo',
  'env.origin-unknown': 'Không rõ nguồn gốc',
  'env.origin-timeline': 'Xem timeline môi trường',
  'env.apply-error-title': 'Patch không áp được lên HEAD mới',
  'env.apply-error-files': 'File lỗi',
  'env.apply-error-rerun': 'Chạy lại task trên HEAD mới',
  'env.apply-error-rerun-done': 'Đã tạo task chạy lại',
  'env.apply-error-dismiss': 'Bỏ patch',
  'env.apply-error-dismissed': 'Đã bỏ patch',
  'env.apply-error-manual': 'Sửa tay',
  'env.apply-error-manual-help':
    'Mở repo trên máy, tự gộp patch vào HEAD mới rồi áp/commit lại. Patch cũ vẫn còn nguyên.',
  'env.apply-error-note': 'Mã xác nhận của patch cũ chưa bị tiêu thụ, nên hai lựa chọn còn lại vẫn dùng được.',
} as const

export type EnvKey = keyof typeof vi

const en: Record<EnvKey, string> = {
  'env.title': 'Environments',
  'env.subtitle': 'Each environment is a repo Kite works on. One environment can have many chat sessions.',
  'env.empty': 'No environments besides Default yet.',
  'env.new': 'Add environment',
  'env.default-name': 'Default',
  'env.default-desc': 'Not attached to any repo. General chat and small tasks that need no repo code.',
  'env.kind-default': 'Default',
  'env.kind-github': 'GitHub repo (cloned by Kite)',
  'env.kind-local': 'Existing folder',
  'env.branch': 'Branch',
  'env.head': 'HEAD',
  'env.clean': 'clean',
  'env.dirty': 'has uncommitted changes',
  'env.clean-unknown': 'status unknown',
  'env.last-sync': 'Last synced',
  'env.never-synced': 'never synced',
  'env.sessions': 'sessions',
  'env.tasks': 'tasks',
  'env.lessons': 'lessons',
  'env.spend-month': 'Spend this month',
  'env.open-chat': 'Open a new chat in this environment',
  'env.sync': 'Sync',
  'env.sync-done': 'Synced',
  'env.view-sessions': 'View sessions',
  'env.view-tasks': 'View tasks',
  'env.view-lessons': 'View lessons',
  'env.delete': 'Delete environment',
  'env.delete-title': 'Delete this environment?',
  'env.delete-message': 'Only removes the environment from Kite; the repo on disk and session/task history are kept.',
  'env.delete-blocked': 'Cannot delete: there are still active sessions or tasks in this environment.',
  'env.add-title': 'Add environment',
  'env.form-name': 'Display name',
  'env.form-name-ph': 'e.g. Kite',
  'env.form-kind': 'Type',
  'env.form-url': 'GitHub repo URL',
  'env.form-url-ph': 'https://github.com/owner/repo',
  'env.form-path': 'Folder path on the Kite machine',
  'env.form-path-ph': '/home/ubuntu/sweetpaw',
  'env.form-path-desc-github': 'Repo folder on the Kite machine (a Git repo with at least one commit).',
  'env.form-path-desc-local': 'An existing repo folder on the Kite machine.',
  'env.form-pat-after': 'Enter the PAT after creation, on the environment detail page.',
  'env.form-branch': 'Branch',
  'env.form-branch-ph': 'main',
  'env.form-pat': 'GitHub token (PAT)',
  'env.form-pat-ph': 'ghp_… or github_pat_…',
  'env.form-pat-help':
    'One PAT per environment, read-only on that repo only (fine-grained, Contents read-only, with an expiry). A saved PAT is never shown again.',
  'env.form-has-pat': 'A PAT is saved for this environment.',
  'env.form-no-pat': 'No PAT yet. Public repos need none; private repos need a PAT to clone/pull.',
  'env.form-pat-change': 'Replace PAT',
  'env.form-pat-delete': 'Delete PAT',
  'env.form-pat-saved': 'PAT saved',
  'env.form-pat-deleted': 'PAT deleted',
  'env.form-add': 'Add',
  'env.form-cancel': 'Cancel',
  'env.form-added': 'Environment added',
  'env.detail-overview': 'Overview',
  'env.detail-path': 'Path',
  'env.detail-sessions-tasks': 'Sessions & Tasks',
  'env.detail-context': 'Context',
  'env.detail-lessons': 'Lessons',
  'env.detail-git-info': 'Git info',
  'env.detail-recent-commits': 'Recent commits',
  'env.detail-recent-sessions': 'Recent sessions',
  'env.detail-recent-tasks': 'Recent tasks',
  'env.detail-no-sessions': 'No sessions in this environment yet.',
  'env.detail-no-tasks': 'No tasks in this environment yet.',
  'env.detail-experience': 'Approved experience document',
  'env.detail-experience-none': 'No experience document for this repo yet.',
  'env.detail-pending': 'Proposal awaiting review',
  'env.detail-pending-none': 'No proposals awaiting review.',
  'env.detail-approve': 'Approve',
  'env.detail-reject': 'Reject',
  'env.detail-diff': 'View diff',
  'env.detail-lessons-none': 'No lessons yet.',
  'env.detail-moves': 'Session move history',
  'env.detail-open-env-chat': 'Open a new chat in this environment',
  'env.detail-back': 'All environments',
  'env.origin-session': 'Session',
  'env.origin-environment': 'Environment at creation',
  'env.origin-unknown': 'Unknown origin',
  'env.origin-timeline': 'View environment timeline',
  'env.apply-error-title': 'Patch does not apply onto the new HEAD',
  'env.apply-error-files': 'Failed files',
  'env.apply-error-rerun': 'Re-run task on the new HEAD',
  'env.apply-error-rerun-done': 'Re-run task created',
  'env.apply-error-dismiss': 'Discard patch',
  'env.apply-error-dismissed': 'Patch discarded',
  'env.apply-error-manual': 'Fix by hand',
  'env.apply-error-manual-help':
    'Open the repo on the machine, merge the patch onto the new HEAD yourself, then apply/commit again. The old patch is still intact.',
  'env.apply-error-note': 'The old patch confirmation code was not consumed, so the other two options still work.',
}

const dicts: Record<Language, Record<EnvKey, string>> = { vi: vi as unknown as Record<EnvKey, string>, en }

/** Temporary hook until Gói 0(b) merges this module into the global i18n layer. */
export function useEnvStrings(): Record<EnvKey, string> {
  return dicts[useLanguage()]
}
