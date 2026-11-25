// Chuỗi UI trang Giám sát — namespace `monitor.*` (theo lớp i18n của Gói 0(b)).
// Hai dict có cùng tập key (kiểm bằng TypeScript: `Record<MonitorKey, string>`).
// Khi Gói 0(b) hợp nhất module này vào lớp chung (`vi.ts`/`en.ts`), hook
// `useMonitorStrings()` được thay bằng `useT()` — key không đổi.
// Key chi tiết của khối cảnh báo do worker (c) thêm vào cùng file này khi cần.
import { useSyncExternalStore } from 'react'

export const monitorVi = {
  'monitor.title': 'Giám sát',
  'monitor.health.title': 'Sức khỏe',
  'monitor.health.recheck': 'Kiểm tra lại',
  'monitor.health.checking': 'Đang kiểm tra…',
  'monitor.health.errors': '{n} lỗi',
  'monitor.health.warnings': '{n} cảnh báo',
  'monitor.health.ok': 'ổn',
  'monitor.health.quota-gpu': 'Quota GPU: {value}',
  'monitor.health.ok-list': 'Ổn: {list}',
  'monitor.health.fix': 'Cách sửa:',
  'monitor.cost.title': 'Chi phí theo worker',
  'monitor.cost.total-today': 'Tổng hôm nay',
  'monitor.cost.total-month': 'Tổng tháng này',
  'monitor.cost.today': 'Hôm nay',
  'monitor.cost.month': 'Tháng này',
  'monitor.cost.empty': 'Chưa có worker nào.',
  'monitor.cost.no-cap': 'không đặt trần',
  'monitor.cost.cap-day': 'Trần ngày',
  'monitor.cost.cap-month': 'Trần tháng',
  'monitor.cost.quota-used': 'Đã dùng (7 ngày qua)',
  'monitor.cost.quota-remaining': 'Còn lại',
  'monitor.cost.quota-pct': '% quota đã dùng',
  'monitor.cost.quota-account': 'Quota tài khoản',
  'monitor.cost.kaggle-account': 'theo tài khoản Kaggle',
  'monitor.cost.runpod-hours': 'Giờ thuê tháng này',
  'monitor.cost.runpod-cost': 'Tiền thuê tháng này',
  'monitor.cost.running': 'đang chạy',
  'monitor.cost.stopped': 'đã dừng',
  'monitor.cost.relay': 'Relay',
  'monitor.cost.kaggle': 'Kaggle',
  'monitor.cost.runpod': 'RunPod',
  'monitor.alerts.title': 'Cảnh báo',
  'monitor.alerts.add-title': 'Thêm rule cảnh báo',
  'monitor.alerts.worker': 'Worker',
  'monitor.alerts.metric': 'Chỉ số',
  'monitor.alerts.threshold': 'Ngưỡng',
  'monitor.alerts.threshold-hint': 'Số — % với chỉ số %, USD với tiền thuê RunPod.',
  'monitor.alerts.save': 'Lưu rule',
  'monitor.alerts.saved': 'Đã lưu rule cảnh báo.',
  'monitor.alerts.rules-title': 'Rule đang có',
  'monitor.alerts.rules-empty': 'Chưa có rule nào — chủ tự tạo, không có rule mặc định.',
  'monitor.alerts.delete': 'Xóa',
  'monitor.alerts.deleted': 'Đã xóa rule.',
  'monitor.alerts.enabled': 'Đang bật',
  'monitor.alerts.history-title': 'Lịch sử cảnh báo',
  'monitor.alerts.history-empty': 'Chưa có cảnh báo nào.',
  'monitor.alerts.history-time': 'Thời gian',
  'monitor.alerts.history-value-col': 'Giá trị',
  'monitor.alerts.check-now': 'Kiểm tra ngay',
  'monitor.alerts.checking': 'Đang kiểm tra…',
  'monitor.alerts.checked-fired': 'Đã bắn {n} cảnh báo.',
  'monitor.alerts.checked-none': 'Không có cảnh báo nào vượt ngưỡng.',
  'monitor.alerts.ntfy-missing': 'Chưa cài ntfy: cảnh báo chỉ lưu vào lịch sử, không gửi về điện thoại.',
  'monitor.alerts.g4-note': 'Gói 4 đã có cảnh báo mặc định ở 80% trần relay — đặt trùng 80% sẽ nhận 2 tin.',
  'monitor.alerts.metric.cost-day-pct': 'Chi phí ngày (% trần)',
  'monitor.alerts.metric.cost-month-pct': 'Chi phí tháng (% trần)',
  'monitor.alerts.metric.kaggle-quota-pct': 'Quota Kaggle đã dùng (%)',
  'monitor.alerts.metric.runpod-month-usd': 'Tiền thuê RunPod tháng (USD)',
  'monitor.alerts.notified-yes': 'Đã gửi',
  'monitor.alerts.notified-no': 'Chưa gửi',
  'monitor.alerts.history-value': '{value} / ngưỡng {threshold}',
  'monitor.alerts.workers-empty': 'Chưa có worker nào.',
} as const

