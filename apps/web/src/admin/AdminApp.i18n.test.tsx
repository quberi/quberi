import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdminApiError } from './api'
import { LANG_STORAGE_KEY, setLang } from './i18n'

const api = vi.hoisted(() => ({
  adminLogin: vi.fn(),
  changeAdminPassword: vi.fn(),
  getOverview: vi.fn(),
  getOrganizations: vi.fn(),
  getModelCatalog: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const real = await importOriginal<typeof import('./api')>()
  return { ...real, ...api }
})
// The dashboard views are converted separately; only the shell is under test here.
vi.mock('./OverviewView', () => ({ OverviewView: () => <div>overview-view</div> }))
vi.mock('./OrganizationsView', () => ({ OrganizationsView: () => <div>orgs-view</div> }))
vi.mock('./ModelsView', () => ({ ModelsView: () => <div>models-view</div> }))
vi.mock('./ClientsView', () => ({ ClientsView: () => <div>clients-view</div> }))
vi.mock('./TrafficView', () => ({ TrafficView: () => <div>traffic-view</div> }))

import { AdminApp } from './AdminApp'

const SESSION = { token: 'sess-1', expires_at: 9999999999, username: 'admin' }

beforeEach(() => {
  window.sessionStorage.clear()
  window.localStorage.removeItem(LANG_STORAGE_KEY)
  setLang('en')
  Object.values(api).forEach((f) => f.mockReset())
  api.getOverview.mockResolvedValue({ totals: {} })
  api.getOrganizations.mockResolvedValue({ items: [] })
  api.getModelCatalog.mockResolvedValue([])
})
afterEach(() => {
  window.sessionStorage.clear()
  window.localStorage.removeItem(LANG_STORAGE_KEY)
})

function chooseChinese() {
  fireEvent.click(screen.getByRole('button', { name: '简体中文' }))
}
function chooseEnglish() {
  fireEvent.click(screen.getByRole('button', { name: 'English' }))
}
function fillSignIn(username: string, password: string, labels = ['Username', 'Password']) {
  fireEvent.change(screen.getByLabelText(labels[0]), { target: { value: username } })
  fireEvent.change(screen.getByLabelText(labels[1]), { target: { value: password } })
  fireEvent.submit(screen.getByLabelText(labels[0]).closest('form') as HTMLFormElement)
}

describe('language switch on the sign-in gate', () => {
  it('starts in English with the switch below the card', () => {
    render(<AdminApp />)
    expect(screen.getByRole('heading', { name: 'Quberi Platform Admin' })).toBeTruthy()
    const group = screen.getByRole('group', { name: 'Language' })
    expect(group.closest('.admin-gate-card')).toBeNull()
    expect(screen.getByRole('button', { name: 'English' }).getAttribute('aria-pressed')).toBe('true')
    expect(document.documentElement.lang).toBe('en')
    expect(document.title).toBe('Quberi Platform Admin')
  })

  it('re-renders in Chinese, persists the choice and sets the document language and title', () => {
    render(<AdminApp />)
    chooseChinese()
    expect(screen.getByRole('heading', { name: 'Quberi 平台管理' })).toBeTruthy()
    expect(screen.getByLabelText('用户名')).toBeTruthy()
    expect(screen.getByLabelText('密码')).toBeTruthy()
    expect(screen.getByRole('button', { name: '登录' })).toBeTruthy()
    expect(screen.getByRole('group', { name: '语言' })).toBeTruthy()
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe('zh')
    expect(document.documentElement.lang).toBe('zh-CN')
    expect(document.title).toBe('Quberi 平台管理')
    expect(screen.getByRole('button', { name: '简体中文' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('switches back to English', () => {
    render(<AdminApp />)
    chooseChinese()
    chooseEnglish()
    expect(screen.getByRole('heading', { name: 'Quberi Platform Admin' })).toBeTruthy()
    expect(window.localStorage.getItem(LANG_STORAGE_KEY)).toBe('en')
    expect(document.documentElement.lang).toBe('en')
    expect(document.title).toBe('Quberi Platform Admin')
  })

  it('keeps the choice when the app is rendered again', () => {
    const first = render(<AdminApp />)
    chooseChinese()
    first.unmount()
    render(<AdminApp />)
    expect(screen.getByRole('heading', { name: 'Quberi 平台管理' })).toBeTruthy()
  })
})

describe('sign-in errors', () => {
  it('shows a coded error in the active language and re-translates it on a switch', async () => {
    api.adminLogin.mockRejectedValue(
      new AdminApiError(401, 'invalid username or password', 'admin.auth.bad_credentials'),
    )
    render(<AdminApp />)
    fillSignIn('admin', 'wrong')
    expect((await screen.findByRole('alert')).textContent).toBe('Invalid username or password.')
    chooseChinese()
    expect(screen.getByRole('alert').textContent).toBe('用户名或密码不正确。')
  })

  it('maps status 429 and 401 without a code, and an unreachable API, in Chinese', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, 'zh')
    setLang('zh')
    api.adminLogin.mockRejectedValue(new AdminApiError(429, 'too many attempts'))
    render(<AdminApp />)
    fillSignIn('admin', 'x', ['用户名', '密码'])
    expect((await screen.findByRole('alert')).textContent).toBe('尝试次数过多，请等待几分钟后再试。')

    api.adminLogin.mockRejectedValue(new AdminApiError(401, 'invalid username or password'))
    fireEvent.submit(screen.getByLabelText('用户名').closest('form') as HTMLFormElement)
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toBe('用户名或密码不正确。'),
    )

    api.adminLogin.mockRejectedValue(new TypeError('network down'))
    fireEvent.submit(screen.getByLabelText('用户名').closest('form') as HTMLFormElement)
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toBe('无法连接管理 API，请稍后再试。'),
    )
  })
})

describe('change-password gate', () => {
  async function openGate() {
    api.adminLogin.mockResolvedValue({ ...SESSION, must_change_password: true })
    render(<AdminApp />)
    fillSignIn('admin', 'generated-once-in-the-log')
    await screen.findByRole('heading', { name: 'Choose a new password' })
  }
  function submitChange(labels = ['Current password', 'New password', 'Confirm new password']) {
    fireEvent.change(screen.getByLabelText(labels[0]), { target: { value: 'old' } })
    fireEvent.change(screen.getByLabelText(labels[1]), { target: { value: 'short' } })
    fireEvent.change(screen.getByLabelText(labels[2]), { target: { value: 'short' } })
    fireEvent.submit(screen.getByLabelText(labels[0]).closest('form') as HTMLFormElement)
  }

  it('is translated, with the password limits in the hint, and has the switch', async () => {
    await openGate()
    expect(screen.getByText(/\(12 to 128 characters\)/)).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Language' })).toBeTruthy()
    chooseChinese()
    expect(screen.getByRole('heading', { name: '请设置新密码' })).toBeTruthy()
    expect(screen.getByText(/12 到 128 个字符/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '修改密码' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '退出登录' })).toBeTruthy()
  })

  it('shows a coded rejection with its params in Chinese', async () => {
    await openGate()
    chooseChinese()
    api.changeAdminPassword.mockRejectedValue(
      new AdminApiError(
        400,
        'password must be at least 12 characters',
        'admin.auth.password_too_short',
        { min: 12 },
      ),
    )
    submitChange(['当前密码', '新密码', '确认新密码'])
    expect((await screen.findByRole('alert')).textContent).toBe('密码至少需要 12 个字符。')
  })

  it('falls back to the server detail for an unknown code', async () => {
    await openGate()
    api.changeAdminPassword.mockRejectedValue(
      new AdminApiError(400, 'something only the server knows', 'admin.auth.future_code'),
    )
    submitChange()
    expect((await screen.findByRole('alert')).textContent).toBe('something only the server knows')
  })

  it('does not render [object Object] for a 422', async () => {
    await openGate()
    api.changeAdminPassword.mockRejectedValue(new AdminApiError(422, '', 'validation'))
    submitChange()
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Some of the values entered are not valid.',
    )
  })

  it('shows the network message for a TypeError', async () => {
    await openGate()
    api.changeAdminPassword.mockRejectedValue(new TypeError('Failed to fetch'))
    submitChange()
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Could not reach the admin API. Try again in a moment.',
    )
  })

  it('translates the mismatch message', async () => {
    await openGate()
    chooseChinese()
    fireEvent.change(screen.getByLabelText('当前密码'), { target: { value: 'old' } })
    fireEvent.change(screen.getByLabelText('新密码'), { target: { value: 'one' } })
    fireEvent.change(screen.getByLabelText('确认新密码'), { target: { value: 'two' } })
    fireEvent.submit(screen.getByLabelText('当前密码').closest('form') as HTMLFormElement)
    expect((await screen.findByRole('alert')).textContent).toBe('两次输入的新密码不一致。')
    expect(api.changeAdminPassword).not.toHaveBeenCalled()
  })
})

