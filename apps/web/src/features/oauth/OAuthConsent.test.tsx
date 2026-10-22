import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { OAuthConsent } from './OAuthConsent'
import type { ConsentView } from './api'
import { clientIcon } from './clientIcons'
import { deniedAddress, isConsentRoute, readAuthorizeParams } from './params'

const getConsent = vi.fn()
const authorize = vi.fn()
const enable = vi.fn()

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) =>
      opts?.client ? `${key}:${opts.client}` : key,
    i18n: { resolvedLanguage: 'en-US', changeLanguage: () => undefined },
  }),
}))
const auth = { credentials: { sessionToken: 't' } as unknown, isGuest: false }
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('../auth/SignIn', () => ({ SignIn: () => <div>sign-in-page</div> }))
vi.mock('./api', () => ({
  getConsent: (...a: unknown[]) => getConsent(...a),
  authorize: (...a: unknown[]) => authorize(...a),
  enableExternalTools: (...a: unknown[]) => enable(...a),
}))

const SEARCH =
  '?client_id=quberi-harness&redirect_uri=http%3A%2F%2F127.0.0.1%3A53682%2Fcallback' +
  '&state=abc&code_challenge=CH&code_challenge_method=S256&response_type=code'

function view(over: Partial<ConsentView> = {}): ConsentView {
  return {
    client_id: 'quberi-harness',
    client_name: 'DeepSeek Harness',
    scope: 'knowledge:read',
    redirect_uri: 'http://127.0.0.1:53682/callback',
    organizations: [
      {
        organization_id: 'o-team',
        name: 'Team',
        tier: 'team',
        is_current: false,
        external_tools_enabled: true,
        can_enable: false,
      },
      {
        organization_id: 'o-me',
        name: 'Me',
        tier: 'personal',
        is_current: true,
        external_tools_enabled: true,
        can_enable: false,
      },
    ],
    ...over,
  }
}

describe('address helpers', () => {
  it('recognises the consent page and reads what the tool asked for', () => {
    expect(isConsentRoute('/oauth/authorize')).toBe(true)
    expect(isConsentRoute('/oauth/authorize/')).toBe(true)
    expect(isConsentRoute('/admin')).toBe(false)
    expect(readAuthorizeParams(SEARCH)).toEqual({
      clientId: 'quberi-harness',
      redirectUri: 'http://127.0.0.1:53682/callback',
      scope: '',
      state: 'abc',
      codeChallenge: 'CH',
      codeChallengeMethod: 'S256',
    })
  })

  it('refuses a request that lacks the client, address or challenge', () => {
    for (const q of [
      '?redirect_uri=x&code_challenge=c',
      '?client_id=a&code_challenge=c',
      '?client_id=a&redirect_uri=x',
      '?client_id=a&redirect_uri=x&code_challenge=c&response_type=token',
      '',
    ]) {
      expect(readAuthorizeParams(q)).toBeNull()
    }
  })

  it('builds the "no" address with the error and the tool\'s own state', () => {
    expect(deniedAddress('http://127.0.0.1:1/callback', 's 1')).toBe(
      'http://127.0.0.1:1/callback?error=access_denied&state=s+1',
    )
    expect(deniedAddress('http://127.0.0.1:1/callback', '')).toBe(
      'http://127.0.0.1:1/callback?error=access_denied',
    )
  })
})

describe('which tool is asking', () => {
  it('knows the mark of a registered tool, and nothing for an unknown one', () => {
    expect(clientIcon('quberi-harness')).toBe('/clients/deepseek-harness.png')
    for (const id of ['unknown', '', '__proto__', 'constructor', 'toString']) {
      expect(clientIcon(id), id).toBeUndefined()
    }
  })

  it('shows the tool\'s own mark on the consent page', async () => {
    getConsent.mockResolvedValue(view())
    const { container } = render(<OAuthConsent search={SEARCH} navigate={vi.fn()} />)
    await screen.findByText('oauth.title:DeepSeek Harness')
    const tool = container.querySelector('.oauth-tile-tool')
    expect(tool?.classList.contains('has-image')).toBe(true)
    expect(tool?.querySelector('img')?.getAttribute('src')).toBe('/clients/deepseek-harness.png')
  })

  it('falls back to a generic icon for a tool without a mark', async () => {
    getConsent.mockResolvedValue(view({ client_id: 'some-new-tool', client_name: 'Some Tool' }))
    const { container } = render(<OAuthConsent search={SEARCH} navigate={vi.fn()} />)
    await screen.findByText('oauth.title:Some Tool')
    const tool = container.querySelector('.oauth-tile-tool')
    expect(tool?.classList.contains('has-image')).toBe(false)
    expect(tool?.querySelector('img')).toBeNull()
    expect(tool?.querySelector('svg')).not.toBeNull()
  })
})

