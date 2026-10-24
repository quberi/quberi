import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import enUS from '../../i18n/locales/en-US.json'
import zhCN from '../../i18n/locales/zh-CN.json'

import { AppShell } from './AppShell'

const auth = vi.hoisted(() => ({
  credentials: { tier: 'personal' } as Record<string, unknown> | null,
  isGuest: true,
  signOut: vi.fn(),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { resolvedLanguage: 'zh-CN', changeLanguage: vi.fn() },
  }),
}))
vi.mock('../../features/auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('./OrgSwitcher', () => ({ OrgSwitcher: () => <div>org-switcher</div> }))

const shell = (over: Partial<Parameters<typeof AppShell>[0]> = {}) =>
  render(
    <AppShell currentView="chat" onSelectView={() => {}} onOpenAuth={over.onOpenAuth} {...over}>
      <p>page body</p>
    </AppShell>,
  )

beforeEach(() => {
  auth.credentials = { tier: 'personal' }
  auth.isGuest = true
})

describe('the guest trial note in the top bar', () => {
  it('tells a guest this is a personal trial account and where to go for the enterprise one', () => {
    shell({ onOpenAuth: () => {} })
    expect(screen.getByRole('note').textContent).toContain('auth.guestTrialNote')
  })

  it('opens sign-in from the note', () => {
    const onOpenAuth = vi.fn()
    shell({ onOpenAuth })
    fireEvent.click(screen.getByRole('button', { name: 'auth.guestTrialSignIn' }))
    expect(onOpenAuth).toHaveBeenCalledTimes(1)
  })

  it('is not shown to someone who is signed in', () => {
    auth.isGuest = false
    auth.credentials = { tier: 'team', userEmail: 'ada@example.test', displayName: 'Ada' }
    shell({ onOpenAuth: () => {} })
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('is not shown before there are any credentials at all', () => {
    auth.credentials = null
    shell({ onOpenAuth: () => {} })
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('shows the note without a button when the page gives no way to sign in', () => {
    shell()
    expect(screen.getByRole('note')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'auth.guestTrialSignIn' })).toBeNull()
  })

  it('sits in the top bar, so it shows on every page and does not move the page', () => {
    shell({ onOpenAuth: () => {} })
    const bar = screen.getByRole('note').closest('header')
    expect(bar?.className).toBe('topbar')
    expect(screen.getByText('page body').closest('main')).not.toContain(screen.getByRole('note'))
  })
})

describe('the wording', () => {
  it('says what was asked for, in Chinese', () => {
    expect(zhCN.auth.guestTrialNote).toBe('这是个人用户体验账号，如果您想体验企业知识库，请您先登录。')
    expect(zhCN.auth.guestTrialSignIn).toBe('登录')
  })

  it('says the same in English', () => {
    expect(enUS.auth.guestTrialNote).toBe(
      'This is a personal trial account. To try the enterprise knowledge base, please sign in first.',
    )
    expect(enUS.auth.guestTrialSignIn).toBe('Sign in')
  })
})
