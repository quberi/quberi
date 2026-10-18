import { beforeEach, describe, expect, it } from 'vitest'

import { clearCredentials, loadCredentials, saveCredentials } from './storage'
import type { DevCredentials } from './types'

const session: DevCredentials = {
  adminToken: '',
  organizationSlug: 'team-20fa5e',
  organizationName: '衡准精密制造',
  userEmail: 'jiangwei@quberi.com',
  sessionToken: 'tok',
  displayName: '蒋伟',
  tier: 'team',
  isGuest: false,
  orgRole: 'member',
}

describe('saved sign-in', () => {
  beforeEach(() => window.localStorage.clear())

  it('comes back whole after a reload, including the organisation name', () => {
    saveCredentials(session)
    expect(loadCredentials()).toEqual(session)
  })

  it('still loads a sign-in saved by an older build that had no name', () => {
    const { organizationName: _name, ...older } = session
    window.localStorage.setItem('quberi.dev.credentials', JSON.stringify(older))
    const loaded = loadCredentials()
    expect(loaded?.sessionToken).toBe('tok')
    expect(loaded?.organizationName).toBeUndefined()
    expect(loaded?.organizationSlug).toBe('team-20fa5e')
  })

  it('returns nothing after sign-out or for damaged data', () => {
    saveCredentials(session)
    clearCredentials()
    expect(loadCredentials()).toBeNull()
    window.localStorage.setItem('quberi.dev.credentials', '{not json')
    expect(loadCredentials()).toBeNull()
  })
})
