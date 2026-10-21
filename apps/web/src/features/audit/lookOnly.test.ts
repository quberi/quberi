import { describe, expect, it } from 'vitest'

import { isLookOnlyAction } from './sentence'

describe('isLookOnlyAction', () => {
  it('treats looking and chatting as look-only', () => {
    for (const a of [
      'document.access_checked',
      'space.documents_listed_for_access',
      'knowledge.external_search',
      'message.sent',
    ]) {
      expect(isLookOnlyAction(a)).toBe(true)
    }
  })

  it('keeps every change visible', () => {
    for (const a of [
      'member.deactivated',
      'space.access_granted',
      'document.access_rule_added',
      'document.uploaded',
      'document.deleted',
      'external_access.granted',
      'knowledge.external_save',
      'knowledge.external_upload',
    ]) {
      expect(isLookOnlyAction(a)).toBe(false)
    }
  })
})
