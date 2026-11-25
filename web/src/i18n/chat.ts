// Strings for the Chat page (Gói 2: multi-session + environment picker).
// Same pattern as web/src/i18n/env.ts: two dicts with the same key set,
// language from localStorage key `kite-language` (default `vi`).
// Gói 0(b) will merge this module into the global i18n layer later;
// the `chat.*` keys stay unchanged.

import { useLanguage, type Language } from './env'

const vi = {
  'chat.new-session': 'Phiên mới',
  'chat.no-sessions': 'Chưa có phiên nào.',
  'chat.untitled': 'Phiên chưa đặt tên',
  'chat.archive': 'Lưu trữ',
  'chat.archived': 'Đã lưu trữ phiên',
  'chat.delete': 'Xóa',
  'chat.rename': 'Đổi tên',
  'chat.environment': 'Môi trường',
  'chat.environment-default': 'Mặc định',
  'chat.picker-label': 'Môi trường cho phiên này',
  'chat.picker-help': 'Chọn repo để phiên này chuyển sang đó. Phiên đã ở repo thì không đổi được nữa.',
  'chat.picker-locked': 'Phiên này đã gắn với repo, không đổi môi trường được.',
  'chat.picker-placeholder': 'Chọn môi trường…',
  'chat.context-title': 'Ngữ cảnh của môi trường',
  'chat.context-lessons': 'bài học',
  'chat.context-open': 'Mở mục Ngữ cảnh',
  'chat.context-default': 'Phiên chưa gắn repo nào. Chọn một môi trường ở dưới để bắt đầu làm việc trên repo đó.',
  'chat.move-failed': 'Không chuyển được phiên sang môi trường đã chọn',
  'chat.session-archived': 'Phiên đã lưu trữ',
} as const

export type ChatKey = keyof typeof vi

const en: Record<ChatKey, string> = {
  'chat.new-session': 'New session',
  'chat.no-sessions': 'No sessions yet.',
  'chat.untitled': 'Untitled session',
  'chat.archive': 'Archive',
  'chat.archived': 'Session archived',
  'chat.delete': 'Delete',
  'chat.rename': 'Rename',
  'chat.environment': 'Environment',
  'chat.environment-default': 'Default',
  'chat.picker-label': 'Environment for this session',
  'chat.picker-help': 'Pick a repo and this session moves there. A session already on a repo cannot change.',
  'chat.picker-locked': 'This session is already attached to a repo and cannot change environment.',
  'chat.picker-placeholder': 'Choose an environment…',
  'chat.context-title': 'Environment context',
  'chat.context-lessons': 'lessons',
  'chat.context-open': 'Open the Context section',
  'chat.context-default': 'This session is not attached to any repo yet. Pick an environment below to start working on a repo.',
  'chat.move-failed': 'Could not move the session to the chosen environment',
  'chat.session-archived': 'Session archived',
}

const dicts: Record<Language, Record<ChatKey, string>> = { vi: vi as unknown as Record<ChatKey, string>, en }

/** Temporary hook until Gói 0(b) merges this module into the global i18n layer. */
export function useChatStrings(): Record<ChatKey, string> {
  return dicts[useLanguage()]
}
