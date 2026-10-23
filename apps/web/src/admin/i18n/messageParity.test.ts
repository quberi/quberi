import { describe, expect, it } from 'vitest'

// The backend's message catalog, read as text. Each entry is one `"admin.x.y": Message(`
// line, so a regular expression is enough to list the codes.
import catalogSource from '../../../../api/src/quberi_api/services/admin_messages.py?raw'

import { en } from './en'
import { zh } from './zh'

const CODE_LINE = /^\s+"(admin\.[a-z_.]+)":\s*Message\(/gm
const catalogCodes = [...catalogSource.matchAll(CODE_LINE)].map((m) => m[1])

describe('backend message catalog vs the console dictionaries', () => {
  it('finds the catalog codes', () => {
    expect(catalogCodes.length).toBeGreaterThan(30)
    expect(new Set(catalogCodes).size).toBe(catalogCodes.length)
  })

  it.each(catalogCodes)('has msg.%s in English and Chinese', (code) => {
    expect(Object.keys(en)).toContain(`msg.${code}`)
    expect(Object.keys(zh)).toContain(`msg.${code}`)
  })

  it('has no msg.* entry without a catalog code', () => {
    const known = new Set(catalogCodes.map((code) => `msg.${code}`))
    const extra = Object.keys(en).filter((key) => key.startsWith('msg.') && !known.has(key))
    expect(extra).toEqual([])
  })

  it('uses the catalog\'s {param} names', () => {
    // One block per entry: from its code line to the next one.
    const blocks = catalogSource.split(/\n(?=\s+"admin\.[a-z_.]+":\s*Message\()/).slice(1)
    for (const block of blocks) {
      const code = /"(admin\.[a-z_.]+)"/.exec(block)?.[1] as string
      const enText = /en=\(?\s*((?:"(?:[^"\\]|\\.)*"\s*)+)/.exec(block)?.[1] ?? ''
      const names = [...enText.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
      const dictText = en[`msg.${code}` as keyof typeof en] as string
      const dictNames = [...dictText.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
      expect([...new Set(dictNames)], code).toEqual([...new Set(names)])
    }
  })
})
