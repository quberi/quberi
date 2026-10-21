import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { DocumentAccessTab } from './DocumentAccessTab'

const api = vi.hoisted(() => ({
  listAccessRules: vi.fn(),
  addAccessRule: vi.fn(),
  removeAccessRule: vi.fn(),
  checkDocumentAccess: vi.fn(),
  getDocumentAccessSummary: vi.fn(),
}))
vi.mock('../members/MembersContext', () => ({
  useMembers: () => ({ state: { kind: 'ready', items: [] } }),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't' } }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('./accessRulesApi', () => api)
const orgCandidates = vi.hoisted(() => [{ id: 'organization', label: 'Acme', subtitle: '' }])
const userCandidates = vi.hoisted(() => [{ id: 'u1', label: 'Ann', subtitle: 'ann@example.com' }])
vi.mock('../spaceAccess/useSubjectCandidates', async () => {
  const original = await vi.importActual<typeof import('../spaceAccess/useSubjectCandidates')>(
    '../spaceAccess/useSubjectCandidates',
  )
  return {
    ...original,
    useSubjectCandidates: (type: string) => (type === 'organization' ? orgCandidates : userCandidates),
  }
})

const rule = (id: string, effect: 'allow' | 'deny', name: string) => ({
  id,
  effect,
  subject_type: 'user',
  subject_id: 'u1',
  subject_display_name: name,
  subject_subtitle: null,
  created_at: '2026-01-01T00:00:00Z',
})

const summary = (over: Record<string, unknown> = {}) => ({
  document_id: 'd1',
  space_id: 's1',
  review_state: 'approved',
  space_people: 3,
  readers: 3,
  shared_only: 0,
  hidden: 0,
  allow_rules: 0,
  deny_rules: 0,
  sources: [],
  ...over,
})

describe('DocumentAccessTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getDocumentAccessSummary.mockResolvedValue(summary())
  })

  it('shows how many people can read it, above the rules', async () => {
    api.listAccessRules.mockResolvedValue({ items: [], total: 0 })
    api.getDocumentAccessSummary.mockResolvedValue(
      summary({ readers: 11, space_people: 12, hidden: 1, deny_rules: 1 }),
    )
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    expect(await screen.findByTestId('document-access-summary')).toBeTruthy()
    expect(screen.getByText('11')).toBeTruthy()
    expect(api.getDocumentAccessSummary).toHaveBeenCalledWith({ sessionToken: 't' }, 's1', 'd1')
  })

  it('still shows the rules when the numbers cannot be had', async () => {
    api.listAccessRules.mockResolvedValue({ items: [rule('r1', 'allow', 'Bob')], total: 1 })
    api.getDocumentAccessSummary.mockRejectedValue(new ApiError(500, 'boom'))
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    expect(await screen.findByText('Bob')).toBeTruthy()
    expect(screen.queryByTestId('document-access-summary')).toBeNull()
  })

  it('shows who the document is shared with and hidden from', async () => {
    api.listAccessRules.mockResolvedValue({
      items: [rule('r1', 'allow', 'Bob'), rule('r2', 'deny', 'Cara')],
      total: 2,
    })
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    expect(await screen.findByText('Bob')).toBeTruthy()
    expect(screen.getByText('Cara')).toBeTruthy()
    expect(api.listAccessRules).toHaveBeenCalledWith({ sessionToken: 't' }, 's1', 'd1')
  })

  it('tells someone who is not a manager that they cannot see or change this', async () => {
    api.listAccessRules.mockRejectedValue(new ApiError(403, 'no'))
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    expect(await screen.findByText('docAccess.forbidden')).toBeTruthy()
    expect(screen.queryByText('docAccess.add')).toBeNull()
  })

  it('adds a rule for the picked person and reloads', async () => {
    api.listAccessRules.mockResolvedValue({ items: [], total: 0 })
    api.addAccessRule.mockResolvedValue(rule('r3', 'deny', 'Ann'))
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    await screen.findByText('docAccess.noneShared')
    fireEvent.click(screen.getByRole('radio', { name: /docAccess.effectName.deny/ }))
    fireEvent.click(screen.getByText('docAccess.add'))
    await waitFor(() =>
      expect(api.addAccessRule).toHaveBeenCalledWith({ sessionToken: 't' }, 's1', 'd1', {
        effect: 'deny',
        subject_type: 'user',
        subject_id: 'u1',
      }),
    )
    await waitFor(() => expect(api.listAccessRules).toHaveBeenCalledTimes(2))
  })

  it('does not offer someone who is already on the list being added to', async () => {
    api.listAccessRules.mockResolvedValue({ items: [rule('r2', 'deny', 'Ann')], total: 1 })
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    await screen.findByText('Ann')
    // Allow is the default: Ann is denied, and a denial wins, so allowing her would do nothing.
    expect(screen.getByText('docAccess.allCovered')).toBeTruthy()
    await waitFor(() =>
      expect((screen.getByText('docAccess.add').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )
    fireEvent.click(screen.getByRole('radio', { name: /docAccess.effectName.deny/ }))
    expect(screen.getByText('docAccess.allCovered')).toBeTruthy()
  })

  it('still offers a person who is allowed when adding a denial', async () => {
    api.listAccessRules.mockResolvedValue({ items: [rule('r1', 'allow', 'Ann')], total: 1 })
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    await screen.findByText('Ann')
    // She is already allowed, so Allow has nobody left; Deny can still pick her.
    expect(screen.getByText('docAccess.allCovered')).toBeTruthy()
    fireEvent.click(screen.getByRole('radio', { name: /docAccess.effectName.deny/ }))
    expect(screen.getByText('Ann · ann@example.com')).toBeTruthy()
  })

  it('does not send an id for the whole organisation', async () => {
    api.listAccessRules.mockResolvedValue({ items: [], total: 0 })
    api.addAccessRule.mockResolvedValue(rule('r4', 'allow', 'Acme'))
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    await screen.findByText('docAccess.noneShared')
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'organization' } })
    fireEvent.click(screen.getByText('docAccess.add'))
    await waitFor(() => expect(api.addAccessRule).toHaveBeenCalled())
    const body = api.addAccessRule.mock.calls[0][3]
    expect(body).toEqual({ effect: 'allow', subject_type: 'organization', subject_id: undefined })
  })

  it('removes a rule and shows the server error when it fails', async () => {
    api.listAccessRules.mockResolvedValue({ items: [rule('r1', 'allow', 'Bob')], total: 1 })
    api.removeAccessRule.mockRejectedValueOnce(new ApiError(500, 'boom'))
    render(<DocumentAccessTab documentId="d1" spaceId="s1" />)
    await screen.findByText('Bob')
    fireEvent.click(screen.getByLabelText('docAccess.remove'))
    expect(await screen.findByText('boom')).toBeTruthy()
    expect(api.removeAccessRule).toHaveBeenCalledWith({ sessionToken: 't' }, 's1', 'd1', 'r1')
  })
})
