import { useTranslation } from 'react-i18next'

/**
 * Date/time formatting that follows the **interface language**.
 *
 * ``Date#toLocaleString()`` with no locale uses the browser's own language,
 * so a visitor on a Chinese-language browser who switched Quberi to English
 * still saw "2026/10/9 13:54:51". These helpers take the locale from i18next
 * instead, so the whole page agrees with the language selector.
 */

export type DateStyle = 'date' | 'time' | 'datetime'

const OPTIONS: Record<DateStyle, Intl.DateTimeFormatOptions> = {
  date: { year: 'numeric', month: 'short', day: 'numeric' },
  time: { hour: '2-digit', minute: '2-digit', second: '2-digit' },
  datetime: {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  },
}

export function formatDate(
  value: string | number | Date | null | undefined,
  locale: string,
  style: DateStyle = 'datetime',
): string {
  if (value === null || value === undefined || value === '') return '—'
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(locale, OPTIONS[style]).format(date)
}

/** Hook form: ``const fmt = useFormatDate(); fmt(doc.created_at, 'date')``. */
export function useFormatDate(): (
  value: string | number | Date | null | undefined,
  style?: DateStyle,
) => string {
  const { i18n } = useTranslation()
  const locale = i18n.resolvedLanguage === 'zh-CN' ? 'zh-CN' : 'en-US'
  return (value, style = 'datetime') => formatDate(value, locale, style)
}