describe('OAuthConsent', () => {
  beforeEach(() => {
    getConsent.mockReset()
    authorize.mockReset()
    enable.mockReset()
    getConsent.mockResolvedValue(view())
    authorize.mockResolvedValue({ redirect_to: 'http://127.0.0.1:53682/callback?code=C&state=abc' })
    auth.credentials = { sessionToken: 't' }
    auth.isGuest = false
  })

  it('shows an error and asks the server nothing when the request is incomplete', () => {
    render(<OAuthConsent search="?client_id=x" navigate={vi.fn()} />)
    expect(screen.getByText('oauth.invalid.body')).toBeInTheDocument()
    expect(getConsent).not.toHaveBeenCalled()
  })

  it('asks a guest or signed-out visitor to sign in first', () => {
    auth.isGuest = true
    const { unmount } = render(<OAuthConsent search={SEARCH} navigate={vi.fn()} />)
    expect(screen.getByText('sign-in-page')).toBeInTheDocument()
    expect(getConsent).not.toHaveBeenCalled()
    unmount()
    auth.isGuest = false
    auth.credentials = null
    render(<OAuthConsent search={SEARCH} navigate={vi.fn()} />)
    expect(screen.getByText('sign-in-page')).toBeInTheDocument()
  })

  it('preselects the current organization and sends the browser where the server says', async () => {
    const navigate = vi.fn()
    render(<OAuthConsent search={SEARCH} navigate={navigate} />)
    await screen.findByText('oauth.title:DeepSeek Harness')
    // What leaves Quberi is stated on the page, not buried.
    expect(screen.getByText('oauth.leaves:DeepSeek Harness')).toBeInTheDocument()

    fireEvent.click(screen.getByText('oauth.allow'))
    await waitFor(() => expect(navigate).toHaveBeenCalledTimes(1))
    const [, params, orgId] = authorize.mock.calls[0]
    expect(orgId).toBe('o-me')
    expect(params.codeChallenge).toBe('CH')
    expect(params.state).toBe('abc')
    expect(navigate).toHaveBeenCalledWith('http://127.0.0.1:53682/callback?code=C&state=abc')
  })

  it('lets the person pick another organization', async () => {
    render(<OAuthConsent search={SEARCH} navigate={vi.fn()} />)
    await screen.findByText('oauth.title:DeepSeek Harness')
    fireEvent.click(screen.getByLabelText('Team'))
    fireEvent.click(screen.getByText('oauth.allow'))
    await waitFor(() => expect(authorize).toHaveBeenCalled())
    expect(authorize.mock.calls[0][2]).toBe('o-team')
  })

  it('"cancel" sends access_denied to the address the SERVER validated', async () => {
    const navigate = vi.fn()
    getConsent.mockResolvedValue(view({ redirect_uri: 'http://127.0.0.1:53682/callback' }))
    // The page address names a different return address; it must not be used.
    const forged =
      SEARCH.replace('127.0.0.1%3A53682', 'evil.example') + '&x=1'
    render(<OAuthConsent search={forged} navigate={navigate} />)
    await screen.findByText('oauth.title:DeepSeek Harness')
    fireEvent.click(screen.getByText('oauth.cancel'))
    expect(navigate).toHaveBeenCalledWith(
      'http://127.0.0.1:53682/callback?error=access_denied&state=abc',
    )
    expect(authorize).not.toHaveBeenCalled()
  })

  it('cannot allow an organization that has outside tools off', async () => {
    getConsent.mockResolvedValue(
      view({
        organizations: [
          {
            organization_id: 'o-off',
            name: 'Off',
            tier: 'team',
            is_current: true,
            external_tools_enabled: false,
            can_enable: false,
          },
        ],
      }),
    )
    render(<OAuthConsent search={SEARCH} navigate={vi.fn()} />)
    await screen.findByText('oauth.offAskAdmin')
    expect(screen.getByText('oauth.allow')).toBeDisabled()
    expect(screen.queryByText('oauth.turnOn')).toBeNull()
    fireEvent.click(screen.getByText('oauth.allow'))
    expect(authorize).not.toHaveBeenCalled()
  })

  it('lets an owner turn outside tools on, then continue', async () => {
    const off = view({
      organizations: [
        {
          organization_id: 'o-off',
          name: 'Off',
          tier: 'team',
          is_current: true,
          external_tools_enabled: false,
          can_enable: true,
        },
      ],
    })
    const on = view({
      organizations: [{ ...off.organizations[0], external_tools_enabled: true, can_enable: false }],
    })
    getConsent.mockResolvedValueOnce(off).mockResolvedValue(on)
    enable.mockResolvedValue(undefined)
    render(<OAuthConsent search={SEARCH} navigate={vi.fn()} />)
    await screen.findByText('oauth.offCanEnable')
    expect(screen.getByText('oauth.allow')).toBeDisabled()

    fireEvent.click(screen.getByText('oauth.turnOn'))
    await waitFor(() => expect(enable).toHaveBeenCalledWith(auth.credentials, 'o-off'))
    await waitFor(() => expect(screen.getByText('oauth.allow')).toBeEnabled())
  })

  it('shows the server\'s reason and sends the browser nowhere when approval fails', async () => {
    const navigate = vi.fn()
    authorize.mockRejectedValue(
      Object.assign(new Error('x'), { detail: 'this organization does not allow outside tools' }),
    )
    render(<OAuthConsent search={SEARCH} navigate={navigate} />)
    await screen.findByText('oauth.title:DeepSeek Harness')
    fireEvent.click(screen.getByText('oauth.allow'))
    await waitFor(() => expect(authorize).toHaveBeenCalled())
    expect(navigate).not.toHaveBeenCalled()
    expect(screen.getByText('oauth.allow')).toBeEnabled()
  })
})

