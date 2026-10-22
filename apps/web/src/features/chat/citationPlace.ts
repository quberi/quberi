import type { MessageCitation } from './types'

type Translate = (key: string, options?: Record<string, unknown>) => string

export interface CitationPlace {
  /** "page 3", "pages 2-4", or "chunk #5" when the file has no pages recorded. */
  place: string
  /** The text was read from a picture of the page, so it may contain errors. */
  scanned: boolean
  /** A model's description of a picture: a guide to what it shows, not a transcript. */
  described: boolean
  /**
   * Which version of the file this is, shown only when the file has more than one.
   * ``older`` is true when a newer version has replaced it.
   */
  version: { text: string; older: boolean } | null
}

const MAX_SECTION_CHARS = 200

/** A heading or sheet name from the document, as one short line of text. */
function cleanSection(section: string | undefined): string {
  const text = (section ?? '').split(/\s+/).filter(Boolean).join(' ')
  return text.length > MAX_SECTION_CHARS ? `${text.slice(0, MAX_SECTION_CHARS - 1)}…` : text
}

/** One number, or two when the passage runs across several (``lo`` is never after ``hi``). */
function span(
  lo: number,
  hi: number | null | undefined,
  one: (n: number) => string,
  many: (from: number, to: number) => string,
): string {
  return hi == null || hi === lo ? one(lo) : many(lo, hi)
}

/**
 * Where the passage is, the way its format counts it (the same rules as the server's
 * ``place`` module): a PDF page, a slide, a sheet and row, a paragraph under a heading, a line.
 * ``null`` when nothing was recorded.
 */
function placeText(c: MessageCitation, t: Translate): string | null {
  const section = cleanSection(c.section)
  const unit = c.unit_start
  switch (c.unit_label) {
    case 'slide':
      return c.page_start == null
        ? null
        : span(
            c.page_start,
            c.page_end,
            (n) => t('chat.citations.slide', { n }),
            (from, to) => t('chat.citations.slides', { from, to }),
          )
    case 'sheet_row': {
      if (unit == null || section.includes(' / ')) {
        return section ? t('chat.citations.sheetOnly', { sheet: section }) : null
      }
      return span(
        unit,
        c.unit_end,
        (n) => t('chat.citations.sheetRow', { sheet: section, n }),
        (from, to) => t('chat.citations.sheetRows', { sheet: section, from, to }),
      )
    }
    case 'paragraph': {
      if (unit == null) return section || null
      const under = section ? 'Under' : ''
      return span(
        unit,
        c.unit_end,
        (n) => t(`chat.citations.paragraph${under}`, { section, n }),
        (from, to) => t(`chat.citations.paragraphs${under}`, { section, from, to }),
      )
    }
    case 'line':
      return unit == null
        ? null
        : span(
            unit,
            c.unit_end,
            (n) => t('chat.citations.line', { n }),
            (from, to) => t('chat.citations.lines', { from, to }),
          )
    default:
      return c.page_start == null
        ? null
        : span(
            c.page_start,
            c.page_end,
            (page) => t('chat.citations.page', { page }),
            (from, to) => t('chat.citations.pages', { from, to }),
          )
  }
}

/**
 * Where a cited passage is in its file and which version it comes from, in the words the card
 * shows. A citation saved before pages and versions were recorded has none of these fields and
 * reads as it always did ("chunk #N", no version).
 */
export function describeCitation(c: MessageCitation, t: Translate): CitationPlace {
  const place = placeText(c, t) ?? t('chat.citations.chunk', { index: c.chunk_index })
  const scanned = c.unit_label === 'ocr_block' || c.source_kind === 'image_ocr'
  const described = c.source_kind === 'image_caption'

  const latest = c.latest_version
  const number = c.version_number ?? 1
  let version: CitationPlace['version'] = null
  if (latest != null && latest > 1) {
    version =
      number >= latest
        ? { text: t('chat.citations.versionLatest', { number }), older: false }
        : { text: t('chat.citations.versionOlder', { number, latest }), older: true }
  }
  return { place, scanned, described, version }
}
