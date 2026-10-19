import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { DeactivateMemberDialog } from './DeactivateMemberDialog'
import type { MemberSummary, OffboardingChecklist } from './types'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars ? `${key}${JSON.stringify(vars)}` : key,
    i18n: { language: 'en-US' },
  }),
}))
const owner = vi.hoisted(() => ({ sessionToken: 'tok', isGuest: false, orgRole: 'owner' }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ credentials: owner }) }))
const mocks = vi.hoisted(() => ({
  getChecklist: vi.fn(),
  deactivate: vi.fn(),
}))
vi.mock('./api', () => ({ getOffboardingChecklist: mocks.getChecklist }))
vi.mock('./MembersContext', () => ({ useMembers: () => ({ deactivate: mocks.deactivate }) }))

const member: MemberSummary = {
  id: 'u1',
  display_name: 'Ada',
  email: 'ada@example.com',
  status: 'active',
  role_code: 'member',
  is_pending: false,
  created_at: '',
  updated_at: '',
}

function checklist(overrides: Partial<OffboardingChecklist['impact']> = {}, needs = 0): OffboardingChecklist {
  return {
    member: { id: 'u1', display_name: 'Ada', email: 'ada@example.com', status: 'active', role_code: 'member' },
    personal_space_status: 'active',
    owned_spaces: [],
    owned_spaces_total: needs,
    needs_transfer_total: needs,
    reviewer_spaces: [],
    reviewer_spaces_total: 0,
    uploads_as_uploader: [],
    uploads_as_uploader_total: 0,
    impact: {
      oauth_grants: 2,
      feishu_tokens: 1,
      feishu_sources: 1,
      private_model_bindings: 1,
      personal_spaces: 1,
      org_selections_referencing_private_keys: [],
      ...overrides,
    },
  }
}

describe('DeactivateMemberDialog', () => {
  beforeEach(() => {
    mocks.getChecklist.mockReset()
    mocks.deactivate.mockReset()
  })

  it('lists what will happen, including the global effect of deleting private keys', async () => {
    mocks.getChecklist.mockResolvedValue(checklist({}, 3))
    render(<DeactivateMemberDialog member={member} onClose={vi.fn()} onDone={vi.fn()} />)
    expect(await screen.findByText('deactivateDialog.byokGlobal')).toBeInTheDocument()
    expect(screen.getByText('deactivateDialog.sessions')).toBeInTheDocument()
    expect(screen.getByText(/deactivateDialog.oauth\{"count":2\}/)).toBeInTheDocument()
    expect(screen.getByText(/deactivateDialog.needsTransfer\{"count":3\}/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /deactivateDialog.confirm/ })).toBeEnabled()
  })

  it('refuses to confirm while the organisation embedding model uses the private key', async () => {
    mocks.getChecklist.mockResolvedValue(
      checklist({ org_selections_referencing_private_keys: ['embedding'] }),
    )
    render(<DeactivateMemberDialog member={member} onClose={vi.fn()} onDone={vi.fn()} />)
    expect(await screen.findByText('deactivateDialog.embeddingBlocked')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /deactivateDialog.confirm/ })).toBeDisabled()
  })

  it('warns about the chat fallback but still lets the owner confirm', async () => {
    mocks.getChecklist.mockResolvedValue(
      checklist({ org_selections_referencing_private_keys: ['chat'] }),
    )
    render(<DeactivateMemberDialog member={member} onClose={vi.fn()} onDone={vi.fn()} />)
    expect(await screen.findByText('deactivateDialog.orgChatFallback')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /deactivateDialog.confirm/ })).toBeEnabled()
  })

  it('deactivates on confirm and tells the page', async () => {
    mocks.getChecklist.mockResolvedValue(checklist())
    const updated = { ...member, status: 'suspended' as const }
    mocks.deactivate.mockResolvedValue({ member: updated, changed: true, effects: {} })
    const onDone = vi.fn()
    render(<DeactivateMemberDialog member={member} onClose={vi.fn()} onDone={onDone} />)
    await userEvent.click(await screen.findByRole('button', { name: /deactivateDialog.confirm/ }))
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(updated))
    expect(mocks.deactivate).toHaveBeenCalledWith('u1')
  })

  it('shows the server refusal in words, by its code', async () => {
    mocks.getChecklist.mockResolvedValue(checklist())
    mocks.deactivate.mockRejectedValue(
      new ApiError(409, 'raw detail', 'org_embedding_uses_private_key'),
    )
    render(<DeactivateMemberDialog member={member} onClose={vi.fn()} onDone={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: /deactivateDialog.confirm/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'offboarding.error.org_embedding_uses_private_key',
    )
  })

  it('closes on Escape and gives focus back', async () => {
    mocks.getChecklist.mockResolvedValue(checklist())
    const onClose = vi.fn()
    render(<DeactivateMemberDialog member={member} onClose={onClose} onDone={vi.fn()} />)
    await screen.findByText('deactivateDialog.sessions')
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
