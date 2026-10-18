import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MembersPanel } from './MembersPanel'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'en-US' } }),
}))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ credentials: { sessionToken: 'tok', isGuest: false } }),
}))

const members = vi.hoisted(() => ({
  state: { kind: 'idle' } as { kind: string; items?: unknown[]; message?: string },
  reload: vi.fn(async () => undefined),
}))
vi.mock('./MembersContext', () => ({
  useMembers: () => ({ ...members, invite: vi.fn(), setRole: vi.fn() }),
}))

const person = (name: string) => ({
  id: name,
  display_name: name,
  email: `${name}@quberi.com`,
  role_code: 'member',
  is_pending: true,
  primary_org_unit_name: null,
})

describe('MembersPanel', () => {
  beforeEach(() => {
    members.reload.mockClear()
    members.state = { kind: 'idle' }
  })

  it('reads the list again when it is opened with a list that was loaded earlier', () => {
    members.state = { kind: 'ready', items: [person('ada')] }
    render(<MembersPanel />)
    expect(members.reload).toHaveBeenCalledTimes(1)
  })

  it('does not start a second load while the first is still under way', () => {
    members.state = { kind: 'loading' }
    render(<MembersPanel />)
    expect(members.reload).not.toHaveBeenCalled()
  })

  it('does not load again on a plain re-render', () => {
    members.state = { kind: 'ready', items: [person('ada')] }
    const { rerender } = render(<MembersPanel />)
    members.state = { kind: 'ready', items: [person('ada'), person('liming')] }
    rerender(<MembersPanel />)
    expect(members.reload).toHaveBeenCalledTimes(1)
    expect(screen.getByText('liming')).toBeInTheDocument()
  })
})
