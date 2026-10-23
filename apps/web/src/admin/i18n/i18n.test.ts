import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { en } from './en'
import {
  DOC_TITLE,
  formatDateTime,
  formatNumber,
  getLang,
  HTML_LANG,
  LANG_STORAGE_KEY,
  type MessageKey,
  setLang,
  translate,
} from './index'
import { zh } from './zh'

const sources = import.meta.glob(['../**/*.ts', '../**/*.tsx', '!../**/*.test.ts', '!../**/*.test.tsx'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

function placeholders(entry: unknown): string[] {
  const texts = typeof entry === 'string' ? [entry] : Object.values(entry as object)
  const names = new Set<string>()
  for (const text of texts as string[]) {
    for (const match of text.matchAll(/\{(\w+)\}/g)) names.add(match[1])
  }
  return [...names].sort()
}

beforeEach(() => {
  window.localStorage.removeItem(LANG_STORAGE_KEY)
  setLang('en')
})
afterEach(() => {
  window.localStorage.removeItem(LANG_STORAGE_KEY)
  vi.restoreAllMocks()
})

describe('dictionaries', () => {
  it('have the same keys', () => {
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort())
  })

  it('use the same {param} names per key', () => {
    for (const key of Object.keys(en) as MessageKey[]) {
      expect(placeholders(zh[key]), key).toEqual(placeholders(en[key]))
    }
  })

  it('only use {one, other} plural entries in English, and plain strings in Chinese', () => {
    for (const key of Object.keys(en) as MessageKey[]) {
      const entry = en[key] as unknown
      if (typeof entry !== 'string') {
        expect(Object.keys(entry as object).sort(), key).toEqual(['one', 'other'])
      }
      expect(typeof zh[key], key).toBe('string')
    }
  })
})

describe('translate', () => {
  it('fills {param} placeholders', () => {
    expect(translate('en', 'gate.password.hint', { min: 12, max: 128 })).toContain(
      '(12 to 128 characters)',
    )
    expect(translate('zh', 'gate.password.hint', { min: 12, max: 128 })).toContain(
      '12 到 128',
    )
  })

  it('leaves an unknown {name} as written, and a value is not interpolated again', () => {
    expect(translate('en', 'msg.admin.oauth.url_placeholder_not_allowed', { subject: 'It' })).toBe(
      'It may not contain {placeholder}.',
    )
    expect(
      translate('en', 'msg.admin.orgs.not_found', { id: '{min}', min: 12 }),
    ).toBe('Organization not found: {min}')
    expect(
      translate('en', 'msg.admin.oauth.url_placeholder_not_allowed', {
        subject: 'The URL',
        placeholder: '{id}',
      }),
    ).toBe('The URL may not contain {id}.')
  })

  it('renders message refs in the active language', () => {
    const params = {
      subject: { code: 'admin.oauth.subject.redirect', params: { rule: "'https://x.example.test/cb'" } },
    }
    expect(translate('en', 'msg.admin.oauth.url_not_https', params)).toBe(
      "Redirect 'https://x.example.test/cb' must be an https:// address.",
    )
    expect(translate('zh', 'msg.admin.oauth.url_not_https', params)).toBe(
      "回调地址 'https://x.example.test/cb'必须是 https:// 地址。",
    )
  })

  it('falls back to English and then to the key', () => {
    expect(translate('zh', 'no.such.key')).toBe('no.such.key')
  })

  it('picks plural forms from n (English), and uses one form in Chinese', () => {
    const plural = { one: '{n} file', other: '{n} files' }
    const dict = en as unknown as Record<string, unknown>
    dict['test.plural'] = plural
    ;(zh as unknown as Record<string, unknown>)['test.plural'] = '{n} 个文件'
    try {
      expect(translate('en', 'test.plural', { n: 1 })).toBe('1 file')
      expect(translate('en', 'test.plural', { n: 2 })).toBe('2 files')
      expect(translate('en', 'test.plural', { n: 0 })).toBe('0 files')
      expect(translate('zh', 'test.plural', { n: 1 })).toBe('1 个文件')
      expect(translate('zh', 'test.plural', { n: 2 })).toBe('2 个文件')
    } finally {
      delete dict['test.plural']
      delete (zh as unknown as Record<string, unknown>)['test.plural']
    }
  })
})

describe('language selection', () => {
  async function freshModule() {
    vi.resetModules()
    return import('./index')
  }

  it.each([
    ['zh-CN', 'zh'],
    ['zh-TW', 'zh'],
    ['zh', 'zh'],
    ['en-US', 'en'],
    ['fr', 'en'],
  ])('defaults to the browser language %s -> %s', async (navigatorLanguage, expected) => {
    vi.spyOn(window.navigator, 'language', 'get').mockReturnValue(navigatorLanguage)
    window.localStorage.removeItem(LANG_STORAGE_KEY)
    const fresh = await freshModule()
    expect(fresh.getLang()).toBe(expected)
  })

  it('prefers a stored choice over the browser language', async () => {
    vi.spyOn(window.navigator, 'language', 'get').mockReturnValue('zh-CN')
    window.localStorage.setItem(LANG_STORAGE_KEY, 'en')
    expect((await freshModule()).getLang()).toBe('en')
    window.localStorage.setItem(LANG_STORAGE_KEY, 'zh')
    vi.spyOn(window.navigator, 'language', 'get').mockReturnValue('en-US')
    expect((await freshModule()).getLang()).toBe('zh')
  })

  it('ignores an invalid stored value', async () => {
    vi.spyOn(window.navigator, 'language', 'get').mockReturnValue('en-US')
    window.localStorage.setItem(LANG_STORAGE_KEY, 'klingon')
    expect((await freshModule()).getLang()).toBe('en')
  })

  it('does not decide the language until first use', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, 'en')
    const fresh = await freshModule()
    window.localStorage.setItem(LANG_STORAGE_KEY, 'zh')
    expect(fresh.getLang()).toBe('zh')
  })

  it('setLang persists, and sets <html lang> and the document title', () => {
    setLang('zh')
    expect(getLang()).toBe('zh')
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe('zh')
    expect(document.documentElement.lang).toBe('zh-CN')
    expect(document.title).toBe('Quberi 平台管理')
    setLang('en')
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe('en')
    expect(document.documentElement.lang).toBe('en')
    expect(document.title).toBe('Quberi Platform Admin')
    expect(HTML_LANG).toEqual({ en: 'en', zh: 'zh-CN' })
    expect(DOC_TITLE.zh).toBe('Quberi 平台管理')
  })

  it('survives a localStorage that throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(window.navigator, 'language', 'get').mockReturnValue('zh-CN')
    const fresh = await freshModule()
    expect(fresh.getLang()).toBe('zh')
    expect(() => fresh.setLang('en')).not.toThrow()
    expect(fresh.getLang()).toBe('en')
  })
})

describe('formatting', () => {
  it('formats numbers and dates for the language', () => {
    expect(formatNumber('en', 1234567)).toBe('1,234,567')
    expect(formatNumber('zh', 1234567)).toBe('1,234,567')
    expect(formatNumber('en', 0.5)).toBe('0.5')
    const iso = '2025-03-04T05:06:00Z'
    expect(formatDateTime('en', iso)).not.toBe(formatDateTime('zh', iso))
    expect(formatDateTime('zh', iso)).toMatch(/2025/)
  })

  it('returns the raw text for an unparseable date', () => {
    expect(formatDateTime('en', 'not a date')).toBe('not a date')
  })
})

describe('independence from the main app i18n library', () => {
  it('imports neither i18next nor react-i18next anywhere under src/admin', () => {
    const files = Object.keys(sources)
    expect(files.length).toBeGreaterThan(5)
    for (const [file, text] of Object.entries(sources)) {
      expect(text, file).not.toMatch(/from\s+['"](react-)?i18next['"]|require\(\s*['"](react-)?i18next/)
    }
  })
})