describe('asking the server twice', () => {
  beforeEach(() => {
    getConsent.mockReset()
    auth.credentials = { sessionToken: 't' }
  })

  it('keeps the good answer when an older, slower question times out afterwards', async () => {
    let failFirst: (reason: unknown) => void = () => undefined
    getConsent
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { failFirst = reject }))
      .mockResolvedValueOnce(view())
    const { rerender } = render(<OAuthConsent navigate={() => undefined} search={SEARCH} />)
    // The credentials object is replaced (the profile loaded), so the page asks again.
    auth.credentials = { sessionToken: 't', displayName: 'Ada' }
    rerender(<OAuthConsent navigate={() => undefined} search={SEARCH} />)
    await waitFor(() => expect(screen.getAllByText('DeepSeek Harness', { exact: false }).length).toBeGreaterThan(0))

    failFirst(new DOMException('signal is aborted without reason', 'AbortError'))
    await new Promise((r) => setTimeout(r, 20))

    expect(screen.getAllByText('DeepSeek Harness', { exact: false }).length).toBeGreaterThan(0)
    expect(screen.queryByText(/aborted/)).toBeNull()
  })

  it('shows the clear error from the newest question, not a timeout from an older one', async () => {
    let failFirst: (reason: unknown) => void = () => undefined
    const { ApiError } = await import('../api/client')
    getConsent
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { failFirst = reject }))
      .mockRejectedValueOnce(new ApiError(400, 'unknown application'))
    const { rerender } = render(<OAuthConsent navigate={() => undefined} search={SEARCH} />)
    auth.credentials = { sessionToken: 't', displayName: 'Ada' }
    rerender(<OAuthConsent navigate={() => undefined} search={SEARCH} />)
    await waitFor(() => expect(screen.getByText('unknown application')).toBeTruthy())

    failFirst(new DOMException('signal is aborted without reason', 'AbortError'))
    await new Promise((r) => setTimeout(r, 20))

    expect(screen.getByText('unknown application')).toBeTruthy()
    expect(screen.queryByText(/aborted/)).toBeNull()
  })
})
