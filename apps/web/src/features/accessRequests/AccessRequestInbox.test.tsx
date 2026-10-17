import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AccessRequestInbox } from './AccessRequestInbox'

const api = vi.hoisted(() => ({
  listRequestsToDecide: vi.fn(),
  decideAccessRequest: vi.fn(),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't', isGuest: false } }))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string, o?: Record<string, unknown>) => (o?.name ? `${k}:${o.name}` : k) }),
}))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('../../i18n/formatDate', () => ({ useFormatDate: () => (v: string) => v }))
vi.mock('./api', () => api)

const request = {
  id: 'r1', space_id: 's1', space_name: 'Tech', requester_user_id: 'u2', requester_name: 'Bob',
  requester_email: 'bob@example.com', role: 'viewer', reason: 'quarterly review', status: 'pending',
  decided_by_name: null, decision_note: null, created_at: '2026-01-01T00:00:00Z', decided_at: null,
}

describe('AccessRequestInbox', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows nothing when no request is waiting', async () => {
    api.listRequestsToDecide.mockResolvedValue({ items: [], total: 0 })
    const { container } = render(<AccessRequestInbox />)
    await waitFor(() => expect(api.listRequestsToDecide).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('shows the request with its reason, and approves with the note typed', async () => {
    api.listRequestsToDecide
      .mockResolvedValueOnce({ items: [request], total: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 })
    api.decideAccessRequest.mockResolvedValue({ ...request, status: 'approved' })
    const onDecided = vi.fn()
    render(<AccessRequestInbox onDecided={onDecided} />)
    expect(await screen.findByText('quarterly review')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('accessRequests.note'), { target: { value: 'until Friday' } })
    fireEvent.click(screen.getByText('accessRequests.approve'))
    await waitFor(() =>
      expect(api.decideAccessRequest).toHaveBeenCalledWith(auth.credentials, 'r1', true, 'until Friday'),
    )
    await waitFor(() => expect(onDecided).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByText('quarterly review')).toBeNull())
  })

  it('rejects, and shows the server error when it fails', async () => {
    api.listRequestsToDecide.mockResolvedValue({ items: [request], total: 1 })
    api.decideAccessRequest.mockRejectedValueOnce(new Error('boom'))
    render(<AccessRequestInbox />)
    await screen.findByText('quarterly review')
    fireEvent.click(screen.getByText('accessRequests.reject'))
    expect(await screen.findByText('Error: boom')).toBeTruthy()
    expect(api.decideAccessRequest).toHaveBeenCalledWith(auth.credentials, 'r1', false, '')
  })
})
