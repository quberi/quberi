import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SESSION_INVALID_EVENT } from '../api/client'
import { AuthProvider, useAuth } from './AuthContext'
import { loadCredentials, saveCredentials } from './storage'
import type { DevCredentials } from './types'

const apiMock = vi.hoisted(() => ({ logout: vi.fn(async () => undefined) }))
vi.mock('./api', () => apiMock)

const session: DevCredentials = {
  adminToken: '',
  organizationSlug: 'team',
  userEmail: 'a@example.com',
  sessionToken: 'tok-1',
  displayName: 'A',
  tier: 'team',
  isGuest: false,
  orgRole: 'member',
}

function Probe() {
  const { credentials, notice, signOut, signInAsGuest } = useAuth()
  return (
    <div>
      <span data-testid="who">{credentials?.sessionToken ?? 'nobody'}</span>
      <span data-testid="notice">{notice ?? 'none'}</span>
      <button onClick={signOut}>out</button>
      <button
        onClick={() =>
          signInAsGuest('guest-tok', {
            user_id: 'g',
            email: 'g@example.com',
            display_name: 'Guest',
            organization_id: 'o',
            organization_slug: 'demo',
          })
        }
      >
        guest
      </button>
    </div>
  )
}

describe('AuthProvider and the server ending a session', () => {
  beforeEach(() => {
    window.localStorage.clear()
    apiMock.logout.mockClear()
  })
  afterEach(() => window.localStorage.clear())

  it('signs the person out and leaves a notice when the server says the session ended', () => {
    saveCredentials(session)
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    expect(screen.getByTestId('who')).toHaveTextContent('tok-1')
    act(() => {
      window.dispatchEvent(new Event(SESSION_INVALID_EVENT))
    })
    expect(screen.getByTestId('who')).toHaveTextContent('nobody')
    expect(screen.getByTestId('notice')).toHaveTextContent('sessionInvalid')
    expect(loadCredentials()).toBeNull()
    // The server already ended it: no logout call needed.
    expect(apiMock.logout).not.toHaveBeenCalled()
  })

  it('ignores the signal for a guest session', () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    act(() => screen.getByText('guest').click())
    act(() => {
      window.dispatchEvent(new Event(SESSION_INVALID_EVENT))
    })
    expect(screen.getByTestId('who')).toHaveTextContent('guest-tok')
    expect(screen.getByTestId('notice')).toHaveTextContent('none')
  })

  it('signing out asks the server to end the session as well', () => {
    saveCredentials(session)
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    act(() => screen.getByText('out').click())
    expect(apiMock.logout).toHaveBeenCalledWith('tok-1')
    expect(screen.getByTestId('who')).toHaveTextContent('nobody')
    expect(screen.getByTestId('notice')).toHaveTextContent('none')
    expect(loadCredentials()).toBeNull()
  })

  it('signing out of a guest session does not call the server', () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    act(() => screen.getByText('guest').click())
    act(() => screen.getByText('out').click())
    expect(apiMock.logout).not.toHaveBeenCalled()
  })
})
