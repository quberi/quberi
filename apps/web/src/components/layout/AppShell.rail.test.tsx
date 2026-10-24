import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import enUS from '../../i18n/locales/en-US.json'
import zhCN from '../../i18n/locales/zh-CN.json'

import { AppShell } from './AppShell'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { resolvedLanguage: 'zh-CN', changeLanguage: vi.fn() },
  }),
}))
vi.mock('../../features/auth/AuthContext', () => ({
  useAuth: () => ({ credentials: { tier: 'personal' }, isGuest: true, signOut: vi.fn() }),
}))
vi.mock('./OrgSwitcher', () => ({ OrgSwitcher: () => <div>org-switcher</div> }))

describe('the side rail', () => {
  it('only has things that work: no greyed-out "coming soon" placeholders', () => {
    const { container } = render(
      <AppShell currentView="chat" onSelectView={() => {}}>
        <p>page</p>
      </AppShell>,
    )
    const items = Array.from(container.querySelectorAll('.rail-item')) as HTMLButtonElement[]
    expect(items.length).toBeGreaterThanOrEqual(4)
    expect(items.filter((b) => b.disabled || b.getAttribute('aria-disabled') === 'true')).toEqual([])
    expect(container.querySelector('.planned')).toBeNull()
    expect(container.querySelector('.rail-divider')).toBeNull()
  })

  it('offers the four real views, in order', () => {
    const { container } = render(
      <AppShell currentView="chat" onSelectView={() => {}}>
        <p>page</p>
      </AppShell>,
    )
    const labels = Array.from(container.querySelectorAll('.rail-item-label')).map(
      (n) => n.textContent,
    )
    expect(labels.slice(0, 4)).toEqual([
      'navigation.chat',
      'navigation.agents',
      'navigation.knowledge',
      'navigation.myModels',
    ])
  })

  it('has no leftover wording for the removed placeholders in either language', () => {
    for (const bundle of [zhCN, enUS]) {
      const nav = (bundle as unknown as { navigation: Record<string, string> }).navigation
      for (const key of ['search', 'history', 'planned', 'comingSoon']) expect(nav[key], key).toBeUndefined()
    }
  })
})
