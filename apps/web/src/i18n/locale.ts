export type Locale = 'en-US' | 'zh-CN'

export const LOCALE_STORAGE_KEY = 'quberi.locale'

/**
 * Which language the app opens in.
 *
 * 1. An explicit choice made with the language switch always wins.
 * 2. Otherwise the first browser language we have a translation for
 *    (any `zh-*` gives Simplified Chinese, any `en-*` gives English).
 * 3. Otherwise English.
 */
export function pickLocale(
  stored: string | null,
  browserLanguages: readonly string[],
): Locale {
  if (stored === 'en-US' || stored === 'zh-CN') return stored
  for (const language of browserLanguages) {
    const tag = language.toLowerCase()
    if (tag.startsWith('zh')) return 'zh-CN'
    if (tag.startsWith('en')) return 'en-US'
  }
  return 'en-US'
}

/** The value for `<html lang>`, so screen readers and translation prompts match the UI. */
export function htmlLangFor(locale: string): string {
  return locale === 'zh-CN' ? 'zh-CN' : 'en'
}
