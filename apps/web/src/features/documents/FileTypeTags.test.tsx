import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { FileTypeTags } from './SpaceKnowledgeView'
import type { ListDocumentsParams } from './types'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'en-US' } }),
}))

const { docs } = vi.hoisted(() => ({
  docs: {
    params: {} as ListDocumentsParams,
    setParams: vi.fn(),
    stats: {
      kind: 'ready',
      stats: { by_mime_group: { image: 3, pdf: 5 } as Record<string, number> },
    } as unknown,
  },
}))
vi.mock('./DocumentsContext', () => ({
  useDocuments: () => docs,
  DocumentsProvider: ({ children }: { children: unknown }) => children,
}))
vi.mock('../spaces/SpacesContext', () => ({ useSpaces: () => ({ state: { kind: 'idle' } }) }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ isGuest: false }) }))
vi.mock('../auth/AuthModalContext', () => ({ useAuthModal: () => ({ openAuth: vi.fn() }) }))

/** What the next filter state would be after the click, applied to the current params. */
function nextParams(): ListDocumentsParams {
  const updater = docs.setParams.mock.calls.at(-1)![0] as (p: ListDocumentsParams) => ListDocumentsParams
  return updater(docs.params)
}

beforeEach(() => {
  docs.params = { offset: 50 }
  docs.setParams.mockClear()
  docs.stats = { kind: 'ready', stats: { by_mime_group: { image: 3, pdf: 5 } } }
})

describe('FileTypeTags', () => {
  it('offers All plus only the kinds the space holds, with counts', () => {
    render(<FileTypeTags />)
    expect(screen.getByText('knowledge.view.fileType.all')).toBeInTheDocument()
    expect(screen.getByText('knowledge.view.fileType.image')).toBeInTheDocument()
    expect(screen.getByText('knowledge.view.fileType.pdf')).toBeInTheDocument()
    expect(screen.queryByText('knowledge.view.fileType.spreadsheet')).toBeNull()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).toBeNull() // tags, not a dropdown
  })

  it('picking a tag filters by it and goes back to the first page', () => {
    render(<FileTypeTags />)
    fireEvent.click(screen.getByText('knowledge.view.fileType.image'))
    expect(nextParams()).toMatchObject({ file_type: 'image', offset: 0 })
  })

  it('shows the active tag as pressed; clicking it again, or All, clears the filter', () => {
    docs.params = { file_type: 'image', offset: 0 }
    render(<FileTypeTags />)
    const image = screen.getByText('knowledge.view.fileType.image').closest('button')!
    expect(image).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('knowledge.view.fileType.all').closest('button')).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    fireEvent.click(image)
    expect(nextParams().file_type).toBeUndefined()
    fireEvent.click(screen.getByText('knowledge.view.fileType.all'))
    expect(nextParams().file_type).toBeUndefined()
  })

  it('keeps the selected kind visible even if the space no longer holds any', () => {
    docs.params = { file_type: 'word' }
    render(<FileTypeTags />)
    expect(screen.getByText('knowledge.view.fileType.word')).toBeInTheDocument()
  })

  it('shows nothing for an empty space', () => {
    docs.stats = { kind: 'ready', stats: { by_mime_group: {} } }
    const { container } = render(<FileTypeTags />)
    expect(container).toBeEmptyDOMElement()
  })
})
