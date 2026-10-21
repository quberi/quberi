import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import type { PropsWithChildren } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { DocumentsProvider, useDocuments } from './DocumentsContext'
import { UploadDropZone } from './UploadDropZone'

const api = vi.hoisted(() => ({
  uploadDocument: vi.fn(),
  getDocumentsStats: vi.fn(),
  listDocuments: vi.fn(),
  deleteDocument: vi.fn(),
  downloadDocument: vi.fn(),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (k: string, o?: Record<string, string>) => (o?.name ? `${k}|${o.name}` : k),
  }),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't' } }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('./api', () => api)

const wrapper = ({ children }: PropsWithChildren) => (
  <DocumentsProvider spaceId="s1">{children}</DocumentsProvider>
)

describe('uploading a file the space already has', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getDocumentsStats.mockReturnValue(new Promise(() => undefined))
  })

  it('is a neutral "already there", not a failure, and names the file when it may', async () => {
    api.uploadDocument.mockRejectedValue(
      new ApiError(409, 'bilingual text', 'duplicate_document', {
        code: 'duplicate_document',
        existing_name: 'plan.md',
      }),
    )
    const { result } = renderHook(() => useDocuments(), { wrapper })
    await act(async () => {
      await result.current.upload(new File(['x'], 'copy.md'))
    })
    expect(result.current.uploads[0]).toMatchObject({ status: 'duplicate', existingName: 'plan.md' })
  })

  it('does not name a file the uploader cannot see', async () => {
    api.uploadDocument.mockRejectedValue(
      new ApiError(409, 'bilingual text', 'duplicate_document', { code: 'duplicate_document' }),
    )
    const { result } = renderHook(() => useDocuments(), { wrapper })
    await act(async () => {
      await result.current.upload(new File(['x'], 'copy.md'))
    })
    expect(result.current.uploads[0].status).toBe('duplicate')
    expect(result.current.uploads[0].existingName).toBeUndefined()
  })

  it('still treats any other 409 as a failure', async () => {
    api.uploadDocument.mockRejectedValue(new ApiError(409, 'something else'))
    const { result } = renderHook(() => useDocuments(), { wrapper })
    await act(async () => {
      await result.current.upload(new File(['x'], 'a.md'))
    })
    expect(result.current.uploads[0]).toMatchObject({ status: 'failed', message: 'something else' })
  })

  it('says so on the row, with and without the existing name', async () => {
    api.uploadDocument
      .mockRejectedValueOnce(
        new ApiError(409, 'x', 'duplicate_document', { existing_name: 'plan.md' }),
      )
      .mockRejectedValueOnce(new ApiError(409, 'x', 'duplicate_document', {}))
    const Both = () => {
      const { upload } = useDocuments()
      return (
        <button
          onClick={() => {
            void upload(new File(['x'], 'one.md'))
            void upload(new File(['x'], 'two.md'))
          }}
          type="button"
        >
          go
        </button>
      )
    }
    render(
      <DocumentsProvider spaceId="s1">
        <Both />
        <UploadDropZone />
      </DocumentsProvider>,
    )
    fireEvent.click(screen.getByText('go'))
    await waitFor(() => expect(screen.getByText('documents.upload.duplicate|plan.md')).toBeTruthy())
    expect(screen.getByText('documents.upload.duplicateHidden')).toBeTruthy()
    expect(screen.queryByText('documents.upload.failed')).toBeNull()
  })
})
