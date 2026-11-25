// Chuỗi UI trang Tổng quan — namespace `overview.*` (theo lớp i18n của Gói 0(b)).
// Hai dict có cùng tập key (kiểm bằng TypeScript: `Record<OverviewKey, string>`).
// Khi Gói 0(b) hợp nhất module này vào lớp chung (`vi.ts`/`en.ts`), hook
// `useOverviewStrings()` được thay bằng `useT()` — key không đổi.
import { useSyncExternalStore } from 'react'

export const overviewVi = {
  'overview.title': 'Tổng quan',
  'overview.stat.gateway': 'Gateway',
  'overview.stat.gateway-on': 'đang chạy',
  'overview.stat.gateway-off': 'tắt',
  'overview.stat.jobs': 'Việc đang chờ / đang chạy',
  'overview.stat.spend-today': 'Chi hôm nay',
  'overview.stat.spend-month': 'Chi tháng này',
  'overview.brand-spend.title': 'Chi theo hãng tháng này',
  'overview.quota.title': 'Quota GPU Kaggle',
  'overview.office.title': 'Văn phòng Kite',
  'overview.office.loading': 'Đang tải văn phòng 3D…',
  'overview.office.no-webgl': 'Trình duyệt này không có WebGL nên chỉ xem được 2D.',
  'overview.running.title': 'Đang làm',
  'overview.running.empty': 'Không có task nào đang chạy.',
  'overview.running.queued': 'đang chờ worker',
  'overview.running.preparing': 'đang chuẩn bị',
  'overview.recent-tasks.title': 'Task gần đây',
  'overview.recent-tasks.empty': 'Chưa có task.',
  'overview.recent-events.title': 'Sự kiện mới',
  'overview.recent-events.empty': 'Sự kiện mới sẽ hiện ở đây khi Kite làm việc.',
  'overview.workers.empty': 'Chưa có worker nào: thêm ở trang Worker.',
  'overview.phase.fast-path': 'đường nhanh',
  'overview.phase.review': 'rà soát',
  'overview.task.step': 'bước {step}',
  'overview.task.action': 'vừa {action}',
  'overview.task.since': 'từ {time}',
  'overview.task.worker': 'worker {worker}',
  'overview.worker.running': '{running}/{slots} việc đang chạy',
  'overview.worker.loading-model': 'đang nạp {model}',
  'overview.office.more-tasks': '+{n} task nữa',
  'overview.office.open-task': '(bấm để mở task)',
  'overview.office.aria': 'Văn phòng Kite 3D: worker, task và agent trưởng',
  'overview.office.queue-tip': 'Hàng chờ: task chưa có worker (bấm để xem tab Task)',
  'overview.office.gateway-tip': 'Gateway: chuyển request tới worker (bấm để xem trang Task)',
  'overview.office.gateway-up': 'đang chạy',
  'overview.office.gateway-down': 'không phản hồi',
  'overview.office.worker-tip': '(bấm để xem tab Worker)',
  'overview.office.running-suffix': ' · {running}/{slots} việc',
  'overview.office.lead-tip': 'Agent trưởng: chat với bạn và giao task (bấm để mở Chat)',
  'overview.office.lead-name': 'Agent trưởng',
  'overview.office.answering': 'đang trả lời',
  'overview.office.waiting': 'chờ bạn nhắn',
  'overview.office.queue-name': 'Hàng chờ',
  'overview.office.queue-waiting': '{n} task chờ worker',
  'overview.office.queue-empty': 'trống',
  'overview.office.hint': 'Kéo để xoay · cuộn để phóng to · bấm vào bàn để mở',
  'overview.office.failed': 'Không vẽ được 3D trên trình duyệt này ({error}). Chọn 2D.',
} as const

export type OverviewKey = keyof typeof overviewVi

