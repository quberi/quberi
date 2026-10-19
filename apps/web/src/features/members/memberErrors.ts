import type { TFunction } from 'i18next'
import { ApiError } from '../api/client'

/** The words to show for a failed deactivate / reactivate / handover request. */
export function offboardingErrorText(t: TFunction, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code) {
      const key = `offboarding.error.${error.code}`
      // An empty default means "no wording for this code": fall through to the server text.
      const text = t(key, { defaultValue: '' })
      if (text) return text
    }
    return error.detail || `HTTP ${error.status}`
  }
  return String(error)
}