export type MonitorKey = keyof typeof monitorVi

export const monitorEn: Record<MonitorKey, string> = {
  'monitor.title': 'Monitor',
  'monitor.health.title': 'Health',
  'monitor.health.recheck': 'Check again',
  'monitor.health.checking': 'Checking…',
  'monitor.health.errors': '{n} errors',
  'monitor.health.warnings': '{n} warnings',
  'monitor.health.ok': 'OK',
  'monitor.health.quota-gpu': 'GPU quota: {value}',
  'monitor.health.ok-list': 'OK: {list}',
  'monitor.health.fix': 'Fix:',
  'monitor.cost.title': 'Spend by worker',
  'monitor.cost.total-today': 'Total today',
  'monitor.cost.total-month': 'Total this month',
  'monitor.cost.today': 'Today',
  'monitor.cost.month': 'This month',
  'monitor.cost.empty': 'No workers yet.',
  'monitor.cost.no-cap': 'no cap set',
  'monitor.cost.cap-day': 'Daily cap',
  'monitor.cost.cap-month': 'Monthly cap',
  'monitor.cost.quota-used': 'Used (last 7 days)',
  'monitor.cost.quota-remaining': 'Remaining',
  'monitor.cost.quota-pct': '% of quota used',
  'monitor.cost.quota-account': 'Account quota',
  'monitor.cost.kaggle-account': 'per Kaggle account',
  'monitor.cost.runpod-hours': 'Rented hours this month',
  'monitor.cost.runpod-cost': 'Rental cost this month',
  'monitor.cost.running': 'running',
  'monitor.cost.stopped': 'stopped',
  'monitor.cost.relay': 'Relay',
  'monitor.cost.kaggle': 'Kaggle',
  'monitor.cost.runpod': 'RunPod',
  'monitor.alerts.title': 'Alerts',
  'monitor.alerts.add-title': 'Add an alert rule',
  'monitor.alerts.worker': 'Worker',
  'monitor.alerts.metric': 'Metric',
  'monitor.alerts.threshold': 'Threshold',
  'monitor.alerts.threshold-hint': 'A number — % for percentage metrics, USD for RunPod rental.',
  'monitor.alerts.save': 'Save rule',
  'monitor.alerts.saved': 'Alert rule saved.',
  'monitor.alerts.rules-title': 'Current rules',
  'monitor.alerts.rules-empty': 'No rules yet — you create them; there are no default rules.',
  'monitor.alerts.delete': 'Delete',
  'monitor.alerts.deleted': 'Rule deleted.',
  'monitor.alerts.enabled': 'Enabled',
  'monitor.alerts.history-title': 'Alert history',
  'monitor.alerts.history-empty': 'No alerts yet.',
  'monitor.alerts.history-time': 'Time',
  'monitor.alerts.history-value-col': 'Value',
  'monitor.alerts.check-now': 'Check now',
  'monitor.alerts.checking': 'Checking…',
  'monitor.alerts.checked-fired': '{n} alert(s) fired.',
  'monitor.alerts.checked-none': 'No alert is over its threshold.',
  'monitor.alerts.ntfy-missing': 'ntfy is not set up: alerts are only saved to history, not sent to your phone.',
  'monitor.alerts.g4-note': 'Workspace already has a default alert at 80% of the relay cap — setting 80% here will send 2 messages.',
  'monitor.alerts.metric.cost-day-pct': 'Daily cost (% of cap)',
  'monitor.alerts.metric.cost-month-pct': 'Monthly cost (% of cap)',
  'monitor.alerts.metric.kaggle-quota-pct': 'Kaggle quota used (%)',
  'monitor.alerts.metric.runpod-month-usd': 'RunPod rental this month (USD)',
  'monitor.alerts.notified-yes': 'Sent',
  'monitor.alerts.notified-no': 'Not sent',
  'monitor.alerts.history-value': '{value} vs threshold {threshold}',
  'monitor.alerts.workers-empty': 'No workers yet.',
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
export function useMonitorStrings(): Record<MonitorKey, string> {
  const lang = useSyncExternalStore(subscribeLanguage, readLanguage, () => 'vi' as Language)
  return lang === 'en' ? monitorEn : monitorVi
}

// Thay `{name}` trong template bằng giá trị. Key template là chuỗi tĩnh, không key động.
export function fmt(template: string, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce((s, [k, v]) => s.replaceAll(`{${k}}`, String(v)), template)
}