export const overviewEn: Record<OverviewKey, string> = {
  'overview.title': 'Overview',
  'overview.stat.gateway': 'Gateway',
  'overview.stat.gateway-on': 'running',
  'overview.stat.gateway-off': 'off',
  'overview.stat.jobs': 'Queued / running jobs',
  'overview.stat.spend-today': 'Spent today',
  'overview.stat.spend-month': 'Spent this month',
  'overview.brand-spend.title': 'Spend by brand this month',
  'overview.quota.title': 'Kaggle GPU quota',
  'overview.office.title': 'Kite office',
  'overview.office.loading': 'Loading the 3D office…',
  'overview.office.no-webgl': 'This browser has no WebGL, so only the 2D view is available.',
  'overview.running.title': 'In progress',
  'overview.running.empty': 'No tasks running.',
  'overview.running.queued': 'waiting for a worker',
  'overview.running.preparing': 'preparing',
  'overview.recent-tasks.title': 'Recent tasks',
  'overview.recent-tasks.empty': 'No tasks yet.',
  'overview.recent-events.title': 'Recent events',
  'overview.recent-events.empty': 'New events will appear here as Kite works.',
  'overview.workers.empty': 'No workers yet: add one on the Workers page.',
  'overview.phase.fast-path': 'fast path',
  'overview.phase.review': 'review',
  'overview.task.step': 'step {step}',
  'overview.task.action': 'just {action}',
  'overview.task.since': 'since {time}',
  'overview.task.worker': 'worker {worker}',
  'overview.worker.running': '{running}/{slots} jobs running',
  'overview.worker.loading-model': 'loading {model}',
  'overview.office.more-tasks': '+{n} more tasks',
  'overview.office.open-task': '(click to open the task)',
  'overview.office.aria': 'Kite 3D office: workers, tasks and the lead agent',
  'overview.office.queue-tip': 'Queue: tasks with no worker yet (click to open Tasks)',
  'overview.office.gateway-tip': 'Gateway: routes requests to workers (click to open Tasks)',
  'overview.office.gateway-up': 'running',
  'overview.office.gateway-down': 'not responding',
  'overview.office.worker-tip': '(click to open Workers)',
  'overview.office.running-suffix': ' · {running}/{slots} jobs',
  'overview.office.lead-tip': 'Lead agent: chats with you and assigns tasks (click to open Chat)',
  'overview.office.lead-name': 'Lead agent',
  'overview.office.answering': 'replying',
  'overview.office.waiting': 'waiting for your message',
  'overview.office.queue-name': 'Queue',
  'overview.office.queue-waiting': '{n} tasks waiting for a worker',
  'overview.office.queue-empty': 'empty',
  'overview.office.hint': 'Drag to rotate · scroll to zoom · click a desk to open',
  'overview.office.failed': 'Could not render 3D in this browser ({error}). Switch to 2D.',
}

export type Language = 'vi' | 'en'

function readLanguage(): Language {
  try {
    return localStorage.getItem('kite-language') === 'en' ? 'en' : 'vi'
  } catch {
    return 'vi'
  }
}

function subscribeLanguage(onChange: () => void): () => void {
  const onStorage = () => onChange()
  window.addEventListener('storage', onStorage)
  window.addEventListener('kite-language-change', onStorage)
  return () => {
    window.removeEventListener('storage', onStorage)
    window.removeEventListener('kite-language-change', onStorage)
  }
}

// Hook TẠM THỜI — tới lúc Gói 0(b) hợp nhất module này vào lớp i18n chung thì
// thay bằng `useT()` của lớp đó. Không xây lớp i18n thứ hai.
export function useOverviewStrings(): Record<OverviewKey, string> {
  const lang = useSyncExternalStore(subscribeLanguage, readLanguage, () => 'vi' as Language)
  return lang === 'en' ? overviewEn : overviewVi
}

// Thay `{name}` trong template bằng giá trị. Key template là chuỗi tĩnh, không key động.
export function fmt(template: string, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce((s, [k, v]) => s.replaceAll(`{${k}}`, String(v)), template)
}
