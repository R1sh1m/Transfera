import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'
import { isAxiosError } from 'axios'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

const KNOWN_FRIENDLY_MESSAGES: Record<string, string> = {
  'Network Error': "Could not reach the server. Check that the app is still running and try again.",
}

const STATUS_FRIENDLY_MESSAGES: Record<number, string> = {
  404: 'The requested item was not found. It may have been removed or no longer available.',
  409: 'A conflict occurred. The item may already exist or is in an unexpected state.',
  422: 'The request was invalid. Please check your input and try again.',
}

function isTimeoutError(error: unknown): boolean {
  if (isAxiosError(error) && error.code === 'ECONNABORTED') return true
  if (error instanceof Error && /timeout/i.test(error.message)) return true
  return false
}

function isServerError(error: unknown): boolean {
  if (isAxiosError(error) && error.response && error.response.status >= 500) return true
  return false
}

function getStatusFromError(error: unknown): number | undefined {
  if (isAxiosError(error) && error.response) {
    return error.response.status
  }
  return undefined
}

function getRawDetail(error: unknown): string | undefined {
  if (isAxiosError(error) && error.response?.data) {
    const detail = error.response.data as Record<string, unknown>
    if (typeof detail.detail === 'string') return detail.detail
    if (Array.isArray(detail.detail)) return JSON.stringify(detail.detail)
  }
  if (error instanceof Error) return error.message
  return undefined
}

export function toUserFriendlyError(error: unknown): string {
  // Always log the full technical detail for debugging
  const raw = getRawDetail(error)
  if (raw) {
    console.warn('[api] Error:', raw)
  }

  // 1. Timeout errors — most common user-facing failure
  if (isTimeoutError(error)) {
    return 'This is taking longer than expected. You can keep waiting or try again.'
  }

  // 2. Network error (server unreachable)
  if (isAxiosError(error) && !error.response && error.message === 'Network Error') {
    return "Could not reach the server. Check that the app is still running and try again."
  }

  // 3. Known HTTP status codes with friendly messages
  const status = getStatusFromError(error)
  if (status && STATUS_FRIENDLY_MESSAGES[status]) {
    return STATUS_FRIENDLY_MESSAGES[status]
  }

  // 4. Server errors (500+)
  if (isServerError(error)) {
    return 'The server encountered a problem. Please try again.'
  }

  // 5. Known generic error message patterns
  if (error instanceof Error) {
    const friendly = KNOWN_FRIENDLY_MESSAGES[error.message]
    if (friendly) return friendly
  }

  // 6. Backend provided a detail message — use it as-is (it's usually decent)
  if (raw && raw.length < 200) {
    return raw
  }

  // 7. Fallback
  if (error instanceof Error) {
    return `Something went wrong. Please try again.`
  }

  return 'Something went wrong. Please try again.'
}

export function extractErrorMessage(error: unknown): string {
  return toUserFriendlyError(error)
}

export const isElectron: boolean =
  typeof window !== 'undefined' &&
  !!(window as unknown as Record<string, unknown>).electronAPI
