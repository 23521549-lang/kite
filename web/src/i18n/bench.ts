// Chuỗi UI trang Bench — namespace `bench.*` (theo lớp i18n của Gói 0(b)).
// Hai dict có cùng tập key (kiểm bằng TypeScript: `Record<BenchKey, string>`).
// Các key `bench.title` / `bench.suite.*` / `bench.new.*` / `bench.history.*` đã có
// trong lớp chung của Gói 0(b) — giá trị ở đây giữ nguyên văn bản đó để lúc hợp
// nhất chỉ việc dời key, không đổi wording. Key mới của Gói 7 (radar, official,
// network, axis, level) thêm vào dưới; key cũ không sửa.
// Khi Gói 0(b) hợp nhất module này vào lớp chung (`vi.ts`/`en.ts`), hook
// `useBenchStrings()` được thay bằng `useT()` — key không đổi.
import { useSyncExternalStore } from 'react'

export const benchVi = {
  'bench.title': 'Bench',
  'bench.suite.code': 'code: sửa code thật, Kite tự test',
  'bench.suite.lead': 'lead: agent trưởng chọn tool đúng',
  'bench.suite.speed': 'speed: tốc độ model',
  'bench.new-run': 'Chạy đo mới',
  'bench.new.label': 'Tên lần đo',
  'bench.new.models': 'Model (để trống: tất cả)',
  'bench.new.worker': 'Worker (để trống: worker rảnh)',
  'bench.new.suites': 'Bộ đo',
  'bench.new.started': 'Đã bắt đầu đo ở nền',
  'bench.new.run-bg': 'Chạy ở nền',
  'bench.new.note': 'Đo dùng GPU của worker; task của bạn sẽ phải chờ.',
  'bench.new.exclusive-note':
    'Bench chạy độc quyền: trong lúc đo, task mới bị từ chối; còn task đang chạy thì bench phải chờ.',
  'bench.new.network-title': 'Chạy có mạng',
  'bench.new.network-hosts': 'Các host đã khai báo',
  'bench.new.network-confirm': 'Tôi đã xem danh sách host và đồng ý cho lần đo này dùng mạng tới đúng các host đó.',
  'bench.new.network-empty': 'Chưa có bài bench nào khai báo mạng.',
  'bench.new.network-note': 'Bench có mạng không vào tập gác cổng.',
  'bench.history': 'Các lần đo',
  'bench.history.refresh': 'Làm mới',
  'bench.history.empty': 'Chưa có lần đo nào.',
  'bench.history.table-note': 'Bảng như kite bench history',
  'bench.official.title': 'Mức chính thức',
  'bench.official.not-official': 'chưa chính thức',
  'bench.official.runs': '{n} lần chạy',
  'bench.official.unstable': 'mức không ổn định: các lần chạy cắt qua biên ngưỡng',
  'bench.official.criteria-version': 'Tiêu chí {version}',
  'bench.runs.title': 'Điểm từng lần chạy',
  'bench.runs.run': 'Lần đo',
  'bench.runs.started': 'Bắt đầu',
  'bench.runs.model': 'Model',
  'bench.runs.overall': 'Overall',
  'bench.runs.level': 'Mức',
  'bench.runs.criteria': 'Tiêu chí',
  'bench.runs.network': 'Mạng',
  'bench.runs.network-offline': 'offline',
  'bench.runs.network-allowlist': 'allowlist',
  'bench.runs.unstable': 'không ổn định',
  'bench.axes.title': 'Điểm 8 trục (lần mới nhất)',
  'bench.axes.axis': 'Trục',
  'bench.axes.score': 'Điểm',
  'bench.radar.title': 'Radar 8 trục',
  'bench.radar.empty': 'Chưa có điểm trục để vẽ radar.',
  'bench.radar.latest': 'mới nhất',
  'bench.radar.previous': 'trước đó',
  'bench.axis.coding': 'Coding',
  'bench.axis.phan-tich': 'Phân tích',
  'bench.axis.suy-luan': 'Suy luận',
  'bench.axis.toc-do': 'Tốc độ',
  'bench.axis.dung-viec': 'Đúng việc',
  'bench.axis.chi-phi': 'Chi phí',
  'bench.axis.on-dinh': 'Ổn định',
  'bench.axis.tri-nho': 'Trí nhớ',
  'bench.level.1.name': 'Mới bắt đầu',
  'bench.level.1.desc': 'Cần hướng dẫn từng bước; chỉ làm được việc quen thuộc, có người dẫn.',
  'bench.level.2.name': 'Mới có kinh nghiệm',
  'bench.level.2.desc': 'Tự làm được việc thường gặp; vẫn cần giúp khi tình huống thay đổi.',
  'bench.level.3.name': 'Vững việc',
  'bench.level.3.desc': 'Tự lập kế hoạch và làm xong việc chuẩn từ đầu tới cuối; thấy được lỗi thường gặp.',
  'bench.level.4.name': 'Thành thạo',
  'bench.level.4.desc': 'Nhìn được toàn cảnh, biết ưu tiên và ứng biến khi tình huống thay đổi.',
  'bench.level.5.name': 'Chuyên gia',
  'bench.level.5.desc': 'Làm theo trực giác từ kinh nghiệm sâu; xử lý được ca khó nhất.',
} as const

