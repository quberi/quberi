import i18next from 'i18next'
import { describe, expect, it } from 'vitest'

import enUS from '../../i18n/locales/en-US.json'
import zhCN from '../../i18n/locales/zh-CN.json'

async function translator(lng: 'en-US' | 'zh-CN') {
  const i = i18next.createInstance()
  await i.init({
    lng,
    fallbackLng: false,
    resources: { 'en-US': { translation: enUS }, 'zh-CN': { translation: zhCN } },
    interpolation: { escapeValue: false },
  })
  return i.t.bind(i)
}

describe('Overview wording', () => {
  it('says "1 upload is" and "2 uploads are", not "1 uploads are"', async () => {
    const t = await translator('en-US')
    expect(t('overview.attention.pending_reviews', { count: 1 })).toBe('1 upload is waiting for review')
    expect(t('overview.attention.pending_reviews', { count: 2 })).toBe('2 uploads are waiting for review')
    expect(t('overview.attention.org_wide_spaces', { count: 1, names: ': Policies' })).toBe(
      '1 space is open to the whole organisation: Policies',
    )
    expect(t('overview.attention.suspended_owning_spaces', { count: 1, names: '' })).toBe(
      '1 deactivated member still owns spaces nobody has taken over',
    )
    expect(t('overview.stat.externalCalls', { count: 1 })).toBe('1 call in the last 7 days')
    expect(t('spaceAccess.summary.noExceptions', { count: 1 })).toBe('1 document, no exception rules')
  })

  it('reads the same for any count in Chinese', async () => {
    const t = await translator('zh-CN')
    expect(t('overview.attention.pending_reviews', { count: 1 })).toBe('1 份上传在等待审核')
    expect(t('overview.attention.pending_reviews', { count: 5 })).toBe('5 份上传在等待审核')
  })

  it('has a specific button label for every kind of attention item, in both languages', async () => {
    const codes = ['pending_reviews', 'access_requests', 'index_failed', 'suspended_owning_spaces', 'org_wide_spaces']
    for (const lng of ['en-US', 'zh-CN'] as const) {
      const t = await translator(lng)
      const labels = codes.map((c) => t(`overview.attention.go_${c}`, { defaultValue: '' }))
      expect(labels.every((l) => l.length > 0)).toBe(true)
      expect(new Set(labels).size).toBe(codes.length) // no two buttons say the same thing
    }
  })
})
