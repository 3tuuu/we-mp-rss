import http from './http'

export interface AuthIssue { code: string | number; message: string; at?: number }
export interface MobileLogin {
  status: 'idle' | 'starting' | 'waiting' | 'scanned' | 'success' | 'failed' | 'expired'
  message: string
  qrcode: string
}
export interface MobileStatus {
  authorized: boolean
  automatic_for_new_feeds: boolean
  auto_refresh: boolean
  maintenance_interval_minutes: number
  refresh_interval_hours: number
  needs_reauth: boolean
  authorized_at?: number
  refreshed_at?: number
  refresh_retry_at?: number
  refresh_error?: AuthIssue
  web_auth?: { valid?: boolean; needs_reauth?: boolean; checked_at?: number; refreshed_at?: number; error?: AuthIssue }
  feed_errors: Record<string, { code?: string | number; message?: string; cooldown_until?: number }>
  login: MobileLogin
}
export interface FeedOption { id: string; mp_name: string }
export interface HistoryResult {
  collected: number
  next_offset: number
  has_more: boolean
  stop_reason: string
}
export interface HistoryPage { items: { reviewId: string }[]; next_offset: number; has_more: boolean }
const base = '/wx/weread/mobile'
export const getMobileStatus = () => http.get<unknown, MobileStatus>(base + '/status')
export const startMobileLogin = () => http.post<unknown, MobileLogin>(base + '/qr/code', {})
export const getMobileFeeds = (offset: number) => http.get<unknown, { list: FeedOption[]; total: number }>('/wx/mps', { params: { offset, limit: 100 } })
export const collectMobileHistory = (mp_id: string, offset: number) => http.post<unknown, HistoryResult>(base + '/collect', { mp_id, offset, page_size: 5, max_pages: 2 })
export const verifyMobileHistory = (mp_id: string) => http.post<unknown, { first: HistoryPage; second: HistoryPage }>(base + '/verify', { mp_id, page_size: 5, enable: false })
