import { describe, expect, it } from 'vitest'

import { htmlLangFor, pickLocale } from './locale'

describe('pickLocale', () => {
  it('follows a Chinese browser when nothing was chosen', () => {
    expect(pickLocale(null, ['zh-CN', 'en-US'])).toBe('zh-CN')
    expect(pickLocale(null, ['zh'])).toBe('zh-CN')
    expect(pickLocale(null, ['zh-TW'])).toBe('zh-CN')
  })

  it('follows an English browser', () => {
    expect(pickLocale(null, ['en-GB'])).toBe('en-US')
    expect(pickLocale(null, ['en-US', 'zh-CN'])).toBe('en-US')
  })

  it('uses the first language that has a translation', () => {
    expect(pickLocale(null, ['fr-FR', 'zh-CN'])).toBe('zh-CN')
    expect(pickLocale(null, ['fr-FR', 'en-US', 'zh-CN'])).toBe('en-US')
  })

  it('falls back to English for unsupported or missing languages', () => {
    expect(pickLocale(null, ['fr-FR', 'de'])).toBe('en-US')
    expect(pickLocale(null, [])).toBe('en-US')
    expect(pickLocale(null, [''])).toBe('en-US')
  })

  it('lets an explicit choice win over the browser', () => {
    expect(pickLocale('en-US', ['zh-CN'])).toBe('en-US')
    expect(pickLocale('zh-CN', ['en-US'])).toBe('zh-CN')
  })

  it('ignores a stored value that is not a supported locale', () => {
    expect(pickLocale('fr-FR', ['zh-CN'])).toBe('zh-CN')
    expect(pickLocale('', ['en-US'])).toBe('en-US')
  })
})

describe('htmlLangFor', () => {
  it('maps the app locale to an html lang value', () => {
    expect(htmlLangFor('zh-CN')).toBe('zh-CN')
    expect(htmlLangFor('en-US')).toBe('en')
  })
})