export type BenchKey = keyof typeof benchVi

export const benchEn: Record<BenchKey, string> = {
  'bench.title': 'Bench',
  'bench.suite.code': 'code: fix real code, Kite tests itself',
  'bench.suite.lead': 'lead: lead agent picks the right tool',
  'bench.suite.speed': 'speed: model speed',
  'bench.new-run': 'New run',
  'bench.new.label': 'Run name',
  'bench.new.models': 'Models (empty: all)',
  'bench.new.worker': 'Worker (empty: an idle worker)',
  'bench.new.suites': 'Suites',
  'bench.new.started': 'Measurement started in the background',
  'bench.new.run-bg': 'Run in background',
  'bench.new.note': 'Measurement uses worker GPU; your tasks will have to wait.',
  'bench.new.exclusive-note':
    'Bench runs exclusively: while measuring, new tasks are refused; while a task runs, bench has to wait.',
  'bench.new.network-title': 'Networked run',
  'bench.new.network-hosts': 'Declared hosts',
  'bench.new.network-confirm': 'I have seen the host list and approve this run reaching exactly those hosts.',
  'bench.new.network-empty': 'No bench task declares network access.',
  'bench.new.network-note': 'A networked bench never joins the gate set.',
  'bench.history': 'Runs',
  'bench.history.refresh': 'Refresh',
  'bench.history.empty': 'No runs yet.',
  'bench.history.table-note': 'Table as in kite bench history',
  'bench.official.title': 'Official level',
  'bench.official.not-official': 'not official yet',
  'bench.official.runs': '{n} runs',
  'bench.official.unstable': 'unstable level: runs straddle a threshold',
  'bench.official.criteria-version': 'Criteria {version}',
  'bench.runs.title': 'Per-run scores',
  'bench.runs.run': 'Run',
  'bench.runs.started': 'Started',
  'bench.runs.model': 'Model',
  'bench.runs.overall': 'Overall',
  'bench.runs.level': 'Level',
  'bench.runs.criteria': 'Criteria',
  'bench.runs.network': 'Network',
  'bench.runs.network-offline': 'offline',
  'bench.runs.network-allowlist': 'allowlist',
  'bench.runs.unstable': 'unstable',
  'bench.axes.title': '8-axis scores (latest run)',
  'bench.axes.axis': 'Axis',
  'bench.axes.score': 'Score',
  'bench.radar.title': '8-axis radar',
  'bench.radar.empty': 'No axis scores to draw the radar yet.',
  'bench.radar.latest': 'latest',
  'bench.radar.previous': 'previous',
  'bench.axis.coding': 'Coding',
  'bench.axis.phan-tich': 'Analysis',
  'bench.axis.suy-luan': 'Reasoning',
  'bench.axis.toc-do': 'Speed',
  'bench.axis.dung-viec': 'Right task',
  'bench.axis.chi-phi': 'Cost',
  'bench.axis.on-dinh': 'Stability',
  'bench.axis.tri-nho': 'Memory',
  'bench.level.1.name': 'Novice',
  'bench.level.1.desc': 'Needs step-by-step guidance; succeeds only on familiar, guided work.',
  'bench.level.2.name': 'Advanced Beginner',
  'bench.level.2.desc': 'Handles routine work alone; still needs help when the situation changes.',
  'bench.level.3.name': 'Competent',
  'bench.level.3.desc': 'Plans and finishes standard work end to end; spots the usual problems.',
  'bench.level.4.name': 'Proficient',
  'bench.level.4.desc': 'Sees the whole situation, prioritises well and adapts when it changes.',
  'bench.level.5.name': 'Expert',
  'bench.level.5.desc': 'Acts on intuition built from deep experience; handles the hardest cases.',
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
export function useBenchStrings(): Record<BenchKey, string> {
  const lang = useSyncExternalStore(subscribeLanguage, readLanguage, () => 'vi' as Language)
  return lang === 'en' ? benchEn : benchVi
}

// Thay `{name}` trong template bằng giá trị. Key template là chuỗi tĩnh, không key động.
export function fmt(template: string, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce((s, [k, v]) => s.replaceAll(`{${k}}`, String(v)), template)
}
