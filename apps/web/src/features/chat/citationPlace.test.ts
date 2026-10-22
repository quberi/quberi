import { describe, expect, it } from 'vitest'

import { describeCitation } from './citationPlace'
import type { MessageCitation } from './types'

// A translator that shows the key and its values, so the test reads the choice made.
const t = (key: string, options?: Record<string, unknown>) =>
  options ? `${key}:${JSON.stringify(options)}` : key

function cite(extra: Partial<MessageCitation> = {}): MessageCitation {
  return {
    index: 1,
    document_id: 'd',
    space_id: 's',
    document_name: 'contract.pdf',
    chunk_index: 5,
    text: 'x',
    score: 0.8,
    used: true,
    char_start: 0,
    char_end: 1,
    ...extra,
  }
}

describe('describeCitation', () => {
  it('reads as it always did for a citation saved before pages and versions', () => {
    expect(describeCitation(cite(), t)).toEqual({
      place: 'chat.citations.chunk:{"index":5}',
      scanned: false,
      described: false,
      version: null,
    })
  })

  it('names one page, or a range when the passage crosses a page break', () => {
    expect(describeCitation(cite({ page_start: 3, page_end: 3 }), t).place).toBe(
      'chat.citations.page:{"page":3}',
    )
    expect(describeCitation(cite({ page_start: 3 }), t).place).toBe(
      'chat.citations.page:{"page":3}',
    )
    expect(describeCitation(cite({ page_start: 2, page_end: 4 }), t).place).toBe(
      'chat.citations.pages:{"from":2,"to":4}',
    )
  })

  it('warns about text read from a picture, whichever way it is recorded', () => {
    expect(describeCitation(cite({ unit_label: 'ocr_block' }), t).scanned).toBe(true)
    expect(describeCitation(cite({ source_kind: 'image_ocr' }), t).scanned).toBe(true)
    expect(describeCitation(cite({ unit_label: 'text_block', source_kind: 'text' }), t).scanned).toBe(
      false,
    )
  })

  it('names a slide, a sheet row, a paragraph under its heading and a line the way each is counted', () => {
    const place = (extra: Partial<MessageCitation>) => describeCitation(cite(extra), t).place
    expect(place({ unit_label: 'slide', page_start: 4, page_end: 4 })).toBe(
      'chat.citations.slide:{"n":4}',
    )
    expect(place({ unit_label: 'slide', page_start: 2, page_end: 5 })).toBe(
      'chat.citations.slides:{"from":2,"to":5}',
    )
    expect(
      place({ unit_label: 'sheet_row', unit_start: 12, unit_end: 12, section: 'Prices' }),
    ).toBe('chat.citations.sheetRow:{"sheet":"Prices","n":12}')
    expect(
      place({ unit_label: 'sheet_row', unit_start: 5, unit_end: 9, section: 'Prices' }),
    ).toBe('chat.citations.sheetRows:{"sheet":"Prices","from":5,"to":9}')
    // A chunk that crossed from one sheet into the next has no single row range.
    expect(
      place({ unit_label: 'sheet_row', unit_start: 5, unit_end: 9, section: 'A / B' }),
    ).toBe('chat.citations.sheetOnly:{"sheet":"A / B"}')
    expect(
      place({ unit_label: 'paragraph', unit_start: 12, unit_end: 12, section: '3.2 Payment' }),
    ).toBe('chat.citations.paragraphUnder:{"section":"3.2 Payment","n":12}')
    expect(place({ unit_label: 'paragraph', unit_start: 12, unit_end: 15 })).toBe(
      'chat.citations.paragraphs:{"section":"","from":12,"to":15}',
    )
    expect(place({ unit_label: 'line', unit_start: 40, unit_end: 52 })).toBe(
      'chat.citations.lines:{"from":40,"to":52}',
    )
  })

  it('falls back to the chunk number when a format recorded nothing', () => {
    for (const label of ['slide', 'sheet_row', 'paragraph', 'line']) {
      expect(describeCitation(cite({ unit_label: label }), t).place).toBe(
        'chat.citations.chunk:{"index":5}',
      )
    }
  })

  it('shows a heading from the document as one clean line', () => {
    const shown = describeCitation(
      cite({ unit_label: 'paragraph', unit_start: 1, unit_end: 1, section: 'Pay\n\n  now\t' }),
      t,
    ).place
    expect(shown).toBe('chat.citations.paragraphUnder:{"section":"Pay now","n":1}')
  })

  it('marks a model description as a guide, not a scan', () => {
    const described = describeCitation(cite({ source_kind: 'image_caption' }), t)
    expect(described.described).toBe(true)
    expect(described.scanned).toBe(false)
    expect(describeCitation(cite({ source_kind: 'image_ocr' }), t).described).toBe(false)
  })

  it('says nothing about versions for a file that has only one', () => {
    expect(describeCitation(cite({ version_number: 1, latest_version: 1 }), t).version).toBeNull()
    expect(describeCitation(cite({ version_number: 1, latest_version: null }), t).version).toBeNull()
  })

  it('marks the newest version and flags an older one as replaced', () => {
    expect(describeCitation(cite({ version_number: 2, latest_version: 2 }), t).version).toEqual({
      text: 'chat.citations.versionLatest:{"number":2}',
      older: false,
    })
    expect(describeCitation(cite({ version_number: 1, latest_version: 3 }), t).version).toEqual({
      text: 'chat.citations.versionOlder:{"number":1,"latest":3}',
      older: true,
    })
  })
})
