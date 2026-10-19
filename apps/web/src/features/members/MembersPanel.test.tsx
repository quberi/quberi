import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MembersPanel } from './MembersPanel'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'en-US' } }),
}))
const auth = vi.hoisted(() => ({
  credentials: { sessionToken: 'tok', isGuest: false, userEmail: 'boss@quberi.com', orgRole: '' } as {
    sessionToken: string
    isGuest: boolean
    userEmail: string
    orgRole: string
  },
}))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))

const members = vi.hoisted(() => ({
  state: { kind: 'idle' } as { kind: string; items?: unknown[]; message?: string },
  reload: vi.fn(async () => undefined),
}))
vi.mock('../groups/api', () => ({ listGroupsForUser: vi.fn(async () => ({ items: [] })) }))
vi.mock('./DeactivateMemberDialog', () => ({
  DeactivateMemberDialog: () => <div>deactivate-dialog</div>,
}))
vi.mock('./OffboardingDialog', () => ({ OffboardingDialog: () => <div>offboarding-dialog</div> }))
vi.mock('./MembersContext', () => ({
  useMembers: () => ({
    ...members,
    invite: vi.fn(),
    setRole: vi.fn(),
    deactivate: vi.fn(),
    reactivate: vi.fn(),
  }),
}))

const person = (name: string, extra: Record<string, unknown> = {}) => ({
  id: name,
  display_name: name,
  email: `${name}@quberi.com`,
  role_code: 'member',
  status: 'active',
  is_pending: true,
  primary_org_unit_name: null,
  ...extra,
})

describe('MembersPanel', () => {
  beforeEach(() => {
    members.reload.mockClear()
    members.state = { kind: 'idle' }
    auth.credentials.orgRole = ''
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

  describe('deactivating members', () => {
    const roster = () => [
      person('boss', { role_code: 'owner', is_pending: false }),
      person('ada'),
      person('gone', { status: 'suspended', is_pending: false }),
    ]

    it('offers deactivate, reactivate and the checklist to the organisation owner only', () => {
      auth.credentials.orgRole = 'owner'
      members.state = { kind: 'ready', items: roster() }
      render(<MembersPanel />)
      const ada = screen.getByText('ada').closest('li') as HTMLElement
      expect(within(ada).getByRole('button', { name: /members.deactivate/ })).toBeInTheDocument()
      const gone = screen.getByText('gone').closest('li') as HTMLElement
      expect(within(gone).getByText('members.suspended')).toBeInTheDocument()
      expect(within(gone).getByRole('button', { name: /members.reactivate/ })).toBeInTheDocument()
      expect(within(gone).getByRole('button', { name: /members.offboarding/ })).toBeInTheDocument()
      expect(within(gone).queryByRole('button', { name: /members.deactivate/ })).toBeNull()
      // Never on the owner's own row.
      const boss = screen.getByText('boss').closest('li') as HTMLElement
      expect(within(boss).queryByRole('button', { name: /members.deactivate/ })).toBeNull()
    })

    it('shows nothing to manage to an administrator', () => {
      auth.credentials.orgRole = 'admin'
      members.state = { kind: 'ready', items: roster() }
      render(<MembersPanel />)
      expect(screen.queryByRole('button', { name: /members.deactivate/ })).toBeNull()
      expect(screen.queryByRole('button', { name: /members.reactivate/ })).toBeNull()
      // The state is still visible to them.
      expect(screen.getByText('members.suspended')).toBeInTheDocument()
    })

    it('opens the confirmation dialog from the deactivate button', async () => {
      auth.credentials.orgRole = 'owner'
      members.state = { kind: 'ready', items: roster() }
      render(<MembersPanel />)
      const ada = screen.getByText('ada').closest('li') as HTMLElement
      await userEvent.click(within(ada).getByRole('button', { name: /members.deactivate/ }))
      expect(screen.getByText('deactivate-dialog')).toBeInTheDocument()
    })
  })
})
