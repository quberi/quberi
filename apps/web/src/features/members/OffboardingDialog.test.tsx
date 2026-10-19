import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { OffboardingDialog } from './OffboardingDialog'
import type { MemberSummary, OffboardingChecklist } from './types'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars && !('defaultValue' in vars) ? `${key}${JSON.stringify(vars)}` : key,
    i18n: { language: 'en-US' },
  }),
}))
const owner = vi.hoisted(() => ({ sessionToken: 'tok', isGuest: false, orgRole: 'owner' }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ credentials: owner }) }))
const mocks = vi.hoisted(() => ({ getChecklist: vi.fn(), transfer: vi.fn() }))
vi.mock('./api', () => ({
  getOffboardingChecklist: mocks.getChecklist,
  transferSpaces: mocks.transfer,
}))
const person = (id: string, name: string, status = 'active', role = 'member') => ({
  id,
  display_name: name,
  email: `${name}@example.com`,
  status,
  role_code: role,
})
vi.mock('./MembersContext', () => ({
  useMembers: () => ({
    state: {
      kind: 'ready',
      items: [
        person('u1', 'ada'),
        person('u2', 'grace'),
        person('u3', 'boss', 'active', 'owner'),
        person('u4', 'gone', 'suspended'),
      ],
    },
  }),
}))

const member = person('u1', 'ada', 'suspended') as unknown as MemberSummary

function checklist(status: 'active' | 'suspended' = 'suspended'): OffboardingChecklist {
  return {
    member: { id: 'u1', display_name: 'ada', email: 'ada@example.com', status, role_code: 'member' },
    personal_space_status: 'frozen',
    owned_spaces: [
      { space_id: 's1', name: 'Alpha', owner_type: 'user', status: 'active', other_owners: [], needs_transfer: true, pending_review_count: 2 },
      { space_id: 's2', name: 'Beta', owner_type: 'user', status: 'archived', other_owners: [{ user_id: 'u9', display_name: 'Zed' }], needs_transfer: false, pending_review_count: 0 },
    ],
    owned_spaces_total: 5,
    needs_transfer_total: 1,
    reviewer_spaces: [{ space_id: 's3', name: 'Gamma', pending_review_count: 1, other_reviewers: 0 }],
    reviewer_spaces_total: 1,
    uploads_as_uploader: [
      { document_id: 'd1', name: 'draft.pdf', size_bytes: 1, created_at: '', space_id: 's1', space_name: 'Alpha', reviewer_count: 0 },
    ],
    uploads_as_uploader_total: 1,
    impact: {
      oauth_grants: 0, feishu_tokens: 0, feishu_sources: 0, private_model_bindings: 0,
      personal_spaces: 1, org_selections_referencing_private_keys: [],
    },
  }
}

describe('OffboardingDialog', () => {
  beforeEach(() => {
    mocks.getChecklist.mockReset()
    mocks.transfer.mockReset()
  })

  it('shows the three lists, the hidden-count hint and a no-reviewer warning in words', async () => {
    mocks.getChecklist.mockResolvedValue(checklist())
    render(<OffboardingDialog member={member} onClose={vi.fn()} />)
    expect(await screen.findByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('Gamma')).toBeInTheDocument()
    expect(screen.getByText('draft.pdf')).toBeInTheDocument()
    expect(screen.getByText(/offboarding.moreHidden\{"count":3\}/)).toBeInTheDocument()
    // Not colour alone: the missing-reviewer state is text.
    expect(screen.getAllByText('offboarding.noReviewer').length).toBe(2)
  })

  it('marks an archived space with a label and leaves active ones unlabeled', async () => {
    mocks.getChecklist.mockResolvedValue(checklist())
    render(<OffboardingDialog member={member} onClose={vi.fn()} />)
    const beta = (await screen.findByText('Beta')).closest('li') as HTMLElement
    const alpha = screen.getByText('Alpha').closest('li') as HTMLElement
    expect(within(beta).getByText('offboarding.archivedTag')).toBeInTheDocument()
    expect(within(alpha).queryByText('offboarding.archivedTag')).toBeNull()
    // An archived space can be chosen for the handover like any other.
    expect(within(beta).getByRole('checkbox')).toBeEnabled()
  })
  it('offers only active members who are not the organisation owner as the new owner', async () => {
    mocks.getChecklist.mockResolvedValue(checklist())
    render(<OffboardingDialog member={member} onClose={vi.fn()} />)
    await screen.findByText('Alpha')
    const select = screen.getByRole('combobox')
    const options = within(select).getAllByRole('option').map((o) => o.textContent)
    expect(options.some((o) => o?.includes('grace'))).toBe(true)
    expect(options.some((o) => o?.includes('boss'))).toBe(false) // the organisation owner
    expect(options.some((o) => o?.includes('gone'))).toBe(false) // deactivated
    expect(options.some((o) => o?.includes('ada@'))).toBe(false) // the departing member
  })

  it('does not allow a handover before the member is deactivated', async () => {
    mocks.getChecklist.mockResolvedValue(checklist('active'))
    render(<OffboardingDialog member={member} onClose={vi.fn()} />)
    await screen.findByText('Alpha')
    expect(screen.getByText('offboarding.notSuspended')).toBeInTheDocument()
    expect(screen.getAllByRole('checkbox')[0]).toBeDisabled()
    expect(screen.getByRole('combobox')).toBeDisabled()
  })

  it('sends the chosen spaces and new owner, then reports each space', async () => {
    mocks.getChecklist.mockResolvedValue(checklist())
    mocks.transfer.mockResolvedValue({
      new_owner_user_id: 'u2',
      results: [
        { space_id: 's1', status: 'transferred', code: null, pending_review_count: 2 },
        { space_id: 's2', status: 'skipped', code: 'has_other_owner', pending_review_count: 0 },
      ],
      transferred: 1, already_done: 0, skipped: 1, failed: 0,
    })
    render(<OffboardingDialog member={member} onClose={vi.fn()} />)
    await screen.findByText('Alpha')
    const submit = screen.getByRole('button', { name: /offboarding.transfer/ })
    expect(submit).toBeDisabled()
    const [first, second] = screen.getAllByRole('checkbox')
    await userEvent.click(first)
    await userEvent.click(second)
    await userEvent.selectOptions(screen.getByRole('combobox'), 'u2')
    await userEvent.click(submit)
    await waitFor(() => expect(mocks.transfer).toHaveBeenCalledWith(
      expect.anything(), 'u1', 'u2', ['s1', 's2'],
    ))
    expect(await screen.findByText(/offboarding.resultsSummary/)).toBeInTheDocument()
    expect(screen.getByText('offboarding.result.transferred')).toBeInTheDocument()
    expect(screen.getByText(/offboarding.result.skipped/)).toBeInTheDocument()
    // The list is read again after the handover.
    expect(mocks.getChecklist).toHaveBeenCalledTimes(2)
  })

  it('shows a request-level refusal by its code and keeps the dialog open', async () => {
    const { ApiError } = await import('../api/client')
    mocks.getChecklist.mockResolvedValue(checklist())
    mocks.transfer.mockRejectedValue(new ApiError(400, 'x', 'new_owner_not_active'))
    const onClose = vi.fn()
    render(<OffboardingDialog member={member} onClose={onClose} />)
    await screen.findByText('Alpha')
    await userEvent.click(screen.getAllByRole('checkbox')[0])
    await userEvent.selectOptions(screen.getByRole('combobox'), 'u2')
    await userEvent.click(screen.getByRole('button', { name: /offboarding.transfer/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('offboarding.error.new_owner_not_active')
    expect(onClose).not.toHaveBeenCalled()
  })
})
