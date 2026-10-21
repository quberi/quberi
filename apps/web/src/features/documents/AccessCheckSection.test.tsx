import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { AccessCheckSection } from './AccessCheckSection'

const api = vi.hoisted(() => ({ checkDocumentAccess: vi.fn() }))

vi.mock('react-i18next', () => ({
  // Echo the key, and the fallback sentence when the screen has no wording of its own.
  useTranslation: () => ({
    t: (k: string, o?: { defaultValue?: string; name?: string }) =>
      o?.name ? `${k}|${o.name}` : (o?.defaultValue ?? k),
  }),
}))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ credentials: { sessionToken: 't' } }) }))
vi.mock('./accessRulesApi', () => api)
vi.mock('../members/MembersContext', () => ({
  useMembers: () => ({
    state: {
      kind: 'ready',
      items: [
        { id: 'u1', display_name: 'Ann', email: 'ann@x.com', status: 'active' },
        { id: 'u2', display_name: 'Old Ben', email: 'ben@x.com', status: 'suspended' },
      ],
    },
  }),
}))

const step = (over: Record<string, unknown>) => ({
  kind: 'x',
  effect: 'info',
  code: 'x',
  summary: 'server sentence',
  subject_type: null,
  subject_id: null,
  subject_display_name: null,
  role_code: null,
  hit_path: null,
  rule_id: null,
  ...over,
})

const answer = (over: Record<string, unknown> = {}) => ({
  user_id: 'u1',
  user_display_name: 'Ann',
  can_read: false,
  outcome: 'denied_by_rule',
  appears_in_search: false,
  steps: [step({ code: 'deny_rule', effect: 'blocks', subject_display_name: 'Sales' })],
  ...over,
})

describe('AccessCheckSection', () => {
  beforeEach(() => vi.clearAllMocks())

  it('offers suspended members too, marked as such', () => {
    render(<AccessCheckSection documentId="d1" rulesVersion="" spaceId="s1" />)
    expect(screen.getByText('Old Ben · ben@x.com · docAccess.check.inactive')).toBeTruthy()
  })

  it('asks about the picked person and words the verdict and reasons itself', async () => {
    api.checkDocumentAccess.mockResolvedValue(answer())
    render(<AccessCheckSection documentId="d1" rulesVersion="" spaceId="s1" />)
    fireEvent.click(screen.getByText('docAccess.check.run'))
    expect(await screen.findByText('docAccess.check.outcome.denied_by_rule|Ann')).toBeTruthy()
    expect(api.checkDocumentAccess).toHaveBeenCalledWith({ sessionToken: 't' }, 's1', 'd1', 'u1')
    expect(screen.getByText('docAccess.check.step.deny_rule|Sales')).toBeTruthy()
    expect(screen.getByText('docAccess.check.searchNo')).toBeTruthy()
    expect(document.querySelector('[data-can-read="no"]')).toBeTruthy()
  })

  it('marks a person who can read as such', async () => {
    api.checkDocumentAccess.mockResolvedValue(
      answer({ can_read: true, outcome: 'allowed_by_space', appears_in_search: true, steps: [] }),
    )
    render(<AccessCheckSection documentId="d1" rulesVersion="" spaceId="s1" />)
    fireEvent.click(screen.getByText('docAccess.check.run'))
    await screen.findByText('docAccess.check.outcome.allowed_by_space|Ann')
    expect(document.querySelector('[data-can-read="yes"]')).toBeTruthy()
    expect(screen.getByText('docAccess.check.searchYes')).toBeTruthy()
  })

  it('falls back to the server sentence for a reason it has no wording for', async () => {
    api.checkDocumentAccess.mockResolvedValue(answer({ steps: [step({ code: 'brand_new' })] }))
    render(<AccessCheckSection documentId="d1" rulesVersion="" spaceId="s1" />)
    fireEvent.click(screen.getByText('docAccess.check.run'))
    expect(await screen.findByText('server sentence')).toBeTruthy()
  })

  it('drops the answer when the rules change, and when another person is picked', async () => {
    api.checkDocumentAccess.mockResolvedValue(answer())
    const { rerender } = render(<AccessCheckSection documentId="d1" rulesVersion="a" spaceId="s1" />)
    fireEvent.click(screen.getByText('docAccess.check.run'))
    await screen.findByText('docAccess.check.outcome.denied_by_rule|Ann')
    rerender(<AccessCheckSection documentId="d1" rulesVersion="b" spaceId="s1" />)
    await waitFor(() => expect(screen.queryByText('docAccess.check.outcome.denied_by_rule|Ann')).toBeNull())

    fireEvent.click(screen.getByText('docAccess.check.run'))
    await screen.findByText('docAccess.check.outcome.denied_by_rule|Ann')
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'u2' } })
    await waitFor(() => expect(screen.queryByText('docAccess.check.outcome.denied_by_rule|Ann')).toBeNull())
  })

  it('shows the server error when the check fails', async () => {
    api.checkDocumentAccess.mockRejectedValue(new ApiError(403, 'not allowed'))
    render(<AccessCheckSection documentId="d1" rulesVersion="" spaceId="s1" />)
    fireEvent.click(screen.getByText('docAccess.check.run'))
    expect(await screen.findByText('not allowed')).toBeTruthy()
  })
})
