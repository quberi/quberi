/**
 * Language support for the platform admin console.
 *
 * Deliberately separate from the main app's i18n setup and dependency-free: the
 * admin console is its own Vite entry, and two languages do not need a library.
 * See ./README.md for how to add a string or a language.
 *
 * State is a tiny module-level store read with `useSyncExternalStore`, so no
 * provider is needed. It is initialised lazily on first read, which lets tests set
 * `localStorage` / `navigator.language` before first use.
 */

import { useEffect, useMemo, useSyncExternalStore } from 'react'

import { en } from './en'
import { zh } from './zh'

export type Lang = 'en' | 'zh'
/** A message: a plain string, or `{one, other}` where the plural form matters (English). */
export type Entry = string | { readonly one: string; readonly other: string }
export type Dict = Record<string, Entry>
export type MessageKey = keyof typeof en

/** A parameter value that is itself a backend catalog message (see the API's `ref()`). */
export interface MessageRef {
  code: string
  params?: Record<string, ParamValue>
}
export type ParamValue = string | number | MessageRef
export type Params = Record<string, ParamValue>

export const LANG_STORAGE_KEY = 'quberi.admin.lang'
export const LOCALE: Record<Lang, string> = { en: 'en-US', zh: 'zh-CN' }
export const HTML_LANG: Record<Lang, string> = { en: 'en', zh: 'zh-CN' }
export const DOC_TITLE: Record<Lang, string> = {
  en: 'Quberi Platform Admin',
  zh: 'Quberi 平台管理',
}

const DICTS: Record<Lang, Dict> = { en, zh }

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

let current: Lang | null = null
const listeners = new Set<() => void>()

function isLang(value: unknown): value is Lang {
  return value === 'en' || value === 'zh'
}

function readStored(): Lang | null {
  try {
    const stored = window.localStorage.getItem(LANG_STORAGE_KEY)
    return isLang(stored) ? stored : null
  } catch {
    return null
  }
}

function detect(): Lang {
  const stored = readStored()
  if (stored) return stored
  const nav = typeof navigator !== 'undefined' ? navigator.language : ''
  return typeof nav === 'string' && nav.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

function applyDocument(lang: Lang): void {
  if (typeof document === 'undefined') return
  if (document.documentElement.lang !== HTML_LANG[lang]) {
    document.documentElement.lang = HTML_LANG[lang]
  }
  if (document.title !== DOC_TITLE[lang]) document.title = DOC_TITLE[lang]
}

/** The active language. The first call decides it (stored choice, else the browser's). */
export function getLang(): Lang {
  if (current === null) {
    current = detect()
    applyDocument(current)
  }
  return current
}

/** Switch language: remembered across visits, applied to the document, re-renders subscribers. */
export function setLang(lang: Lang): void {
  const changed = getLang() !== lang
  current = lang
  try {
    window.localStorage.setItem(LANG_STORAGE_KEY, lang)
  } catch {
    /* storage may be unavailable; the choice then lasts for this visit only */
  }
  applyDocument(lang)
  if (changed) listeners.forEach((listener) => listener())
}

export function subscribeLang(callback: () => void): () => void {
  listeners.add(callback)
  return () => {
    listeners.delete(callback)
  }
}

// ---------------------------------------------------------------------------
// Translation
// ---------------------------------------------------------------------------

function lookup(lang: Lang, key: string): Entry | undefined {
  const dict = DICTS[lang]
  return Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : undefined
}

const PLACEHOLDER = /\{(\w+)\}/g

/**
 * The text for `key` in `lang` with `params` filled in, in one pass: a value is never
 * searched for further placeholders and an unknown `{name}` is left as written.
 * A `{one, other}` entry picks its form from `params.n`. Falls back to English, then to
 * the key itself.
 */
export function translate(lang: Lang, key: string, params?: Params): string {
  const entry = lookup(lang, key) ?? lookup('en', key)
  if (entry === undefined) return key
  let template: string
  if (typeof entry === 'string') {
    template = entry
  } else {
    const n = params?.n
    const form =
      typeof n === 'number' ? new Intl.PluralRules(LOCALE[lang]).select(n) : 'other'
    template = form === 'one' ? entry.one : entry.other
  }
  if (!params) return template
  return template.replace(PLACEHOLDER, (whole, name: string) => {
    if (!Object.prototype.hasOwnProperty.call(params, name)) return whole
    const value = params[name]
    if (typeof value === 'object' && value !== null) {
      return translate(lang, `msg.${value.code}`, value.params)
    }
    return String(value)
  })
}

/** True when the dictionary has the backend message `code` (as `msg.<code>`). */
export function hasCode(code: string): boolean {
  return lookup('en', `msg.${code}`) !== undefined
}

const numberFormats = new Map<string, Intl.NumberFormat>()
const dateFormats = new Map<string, Intl.DateTimeFormat>()

export function formatNumber(lang: Lang, n: number): string {
  const locale = LOCALE[lang]
  let format = numberFormats.get(locale)
  if (!format) {
    format = new Intl.NumberFormat(locale)
    numberFormats.set(locale, format)
  }
  return format.format(n)
}

export function formatDateTime(lang: Lang, value: string | number | Date): string {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const locale = LOCALE[lang]
  let format = dateFormats.get(locale)
  if (!format) {
    format = new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' })
    dateFormats.set(locale, format)
  }
  return format.format(date)
}

export interface I18n {
  lang: Lang
  locale: string
  setLang: (lang: Lang) => void
  t: (key: MessageKey, params?: Params) => string
  /** Text for a backend message code, or null when the dictionary has no entry. */
  tCode: (code: string, params?: Params) => string | null
  formatNumber: (n: number) => string
  formatDateTime: (value: string | number | Date) => string
}

/** The active language and its helpers; re-renders the component when the language changes. */
export function useLang(): I18n {
  const lang = useSyncExternalStore(subscribeLang, getLang, getLang)
  // Idempotent; getLang() already applied these on first read.
  useEffect(() => applyDocument(lang), [lang])
  return useMemo<I18n>(
    () => ({
      lang,
      locale: LOCALE[lang],
      setLang,
      t: (key, params) => translate(lang, key, params),
      tCode: (code, params) =>
        hasCode(code) ? translate(lang, `msg.${code}`, params) : null,
      formatNumber: (n) => formatNumber(lang, n),
      formatDateTime: (value) => formatDateTime(lang, value),
    }),
    [lang],
  )
}
