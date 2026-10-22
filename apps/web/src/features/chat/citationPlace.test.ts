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
