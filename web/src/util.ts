import { notifications } from '@mantine/notifications'
import { useCallback, useEffect, useRef, useState } from 'react'
import { getLanguage } from './i18n'

export function notifyError(error: unknown, title?: string) {
  const message = error instanceof Error ? error.message : String(error)
  notifications.show({
    color: 'red',
    title: title ?? (getLanguage() === 'en' ? 'Something went wrong' : 'Không làm được'),
    message,
    autoClose: 8000,
  })
}

export function notifyOk(message: string, title?: string) {
  notifications.show({ color: 'teal', title, message, autoClose: 4000 })
}

/** Run an action, show its error; returns the result or undefined. */
export async function attempt<T>(fn: () => Promise<T>, ok?: string): Promise<T | undefined> {
  try {
    const result = await fn()
    if (ok) notifyOk(ok)
    return result
  } catch (error) {
    notifyError(error)
    return undefined
  }
}

/** Load data, optionally refresh every `every` ms (paused while the tab is hidden). */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], every?: number) {
  const [data, setData] = useState<T | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const reload = useCallback(async () => {
    try {
      const value = await fnRef.current()
      setData(value)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => {
    setLoading(true)
    reload()
    if (!every) return
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') reload()
    }, every)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { data, error, loading, reload, setData }
}

/** Seconds, milliseconds or an ISO string → Date. */
export function toDate(value: unknown): Date | null {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'number') return new Date(value > 1e12 ? value : value * 1000)
  const d = new Date(String(value))
  return Number.isNaN(d.getTime()) ? null : d
}

export function fmtTime(value: unknown): string {
  const d = toDate(value)
  if (!d) return '—'
  return d.toLocaleString('vi-VN', { hour12: false, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function ago(value: unknown): string {
  const d = toDate(value)
  if (!d) return '—'
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000))
  if (s < 60) return `${s} giây trước`
  if (s < 3600) return `${Math.round(s / 60)} phút trước`
  if (s < 86400) return `${Math.round(s / 3600)} giờ trước`
  return `${Math.round(s / 86400)} ngày trước`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s} giây`
  if (s < 3600) return `${Math.floor(s / 60)} phút ${s % 60 ? `${s % 60} giây` : ''}`.trim()
  return `${Math.floor(s / 3600)} giờ ${Math.round((s % 3600) / 60)} phút`
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value < 1 && value > 0 ? value.toFixed(4) : value.toFixed(2)} USD`
}

export function num(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('vi-VN')
}

export type TaskStateKey = 'queued' | 'leased' | 'completed' | 'failed' | 'cancelled' | 'running' | 'terminated' | 'timed-out'

export const TASK_STATES: Record<string, { label: string; color: string; key: TaskStateKey }> = {
  QUEUED: { label: 'đang chờ', color: 'yellow', key: 'queued' },
  LEASED: { label: 'đang chạy', color: 'blue', key: 'leased' },
  COMPLETED: { label: 'xong', color: 'teal', key: 'completed' },
  FAILED: { label: 'lỗi', color: 'red', key: 'failed' },
  CANCELLED: { label: 'đã hủy', color: 'gray', key: 'cancelled' },
  RUNNING: { label: 'đang chạy', color: 'blue', key: 'running' },
  TERMINATED: { label: 'đã dừng', color: 'gray', key: 'terminated' },
  TIMED_OUT: { label: 'quá giờ', color: 'orange', key: 'timed-out' },
}

export function taskState(state: string | undefined | null) {
  return TASK_STATES[state ?? ''] ?? { label: state ?? '?', color: 'gray', key: null as TaskStateKey | null }
}

export type WorkerStateKey = 'disabled' | 'switch-off' | 'stopping' | 'busy' | 'idle' | 'starting' | 'offline'

export function workerState(w: {
  enabled: boolean
  alive: boolean
  busy: boolean
  draining?: boolean
  switch?: { on: boolean }
  start?: Record<string, any> | null
}): { label: string; color: string; key: WorkerStateKey } {
  if (!w.enabled) return { label: 'tắt trong cấu hình', color: 'gray', key: 'disabled' }
  if (w.switch && !w.switch.on) return { label: 'công tắc tắt', color: 'gray', key: 'switch-off' }
  if (w.draining) return { label: 'đang dừng', color: 'orange', key: 'stopping' }
  if (w.alive && w.busy) return { label: 'bận', color: 'blue', key: 'busy' }
  if (w.alive) return { label: 'rảnh', color: 'teal', key: 'idle' }
  const start = w.start ?? {}
  const phase = String(start.state ?? start.status ?? start.phase ?? '')
  if (/start|queue|wait|boot|launch|running/i.test(phase)) return { label: 'đang khởi động', color: 'yellow', key: 'starting' }
  return { label: 'chưa kết nối', color: 'gray', key: 'offline' }
}

export function short(text: unknown, n = 120): string {
  const s = typeof text === 'string' ? text : JSON.stringify(text)
  if (!s) return ''
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}
