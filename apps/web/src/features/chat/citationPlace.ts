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

/**
 * Where a cited passage is in its file and which version it comes from, in the words the card
 * shows. A citation saved before pages and versions were recorded has none of these fields and
 * reads as it always did ("chunk #N", no version).
 */
export function describeCitation(c: MessageCitation, t: Translate): CitationPlace {
  const start = c.page_start
  let place: string
  if (start == null) {
    place = t('chat.citations.chunk', { index: c.chunk_index })
  } else if (c.page_end == null || c.page_end === start) {
    place = t('chat.citations.page', { page: start })
  } else {
    place = t('chat.citations.pages', { from: start, to: c.page_end })
  }
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
