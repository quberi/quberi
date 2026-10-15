/**
 * Shared, plain-language description for a model id (i18n keys under
 * `modelSettings.modelDesc`). Curated flagships first, then heuristics on the
 * id (reasoning / code / vision / size / speed tier).
 *
 * Used by both the model settings panel (add-model list) and the in-chat
 * Kiro-style model picker so descriptions stay consistent.
 */
export function describeModel(id: string, t: (k: string) => string): string {
  const s = id.toLowerCase()
  const d = (k: string) => t(`modelSettings.modelDesc.${k}`)
  const dated = /-\d{4}-\d{2}-\d{2}$|-\d{8}$|-\d{4}$|-snapshot/.test(s)
  // Variant / version qualifiers first, so e.g. the three qwen-flash*
  // entries read differently instead of all saying the same thing.
  if (/character|roleplay|role-play|persona/.test(s)) {
    return d('character') + (dated ? d('snapshotSuffix') : '')
  }
  if (/coder|codeqwen|code-/.test(s)) {
    return d('code') + (dated ? d('snapshotSuffix') : '')
  }
  if (/qvq|-vl\b|-vl-|vision|omni|multimodal|audio/.test(s)) {
    return d('vision') + (dated ? d('snapshotSuffix') : '')
  }
  if (/(^|[-_/])r1|reasoner|qwq|thinking|-think/.test(s)) {
    return d('reasoning') + (dated ? d('snapshotSuffix') : '')
  }
  if (dated) return d('snapshot')
  if (/-latest$/.test(s)) return d('latest')
  // Flagship aliases (base, undated).
  if (s === 'deepseek-chat' || s.endsWith('/deepseek-chat')) return d('deepseekChat')
  if (s.includes('deepseek-reasoner')) return d('deepseekReasoner')
  if (s.startsWith('qwen-max')) return d('qwenMax')
  if (s.startsWith('qwen-plus')) return d('qwenPlus')
  if (s.startsWith('qwen-turbo')) return d('qwenTurbo')
  if (s.startsWith('qwen-flash')) return d('qwenFlash')
  if (s.startsWith('qwen-long')) return d('long')
  if (s === 'gpt-4o') return d('gpt4o')
  if (s === 'gpt-4o-mini') return d('gpt4oMini')
  if (/distill|[-_.](0\.5b|1\.5b|1\.8b|2b|3b|4b|7b|8b)/.test(s)) return d('small')
  // Speed / capability tiers.
  if (/max/.test(s)) return d('tierMax')
  if (/plus/.test(s)) return d('tierPlus')
  if (/turbo/.test(s)) return d('tierTurbo')
  if (/flash|lite|mini|air/.test(s)) return d('tierFlash')
  if (/long|128k|256k|1m/.test(s)) return d('long')
  return d('generic')
}
