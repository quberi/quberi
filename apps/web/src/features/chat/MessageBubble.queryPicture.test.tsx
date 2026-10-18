import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MessageBubble } from './ChatPanel'
import { forgetQueryPictures, rememberQueryPicture } from './queryPictures'
import type { MessageSummary } from './types'

const api = vi.hoisted(() => ({ fetchQueryPicture: vi.fn() }))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}))
const auth = vi.hoisted(() => ({
  credentials: { sessionToken: 'tok', isGuest: false } as { sessionToken: string; isGuest: boolean } | null,
}))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('../../i18n/formatDate', () => ({ useFormatDate: () => () => '10:00' }))
vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  fetchQueryPicture: api.fetchQueryPicture,
}))

function question(imageQuery: Record<string, unknown>): MessageSummary {
  return {
    id: 'm1',
    conversation_id: 'c1',
    role: 'user',
    content: { type: 'text', text: '', image_query: imageQuery },
    author_user_id: 'u1',
    status: 'completed',
    correlation_id: null,
    created_at: '2026-01-01T00:00:00Z',
  } as MessageSummary
}

describe('a picture-search question in the thread', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.credentials = { sessionToken: 'tok', isGuest: false }
    let n = 0
    vi.stubGlobal('URL', { createObjectURL: () => `blob:t/${++n}`, revokeObjectURL: () => undefined })
  })
  afterEach(() => {
    forgetQueryPictures()
    vi.unstubAllGlobals()
  })

  it('fetches and shows the picture the server kept', async () => {
    api.fetchQueryPicture.mockResolvedValue(new Blob(['png']))
    render(
      <MessageBubble
        message={question({ picture: true, description: false, stored: true, picture_id: 'p1' })}
        onCiteClick={() => undefined}
      />,
    )
    const img = await screen.findByRole('img', { name: 'chat.imageSearch.queryPictureAlt' })
    expect(img.getAttribute('src')).toBe('blob:t/1')
    expect(api.fetchQueryPicture).toHaveBeenCalledWith(auth.credentials, 'c1', 'm1')
    expect(screen.getByText('chat.imageSearch.askedWithPictureKept')).toBeTruthy()
  })

  it('shows the file just sent without asking the server', async () => {
    act(() => {
      rememberQueryPicture('m1', new File(['x'], 'q.png'))
    })
    render(
      <MessageBubble
        message={question({ picture: true, description: false, stored: true, picture_id: 'p1' })}
        onCiteClick={() => undefined}
      />,
    )
    await screen.findByRole('img', { name: 'chat.imageSearch.queryPictureAlt' })
    expect(api.fetchQueryPicture).not.toHaveBeenCalled()
  })

  it('says so, and shows no picture, for questions asked before pictures were kept', async () => {
    render(
      <MessageBubble
        message={question({ picture: true, description: false })}
        onCiteClick={() => undefined}
      />,
    )
    expect(screen.getByText('chat.imageSearch.askedWithPicture')).toBeTruthy()
    expect(screen.queryByRole('img')).toBeNull()
    expect(api.fetchQueryPicture).not.toHaveBeenCalled()
  })

  it('keeps the label and drops the picture when it cannot be fetched', async () => {
    api.fetchQueryPicture.mockRejectedValue(new Error('404'))
    render(
      <MessageBubble
        message={question({ picture: true, description: false, stored: true, picture_id: 'p1' })}
        onCiteClick={() => undefined}
      />,
    )
    await waitFor(() => expect(api.fetchQueryPicture).toHaveBeenCalled())
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText('chat.imageSearch.askedWithPictureKept')).toBeTruthy()
  })

  it('does not fetch for a guest', () => {
    auth.credentials = { sessionToken: 'tok', isGuest: true }
    render(
      <MessageBubble
        message={question({ picture: true, description: false, stored: true, picture_id: 'p1' })}
        onCiteClick={() => undefined}
      />,
    )
    expect(api.fetchQueryPicture).not.toHaveBeenCalled()
  })

  it('offers pictures that ride along with a text answer as possibly related', () => {
    const answer = {
      id: 'a1',
      conversation_id: 'c1',
      role: 'assistant',
      content: {
        type: 'text',
        text: 'nothing found',
        image_matches: [
          { document_id: 'd1', document_name: 'lioness.jpg', space_id: 's1', space_name: 'tech', mime_type: 'image/jpeg', size_bytes: 1, score: 0.4 },
        ],
      },
      author_user_id: null,
      status: 'completed',
      correlation_id: null,
      created_at: '2026-01-01T00:00:00Z',
    } as unknown as MessageSummary
    render(<MessageBubble message={answer} onCiteClick={() => undefined} />)
    expect(screen.getByText('chat.imageSearch.showRelated')).toBeTruthy()
    expect(screen.queryByText('chat.imageSearch.showPictures')).toBeNull()
  })

  it('announces the pictures of a picture answer as the answer', () => {
    const answer = {
      id: 'a2',
      conversation_id: 'c1',
      role: 'assistant',
      content: {
        type: 'text',
        text: 'found 1',
        meta: { kind: 'image_search' },
        image_matches: [
          { document_id: 'd1', document_name: 'lioness.jpg', space_id: 's1', space_name: 'tech', mime_type: 'image/jpeg', size_bytes: 1, score: 0.4 },
        ],
      },
      author_user_id: null,
      status: 'completed',
      correlation_id: null,
      created_at: '2026-01-01T00:00:00Z',
    } as unknown as MessageSummary
    render(<MessageBubble message={answer} onCiteClick={() => undefined} />)
    expect(screen.getByText('chat.imageSearch.showPictures')).toBeTruthy()
    expect(screen.queryByText('chat.imageSearch.showRelated')).toBeNull()
  })
})
