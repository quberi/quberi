import { describe, expect, it } from 'vitest'

import { isReadOnlySpace } from './permissions'

describe('isReadOnlySpace', () => {
  it('is true for a viewer', () => {
    expect(isReadOnlySpace({ caller_role: 'viewer' }, false)).toBe(true)
  })

  it('is false for everyone who can change documents', () => {
    for (const role of ['owner', 'admin', 'editor']) {
      expect(isReadOnlySpace({ caller_role: role }, false)).toBe(false)
    }
  })

  it('is true for a space where only some documents were shared', () => {
    expect(isReadOnlySpace({ caller_role: null, shared_only_documents: 2 }, false)).toBe(true)
  })

  it('does not guess when the role is unknown, and leaves guests to the sandbox rules', () => {
    expect(isReadOnlySpace({ caller_role: undefined }, false)).toBe(false)
    expect(isReadOnlySpace({ caller_role: null }, false)).toBe(false)
    expect(isReadOnlySpace(null, false)).toBe(false)
    expect(isReadOnlySpace({ caller_role: 'viewer' }, true)).toBe(false)
  })
})
