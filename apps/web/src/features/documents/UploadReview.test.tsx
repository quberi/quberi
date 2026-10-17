import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PendingReviewInbox } from './PendingReviewInbox'
import { UploadReviewSection } from './UploadReview'

const api = vi.hoisted(() => ({
  setUploadReview: vi.fn(),
  listReviewDocuments: vi.fn(),
  decideReview: vi.fn(),
  listPendingSpaces: vi.fn(),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't', isGuest: false } }))
const docs = vi.hoisted(() => ({ download: vi.fn(), reloadPage: vi.fn(), reloadStats: vi.fn() }))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string, o?: Record<string, unknown>) => (o?.space ? `${k}:${o.space}:${o.count}` : k) }),
}))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('../../i18n/formatDate', () => ({ useFormatDate: () => (v: string) => v }))
vi.mock('./DocumentsContext', () => ({ useDocuments: () => docs }))
vi.mock('./DocumentRow', () => ({ formatBytes: (n: number) => `${n} B` }))
vi.mock('./reviewApi', () => api)

const item = {
  id: 'd1', name: 'draft.md', mime_type: 'text/markdown', size_bytes: 10, uploader_name: 'Eve',
  created_at: '2026-01-01T00:00:00Z', review_state: 'pending', review_note: null, reviewed_by_name: null,
}

describe('UploadReviewSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listReviewDocuments.mockResolvedValue({ items: [item], total: 1 })
    docs.reloadPage.mockResolvedValue(undefined)
    docs.reloadStats.mockResolvedValue(undefined)
  })

  it('shows nothing to a viewer, or to an editor when approval is off', () => {
    const { container, rerender } = render(<UploadReviewSection required role="viewer" spaceId="s1" />)
    expect(container.textContent).toBe('')
    rerender(<UploadReviewSection required={false} role="editor" spaceId="s1" />)
    expect(container.textContent).toBe('')
  })

  it('tells an editor that uploads wait, without a list or a switch', () => {
    render(<UploadReviewSection required role="editor" spaceId="s1" />)
    expect(screen.getByText('uploadReview.hintEditor')).toBeTruthy()
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(api.listReviewDocuments).not.toHaveBeenCalled()
  })

  it('lets only the owner flip the switch', async () => {
    api.setUploadReview.mockResolvedValue({ space_id: 's1', enabled: true })
    const { unmount } = render(<UploadReviewSection required={false} role="owner" spaceId="s1" />)
    fireEvent.click(screen.getByRole('checkbox'))
    await waitFor(() => expect(api.setUploadReview).toHaveBeenCalledWith(auth.credentials, 's1', true))
    unmount()
    render(<UploadReviewSection required role="admin" spaceId="s1" />)
    await screen.findByText('draft.md')
    expect(screen.queryByRole('checkbox', { name: 'uploadReview.toggle' })).toBeNull()
  })

  it('approves the ticked uploads, and rejecting needs a reason', async () => {
    api.decideReview.mockResolvedValue({ changed: ['d1'] })
    render(<UploadReviewSection required role="admin" spaceId="s1" />)
    await screen.findByText('draft.md')
    fireEvent.click(screen.getAllByRole('checkbox')[0])
    fireEvent.click(screen.getByText('uploadReview.reject'))
    expect(await screen.findByText('uploadReview.needReason')).toBeTruthy()
    expect(api.decideReview).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('uploadReview.note'), { target: { value: 'contains PII' } })
    fireEvent.click(screen.getByText('uploadReview.reject'))
    await waitFor(() =>
      expect(api.decideReview).toHaveBeenCalledWith(auth.credentials, 's1', ['d1'], 'reject', 'contains PII'),
    )
    await waitFor(() => expect(docs.reloadPage).toHaveBeenCalled())
  })
})

describe('PendingReviewInbox', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists spaces with uploads waiting and opens one', async () => {
    api.listPendingSpaces.mockResolvedValue({ items: [{ space_id: 's1', space_name: 'Wiki', pending: 3 }], total_pending: 3 })
    const onOpen = vi.fn()
    render(<PendingReviewInbox onOpenSpace={onOpen} />)
    fireEvent.click(await screen.findByText('uploadReview.inboxOpen'))
    expect(onOpen).toHaveBeenCalledWith('s1')
  })

  it('shows nothing when nothing is waiting', async () => {
    api.listPendingSpaces.mockResolvedValue({ items: [], total_pending: 0 })
    const { container } = render(<PendingReviewInbox onOpenSpace={vi.fn()} />)
    await waitFor(() => expect(api.listPendingSpaces).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
