import { describe, expect, it } from 'vitest'
import { isAssignable } from './useSubjectCandidates'

describe('who can be picked to be given access', () => {
  it('leaves out deactivated and archived accounts', () => {
    expect(isAssignable({ status: 'active' })).toBe(true)
    expect(isAssignable({ status: 'invited' })).toBe(true)
    expect(isAssignable({ status: 'suspended' })).toBe(false)
    expect(isAssignable({ status: 'archived' })).toBe(false)
  })
})
