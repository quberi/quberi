import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdminApiError } from './api'

const api = vi.hoisted(() => ({
  adminLogin: vi.fn(),
  changeAdminPassword: vi.fn(),
  verifyToken: vi.fn(),
  getOverview: vi.fn(),
  getOrganizations: vi.fn(),
  getModelCatalog: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const real = await importOriginal<typeof import('./api')>()
  return { ...real, ...api }
})
// The dashboard views are not under test here.
vi.mock('./OverviewView', () => ({ OverviewView: () => <div>overview-view</div> }))
vi.mock('./OrganizationsView', () => ({ OrganizationsView: () => <div>orgs-view</div> }))
vi.mock('./ModelsView', () => ({ ModelsView: () => <div>models-view</div> }))

import { AdminApp } from './AdminApp'

const SESSION = { token: 'sess-1', expires_at: 9999999999, username: 'admin' }

beforeEach(() => {
  window.sessionStorage.clear()
  Object.values(api).forEach((f) => f.mockReset())
  api.getOverview.mockResolvedValue({ totals: {} })
  api.getOrganizations.mockResolvedValue({ items: [] })
  api.getModelCatalog.mockResolvedValue([])
})
afterEach(() => window.sessionStorage.clear())

function fillSignIn(username: string, password: string) {
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: username } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
}

describe('AdminApp sign-in', () => {
  it('asks for a username and password by default, not a token', () => {
    render(<AdminApp />)
    expect(screen.getByLabelText('Username')).toBeTruthy()
    expect(screen.getByLabelText('Password')).toBeTruthy()
    expect(screen.queryByPlaceholderText('Platform admin token')).toBeNull()
  })

  it('signs in and opens the console, keeping the session for this tab', async () => {
    api.adminLogin.mockResolvedValue({ ...SESSION, must_change_password: false })
    render(<AdminApp />)
    fillSignIn('admin', 'a long enough password')
    await waitFor(() => expect(screen.getByText('overview-view')).toBeTruthy())
    expect(api.adminLogin).toHaveBeenCalledWith('admin', 'a long enough password')
    expect(window.sessionStorage.getItem('quberi.admin.token')).toBe('sess-1')
  })

  it('says only "invalid username or password" on a 401', async () => {
    api.adminLogin.mockRejectedValue(new AdminApiError(401, 'invalid username or password'))
    render(<AdminApp />)
    fillSignIn('admin', 'wrong')
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('Invalid username or password.')
    expect(window.sessionStorage.getItem('quberi.admin.token')).toBeNull()
  })

  it('tells the person to wait when the account is locked (429)', async () => {
    api.adminLogin.mockRejectedValue(new AdminApiError(429, 'too many attempts'))
    render(<AdminApp />)
    fillSignIn('admin', 'whatever')
    expect((await screen.findByRole('alert')).textContent).toMatch(/too many attempts/i)
  })

  it('reports an unreachable API rather than blaming the password', async () => {
    api.adminLogin.mockRejectedValue(new TypeError('network down'))
    render(<AdminApp />)
    fillSignIn('admin', 'whatever')
    expect((await screen.findByRole('alert')).textContent).toMatch(/could not reach/i)
  })

  it('does not submit with an empty username or password', () => {
    render(<AdminApp />)
    const submit = screen.getByRole('button', { name: 'Sign in' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } })
    expect(submit.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'x' } })
    expect(submit.disabled).toBe(false)
  })
})

describe('AdminApp token fallback', () => {
  it('still lets an operator paste the deployment token', async () => {
    api.verifyToken.mockResolvedValue({})
    render(<AdminApp />)
    fireEvent.click(screen.getByRole('button', { name: 'Use an admin token instead' }))
    expect(screen.queryByLabelText('Username')).toBeNull()
    fireEvent.change(screen.getByLabelText('Platform admin token'), {
      target: { value: 'the-deployment-token' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByText('overview-view')).toBeTruthy())
    expect(api.verifyToken).toHaveBeenCalledWith('the-deployment-token')
    expect(api.adminLogin).not.toHaveBeenCalled()
  })

  it('shows a token-specific error and clears the field when switching back', async () => {
    api.verifyToken.mockRejectedValue(new AdminApiError(401, 'invalid platform admin token'))
    render(<AdminApp />)
    fireEvent.click(screen.getByRole('button', { name: 'Use an admin token instead' }))
    fireEvent.change(screen.getByLabelText('Platform admin token'), { target: { value: 'nope' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Invalid admin token.')
    fireEvent.click(screen.getByRole('button', { name: 'Use a username and password' }))
    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('AdminApp first-boot password change', () => {
  async function signInMustChange() {
    api.adminLogin.mockResolvedValue({ ...SESSION, must_change_password: true })
    render(<AdminApp />)
    fillSignIn('admin', 'generated-once-in-the-log')
    await screen.findByRole('heading', { name: 'Choose a new password' })
  }

  it('shows nothing of the console until the password is changed', async () => {
    await signInMustChange()
    expect(screen.queryByText('overview-view')).toBeNull()
    expect(api.getOverview).not.toHaveBeenCalled()
  })

  it('keeps showing the change form after a reload', async () => {
    await signInMustChange()
    // A new render reads the same tab storage.
    document.body.innerHTML = ''
    render(<AdminApp />)
    expect(await screen.findByRole('heading', { name: 'Choose a new password' })).toBeTruthy()
  })

  it('refuses mismatched passwords without calling the server', async () => {
    await signInMustChange()
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old' } })
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'first choice here' } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'different choice' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/do not match/i)
    expect(api.changeAdminPassword).not.toHaveBeenCalled()
  })

  it('opens the console with the fresh session after a successful change', async () => {
    await signInMustChange()
    api.changeAdminPassword.mockResolvedValue({
      ...SESSION,
      token: 'sess-2',
      must_change_password: false,
    })
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old' } })
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'my own passphrase' } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'my own passphrase' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    await waitFor(() => expect(screen.getByText('overview-view')).toBeTruthy())
    expect(api.changeAdminPassword).toHaveBeenCalledWith('sess-1', 'old', 'my own passphrase')
    expect(window.sessionStorage.getItem('quberi.admin.token')).toBe('sess-2')
    expect(window.sessionStorage.getItem('quberi.admin.mustChange')).toBeNull()
  })

  it("shows the server's reason when the new password is rejected", async () => {
    await signInMustChange()
    api.changeAdminPassword.mockRejectedValue(
      new AdminApiError(400, 'password must be at least 12 characters'),
    )
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old' } })
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'short' } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: 'short' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/at least 12/)
    // still gated
    expect(screen.queryByText('overview-view')).toBeNull()
  })

  it('goes back to sign-in if the session has expired meanwhile', async () => {
    await signInMustChange()
    api.changeAdminPassword.mockRejectedValue(new AdminApiError(401, 'sign in required'))
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old' } })
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'my own passphrase' } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'my own passphrase' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    await waitFor(() => expect(screen.getByLabelText('Username')).toBeTruthy())
    expect(window.sessionStorage.getItem('quberi.admin.token')).toBeNull()
  })
})

describe('AdminApp session', () => {
  it('returns to sign-in when the console gets a 401 (expired or replaced session)', async () => {
    window.sessionStorage.setItem('quberi.admin.token', 'stale')
    api.getOverview.mockRejectedValue(new AdminApiError(401, 'invalid platform admin token'))
    render(<AdminApp />)
    await waitFor(() => expect(screen.getByLabelText('Username')).toBeTruthy())
    expect(window.sessionStorage.getItem('quberi.admin.token')).toBeNull()
  })
})
