import { describe, expect, it, vi } from 'vitest'

import { trimPreview } from './ChatPanel'

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }))

describe('trimPreview', () => {
  it('drops heading marks, quote marks and bold markers from a Markdown passage', () => {
    const text = '# 工程变更通知 ECN-2026-014\n\n> **客户机密：仅限星驰汽车项目组可见**\n> 文件编号：PRJ-XC-ECN-014\n\n## 一、变更内容'
    expect(trimPreview(text)).toBe('工程变更通知 ECN-2026-014 客户机密：仅限星驰汽车项目组可见 文件编号：PRJ-XC-ECN-014 一、变更内容')
  })

  it('keeps a > or # that is part of a sentence', () => {
    expect(trimPreview('当 a > b 时报警，编号 #12')).toBe('当 a > b 时报警，编号 #12')
  })

  it('keeps table pipes and numbers as they are', () => {
    expect(trimPreview('| 安装孔孔距 | 240 ± 0.1 mm |')).toBe('| 安装孔孔距 | 240 ± 0.1 mm |')
  })

  it('shortens a long passage and says so', () => {
    const out = trimPreview('字'.repeat(400))
    expect(out.endsWith('…')).toBe(true)
    expect(out.length).toBe(221)
  })

  it('leaves a short plain passage alone', () => {
    expect(trimPreview('  圆度 ≤0.008 mm  ')).toBe('圆度 ≤0.008 mm')
  })
})
