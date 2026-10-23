import { AdminApiError } from '../api'
import type { I18n } from './index'

/**
 * The text to show for a failed admin request, in the active language. One helper for
 * every view and gate. In order:
 *  1. an `AdminApiError` whose `code` the dictionary knows -> that text, with its params;
 *  2. one with a `detail` from the server -> the detail as sent (the server already
 *     honours `Accept-Language`);
 *  3. a 422 validation body (code `validation`) -> `error.validation`;
 *  4. any other `AdminApiError` -> `error.server` (5xx) or `error.status`;
 *  5. anything else (a network `TypeError`) -> `error.network`.
 */
export function errorMessage(
  err: unknown,
  tCode: I18n['tCode'],
  t: I18n['t'],
): string {
  if (err instanceof AdminApiError) {
    if (err.code) {
      const known = tCode(err.code, err.params)
      if (known !== null) return known
    }
    if (err.detail) return err.detail
    if (err.code === 'validation') return t('error.validation')
    if (err.status >= 500) return t('error.server')
    return t('error.status', { status: err.status })
  }
  return t('error.network')
}
