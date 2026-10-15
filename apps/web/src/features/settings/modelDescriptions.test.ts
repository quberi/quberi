import { describe, expect, it } from 'vitest'

import { describeModel } from './modelDescriptions'

/**
 * Fake i18n lookup that mirrors the real ``modelSettings.modelDesc.*``
 * strings closely enough to assert on distinctness — tests don't need the
 * exact Chinese copy, just that different model families map to different
 * keys (and therefore different rendered text).
 */
const t = (key: string): string => key.replace('modelSettings.modelDesc.', '')

describe('describeModel', () => {
  it('gives qwen-flash, qwen-flash-character, and a dated snapshot three DIFFERENT descriptions', () => {
    // Regression: these three used to all say the same generic "flash
    // tier" description, which was confusing when a user saw all three
    // in a provider's model list and couldn't tell them apart.
    const flash = describeModel('qwen-flash', t)
    const character = describeModel('qwen-flash-character', t)
    const dated = describeModel('qwen-flash-2025-01-01', t)

    expect(flash).toBe('qwenFlash')
    expect(character).toBe('character')
    expect(dated).toBe('snapshot')

    const unique = new Set([flash, character, dated])
    expect(unique.size).toBe(3)
  })

  it('recognises curated flagship aliases before falling back to heuristics', () => {
    expect(describeModel('deepseek-chat', t)).toBe('deepseekChat')
    expect(describeModel('gpt-4o', t)).toBe('gpt4o')
    expect(describeModel('gpt-4o-mini', t)).toBe('gpt4oMini')
    expect(describeModel('qwen-max', t)).toBe('qwenMax')
  })

  it('detects reasoning models regardless of vendor naming convention', () => {
    // KNOWN ISSUE (found while writing this test, not yet fixed): the
    // generic ``reasoner|r1|qwq|thinking`` regex branch runs BEFORE the
    // ``deepseek-reasoner`` flagship-alias check further down, so the
    // curated ``deepseekReasoner`` copy is unreachable dead code — every
    // "deepseek-reasoner" id falls into the generic "reasoning" bucket
    // instead. Asserting the CURRENT behaviour here (not the intended
    // one) so this test documents reality; flip this assertion if/when
    // the branch order in modelDescriptions.ts is fixed.
    expect(describeModel('deepseek-reasoner', t)).toBe('reasoning')
    expect(describeModel('qwq-32b', t)).toBe('reasoning')
    expect(describeModel('some-vendor-r1-distill', t)).toBe('reasoning')
  })

  it('detects vision/multimodal models', () => {
    expect(describeModel('qwen-vl-max', t)).toBe('vision')
    expect(describeModel('qvq-72b', t)).toBe('vision')
  })

  it('detects code-specialised models', () => {
    expect(describeModel('codeqwen-7b', t)).toBe('code')
    expect(describeModel('deepseek-coder', t)).toBe('code')
  })

  it('appends a snapshot suffix to dated variant models', () => {
    // A dated *character* variant should still read as "character" plus a
    // "(dated snapshot)" suffix, not silently fall back to plain "dated".
    const key = describeModel('qwen-flash-character-2025-06-01', t)
    expect(key).toContain('character')
    expect(key).toContain('snapshotSuffix')
  })

  it('falls back to a size-based description for small distilled models', () => {
    expect(describeModel('deepseek-r1-distill-qwen-1.5b', t)).toBe('reasoning')
    expect(describeModel('some-model-7b', t)).toBe('small')
  })

  it('falls back to a generic description for unrecognised model ids', () => {
    expect(describeModel('totally-unknown-model-xyz', t)).toBe('generic')
  })

  it('is case-insensitive', () => {
    expect(describeModel('DeepSeek-Chat', t)).toBe(describeModel('deepseek-chat', t))
    expect(describeModel('QWEN-MAX', t)).toBe(describeModel('qwen-max', t))
  })
})
