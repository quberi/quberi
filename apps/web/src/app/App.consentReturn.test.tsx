import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Coming back from a full-page Google sign-in lands on "/#google_login=...".
 * The sign-in module puts the address bar back to the page the person came
 * from; the app must pick its page AFTER that, or the tool's consent page is
 * skipped and the tool waits for an approval that never comes.
 */

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { resolvedLanguage: 'en-US' } }),
}))
// The pages themselves are not under test: just which one is chosen.
vi.mock('../features/oauth/OAuthConsent', () => ({
  OAuthConsent: () => <div>consent-page</div>,
}))
vi.mock('../features/oauth/OAuthConnected', () => ({
  OAuthConnected: () => <div>connected-page</div>,
  isConnectedRoute: (p: string) => p === '/oauth/connected',
}))
vi.mock('../features/auth/SignIn', () => ({ SignIn: () => <div>sign-in-page</div> }))
vi.mock('../admin-console/AdminConsole', () => ({ AdminConsole: () => null }))
vi.mock('../components/layout/AppShell', () => ({ AppShell: () => <div>workspace</div> }))
vi.mock('../features/auth/api', () => ({
  requestGuestSession: () => new Promise(() => undefined),
}))

const CONSENT = '/oauth/authorize?client_id=quberi-harness&state=abc&code_challenge=CH'

function googleReturn(): string {
  const payload = {
    token: 'session-token',
    user: { user_id: 'u1', email: 'me@example.com', organization_slug: 'me', display_name: 'Me' },
  }
  const blob = btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `/#google_login=${blob}`
}

async function loadApp() {
  vi.resetModules() // the sign-in module reads the address once per page load
  const { App } = await import('./App')
  return App
}

beforeEach(() => {
  window.sessionStorage.clear()
  window.localStorage.clear()
})
afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('returning from Google sign-in', () => {
  it('lands on the consent page the person came from, not the workspace', async () => {
    window.sessionStorage.setItem('quberi.postLoginPath', CONSENT)
    window.history.replaceState(null, '', googleReturn())
    const App = await loadApp()
    render(<App />)
    expect(await screen.findByText('consent-page')).toBeInTheDocument()
    expect(screen.queryByText('workspace')).toBeNull()
    // The token is gone from the address bar and the consent request is intact.
    expect(window.location.hash).toBe('')
    expect(window.location.pathname + window.location.search).toBe(CONSENT)
  })

  it('an ordinary Google sign-in still lands on the workspace', async () => {
    window.sessionStorage.setItem('quberi.postLoginPath', '/')
    window.history.replaceState(null, '', googleReturn())
    const App = await loadApp()
    render(<App />)
    expect(await screen.findByText('workspace')).toBeInTheDocument()
    expect(screen.queryByText('consent-page')).toBeNull()
  })

  it('a FAILED Google sign-in also goes back to the consent page, so the error shows there', async () => {
    window.sessionStorage.setItem('quberi.postLoginPath', CONSENT)
    window.history.replaceState(null, '', '/#google_login_error=Google%3A%20access_denied')
    const App = await loadApp()
    render(<App />)
    expect(window.location.pathname + window.location.search).toBe(CONSENT)
    expect(await screen.findByText('consent-page')).toBeInTheDocument()
  })

  it('will not be sent to another site by a tampered remembered path', async () => {
    for (const evil of ['//evil.example/x', 'https://evil.example/', 'javascript:alert(1)']) {
      window.sessionStorage.setItem('quberi.postLoginPath', evil)
      window.history.replaceState(null, '', googleReturn())
      const App = await loadApp()
      render(<App />)
      expect(window.location.origin).toBe('http://localhost:3000')
      expect(window.location.pathname).toBe('/')
    }
  })

  it('a link straight to the result page needs no account', async () => {
    window.history.replaceState(null, '', '/oauth/connected?status=ok')
    const App = await loadApp()
    render(<App />)
    expect(await screen.findByText('connected-page')).toBeInTheDocument()
  })
})
