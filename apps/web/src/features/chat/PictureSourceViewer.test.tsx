import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { PictureSourceViewer, isPictureSource } from './PictureSourceViewer'
import type { MessageCitation } from './types'

const api = vi.hoisted(() => ({
  downloadDocument: vi.fn(),
  downloadDocumentVersion: vi.fn(),
}))

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ credentials: { sessionToken: 'tok' } }),
}))
vi.mock('../documents/api', () => api)

function citation(over: Partial<MessageCitation> = {}): MessageCitation {
  return {
    index: 1,
    document_id: 'd1',
    space_id: 's1',
    document_name: 'bracket.jpg',
    chunk_index: 1000,
    text: 'XC-001 孔距 240',
    score: 0.5,
    used: true,
    char_start: 0,
    char_end: 0,
    source_kind: 'image_ocr',
    ...over,
  }
}

beforeEach(() => {
  api.downloadDocument.mockReset()
  api.downloadDocumentVersion.mockReset()
  URL.createObjectURL = vi.fn(() => 'blob:picture')
  URL.revokeObjectURL = vi.fn()
})

describe('isPictureSource', () => {
  it('is true for text read from a picture and for a description of one only', () => {
    expect(isPictureSource({ source_kind: 'image_ocr' })).toBe(true)
    expect(isPictureSource({ source_kind: 'image_caption' })).toBe(true)
    expect(isPictureSource({ source_kind: 'text' })).toBe(false)
    expect(isPictureSource({})).toBe(false)
  })
})

describe('PictureSourceViewer', () => {
  it('shows the picture and the cited text, without asking for document text', async () => {
    api.downloadDocument.mockResolvedValue({ blob: new Blob(['x']), filename: 'a.jpg' })
    render(<PictureSourceViewer citation={citation()} onClose={() => {}} />)

    const img = await screen.findByRole('img', { name: 'bracket.jpg' })
    expect(img.getAttribute('src')).toBe('blob:picture')
    expect(screen.getByText('XC-001 孔距 240')).toBeTruthy()
    expect(screen.getByText('chat.citations.pictureTextTitle')).toBeTruthy()
    expect(api.downloadDocument).toHaveBeenCalledWith(expect.anything(), 's1', 'd1')
  })

  it('titles a description as a description, not as text read from the picture', async () => {
    api.downloadDocument.mockResolvedValue({ blob: new Blob(['x']), filename: 'a.jpg' })
    render(
      <PictureSourceViewer
        citation={citation({ source_kind: 'image_caption' })}
        onClose={() => {}}
      />,
    )
    expect(screen.getByText('chat.citations.pictureDescribedTitle')).toBeTruthy()
    expect(screen.queryByText('chat.citations.pictureTextTitle')).toBeNull()
    await waitFor(() => expect(api.downloadDocument).toHaveBeenCalled())
  })

  it('opens the version the passage came from', async () => {
    api.downloadDocumentVersion.mockResolvedValue({ blob: new Blob(['x']), filename: 'a.jpg' })
    render(<PictureSourceViewer citation={citation({ version_number: 1 })} onClose={() => {}} />)
    await waitFor(() =>
      expect(api.downloadDocumentVersion).toHaveBeenCalledWith(expect.anything(), 's1', 'd1', 1),
    )
    expect(api.downloadDocument).not.toHaveBeenCalled()
  })

  it('keeps the cited text when the picture cannot be loaded', async () => {
    api.downloadDocument.mockRejectedValue(new Error('boom'))
    render(<PictureSourceViewer citation={citation()} onClose={() => {}} />)
    expect(await screen.findByText('chat.citations.pictureFailed')).toBeTruthy()
    expect(screen.getByText('XC-001 孔距 240')).toBeTruthy()
  })

  it('fetches nothing and shows no text when the reader can no longer see the source', () => {
    render(
      <PictureSourceViewer
        citation={citation({ text: '', text_hidden: true })}
        onClose={() => {}}
      />,
    )
    expect(screen.getByText('chat.citations.textHidden')).toBeTruthy()
    expect(api.downloadDocument).not.toHaveBeenCalled()
    expect(api.downloadDocumentVersion).not.toHaveBeenCalled()
  })
})
