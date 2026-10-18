import { describe, expect, it } from 'vitest'
import { legacyOffice } from './legacyOffice'

describe('legacyOffice', () => {
  it('names the modern format to save as', () => {
    expect(legacyOffice('报价.XLS')).toEqual({ ext: '.xls', product: 'Excel', modern: '.xlsx' })
    expect(legacyOffice('a.doc')).toEqual({ ext: '.doc', product: 'Word', modern: '.docx' })
    expect(legacyOffice('a.ppt')).toEqual({ ext: '.ppt', product: 'PowerPoint', modern: '.pptx' })
  })

  it('lets current formats through, including names that merely contain the old ones', () => {
    for (const name of ['a.xlsx', 'a.docx', 'a.pptx', 'a.csv', 'notes.xls.txt', 'xls']) {
      expect(legacyOffice(name)).toBeNull()
    }
  })
})