describe('dashboard shell', () => {
  beforeEach(() => window.sessionStorage.setItem('quberi.admin.token', 'sess-1'))

  it('translates the rail, topbar and brand, with the switch above Sign out', async () => {
    render(<AdminApp />)
    await screen.findByText('overview-view')
    const rail = document.querySelector('.admin-rail') as HTMLElement
    expect(rail.textContent).toContain('Quberi Admin')
    expect(screen.getByRole('button', { name: 'Outside tools' })).toBeTruthy()

    const group = screen.getByRole('group', { name: 'Language' })
    const signOut = screen.getByRole('button', { name: 'Sign out' })
    expect(rail.contains(group)).toBe(true)
    expect(group.compareDocumentPosition(signOut) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    chooseChinese()
    expect(rail.textContent).toContain('Quberi 管理')
    for (const name of ['概览', '组织', '模型', '外部工具', '访客流量', '退出登录', '刷新']) {
      expect(screen.getByRole('button', { name })).toBeTruthy()
    }
    expect((document.querySelector('.admin-topbar-brand') as HTMLElement).textContent).toContain('概览')
    fireEvent.click(screen.getByRole('button', { name: '访客流量' }))
    expect((document.querySelector('.admin-topbar-brand') as HTMLElement).textContent).toContain('访客流量')
    expect(await screen.findByText('traffic-view')).toBeTruthy()
  })

  it('shows a coded load error in the active language', async () => {
    api.getOverview.mockRejectedValue(
      new AdminApiError(500, 'internal service error', 'admin.orgs.not_found', { id: 'abc' }),
    )
    render(<AdminApp />)
    expect((await screen.findByText('Organization not found: abc'))).toBeTruthy()
    chooseChinese()
    expect(screen.getByText('找不到组织：abc')).toBeTruthy()
  })

  it('shows the server detail for an unknown code, and the status when there is none', async () => {
    api.getOverview.mockRejectedValue(new AdminApiError(400, 'plain server text', 'admin.nope'))
    const { unmount } = render(<AdminApp />)
    expect(await screen.findByText('plain server text')).toBeTruthy()
    unmount()

    api.getOverview.mockRejectedValue(new AdminApiError(404, ''))
    render(<AdminApp />)
    expect(await screen.findByText('Request failed with status 404')).toBeTruthy()
  })

  it('shows the server message for a 5xx without a body, and the network message for a TypeError', async () => {
    api.getOverview.mockRejectedValue(new AdminApiError(503, ''))
    const { unmount } = render(<AdminApp />)
    expect(await screen.findByText('The server ran into a problem. Try again in a moment.')).toBeTruthy()
    unmount()

    api.getOverview.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<AdminApp />)
    expect(
      await screen.findByText('Could not reach the admin API. Try again in a moment.'),
    ).toBeTruthy()
    chooseChinese()
    expect(screen.getByText('无法连接管理 API，请稍后再试。')).toBeTruthy()
  })
})
