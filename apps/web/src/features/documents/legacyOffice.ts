/**
 * Older Office files cannot be read for search, so they are turned away before they are sent,
 * with what to do instead. (The server refuses them too; this just saves the round trip.)
 */
const LEGACY: Record<string, { product: string; modern: string }> = {
  '.xls': { product: 'Excel', modern: '.xlsx' },
  '.doc': { product: 'Word', modern: '.docx' },
  '.ppt': { product: 'PowerPoint', modern: '.pptx' },
}

export interface LegacyOffice {
  ext: string
  product: string
  modern: string
}

export function legacyOffice(fileName: string): LegacyOffice | null {
  const lower = fileName.toLowerCase()
  const ext = Object.keys(LEGACY).find((e) => lower.endsWith(e))
  return ext ? { ext, ...LEGACY[ext] } : null
}
