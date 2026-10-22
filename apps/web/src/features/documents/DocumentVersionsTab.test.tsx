import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import { VersionsTab } from './DocumentDetailDrawer'
import type { DocumentSummary, DocumentVersion } from './types'

const api = vi.hoisted(() => ({
  listDocumentVersions: vi.fn(),
  uploadNewVersion: vi.fn(),
  downloadDocumentVersion: vi.fn(),
}))
const documents = vi.hoisted(() => ({ reloadPage: vi.fn(), reloadStats: vi.fn() }))

vi.mock('./api', () => api)
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (k: string, o?: Record<string, unknown>) => (o ? `${k} ${JSON.stringify(o)}` : k),
  }),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't' } })) // stable, like the real one
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('./DocumentsContext', () => ({ useDocuments: () => documents }))
vi.mock('../../i18n/formatDate', () => ({ useFormatDate: () => (v: string) => `on ${v}` }))

const doc = {
  id: 'd1',
  space_id: 's1',
  name: 'leave.md',
  version_number: 2,
  version_count: 2,
} as unknown as DocumentSummary

const version = (n: number, over: Partial<DocumentVersion> = {}): DocumentVersion => ({
  version_id: `v${n}`,
  version_number: n,
  filename: `leave-${n}.md`,
  mime_type: 'text/markdown',
  size_bytes: 100,
  checksum_sha256: `${n}`.repeat(64),
  note: null,
  created_at: '2026-01-0' + n,
  created_by_user_id: 'u1',
  created_by_name: 'Ann',
  is_current: false,
  ...over,
})

describe('VersionsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listDocumentVersions.mockResolvedValue({
      document_id: 'd1',
      versions: [version(2, { is_current: true, note: '10 days now' }), version(1)],
    })
    documents.reloadPage.mockResolvedValue(undefined)
    documents.reloadStats.mockResolvedValue(undefined)
  })

  it('lists every version, newest first, and marks the current one', async () => {
    render(<VersionsTab canAdd={false} doc={doc} />)
    expect(await screen.findByText('leave-2.md')).toBeTruthy()
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0].textContent).toContain('knowledge.versions.current')
    expect(items[0].textContent).toContain('10 days now')
    expect(items[1].textContent).not.toContain('knowledge.versions.current')
    expect(api.listDocumentVersions).toHaveBeenCalledWith({ sessionToken: 't' }, 's1', 'd1')
  })

  it('offers uploading a new version only to someone who may', async () => {
    const { unmount } = render(<VersionsTab canAdd={false} doc={doc} />)
    await screen.findByText('leave-2.md')
    expect(screen.queryByText('knowledge.versions.upload')).toBeNull()
    unmount()
    render(<VersionsTab canAdd doc={doc} />)
    expect(await screen.findByText('knowledge.versions.upload')).toBeTruthy()
  })

  it('uploads the chosen file with the note, then reloads the list and the page', async () => {
    api.uploadNewVersion.mockResolvedValue({ ...doc, version_number: 3, version_count: 3 })
    const { container } = render(<VersionsTab canAdd doc={doc} />)
    await screen.findByText('leave-2.md')
    fireEvent.change(screen.getByLabelText('knowledge.versions.noteLabel'), {
      target: { value: '  15 days  ' },
    })
    const file = new File(['x'], 'leave-3.md', { type: 'text/markdown' })
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } })

    await waitFor(() => expect(api.uploadNewVersion).toHaveBeenCalled())
    const [, spaceId, documentId, sent, options] = api.uploadNewVersion.mock.calls[0]
    expect([spaceId, documentId, sent]).toEqual(['s1', 'd1', file])
    expect(options.note).toBe('  15 days  ') // trimmed by the API wrapper, not here
    await waitFor(() => expect(documents.reloadPage).toHaveBeenCalled())
    expect(documents.reloadStats).toHaveBeenCalled()
    expect(api.listDocumentVersions).toHaveBeenCalledTimes(2)
  })

  it('shows the reason when a version is refused and does not reload', async () => {
    api.uploadNewVersion.mockRejectedValue(
      new ApiError(409, 'This file is identical to the current version'),
    )
    const { container } = render(<VersionsTab canAdd doc={doc} />)
    await screen.findByText('leave-2.md')
    const file = new File(['x'], 'same.md')
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } })

    expect((await screen.findByRole('alert')).textContent).toContain('identical to the current')
    expect(documents.reloadPage).not.toHaveBeenCalled()
    expect(api.listDocumentVersions).toHaveBeenCalledTimes(1)
  })

  it('downloads an earlier version by its number', async () => {
    api.downloadDocumentVersion.mockResolvedValue({ blob: new Blob(['x']), filename: 'leave-1.md' })
    const created = vi.fn(() => 'blob:x')
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() })
    render(<VersionsTab canAdd={false} doc={doc} />)
    await screen.findByText('leave-1.md')

    fireEvent.click(screen.getByTitle('knowledge.versions.download {"number":1}'))

    await waitFor(() =>
      expect(api.downloadDocumentVersion).toHaveBeenCalledWith({ sessionToken: 't' }, 's1', 'd1', 1),
    )
  })
})
