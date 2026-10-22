import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'

import { fetchQueryPicture, fetchThumbnail, sendImageSearch } from './api'
import { ImageMatchesRail } from './ImageMatches'
import { messageImageMatches, messageImageQuery, messageIsPictureAnswer } from './types'
import type { ImageMatch } from './types'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars ? `${key}:${JSON.stringify(vars)}` : key,
  }),
}))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ credentials: { sessionToken: 'tok' } }),
}))

const credentials = { sessionToken: 'tok' } as never

const MATCHES: ImageMatch[] = [
  {
    document_id: 'd1',
    document_name: 'bracket.png',
    space_id: 's1',
    space_name: 'Parts',
    mime_type: 'image/png',
    size_bytes: 10,
    score: 0.91234,
  },
  {
    document_id: 'd2',
    document_name: 'flange.jpg',
    space_id: 's2',
    space_name: 'Archive',
    mime_type: 'image/jpeg',
    size_bytes: 20,
    score: 0.5,
  },
]

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('sendImageSearch', () => {
  it('posts the picture and words as multipart, with auth and no JSON content type', async () => {
    const reply = { conversation: {}, user_message: {}, assistant_message: {} }
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(reply), { status: 201 }))
    vi.stubGlobal('fetch', fetchMock)
    const file = new File([new Uint8Array([1, 2, 3])], 'q.png', { type: 'image/png' })

    await sendImageSearch(credentials, 'c1', file, '  a bracket  ')

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toMatch(/\/api\/v1\/conversations\/c1\/image-search$/)
    expect(init.method).toBe('POST')
    expect(init.headers.Authorization).toBe('Bearer tok')
    expect(init.headers['Content-Type']).toBeUndefined() // the browser adds the boundary
    const form = init.body as FormData
    expect((form.get('file') as File).name).toBe('q.png')
    expect(form.get('text')).toBe('a bracket')
  })

  it('sends only the words when there is no picture, and omits empty words', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response('{}', { status: 201 }))
    vi.stubGlobal('fetch', fetchMock)

    await sendImageSearch(credentials, 'c1', null, 'red gear')
    await sendImageSearch(
      credentials,
      'c1',
      new File([new Uint8Array([1])], 'q.png', { type: 'image/png' }),
      '   ',
    )

    const first = fetchMock.mock.calls[0][1].body as FormData
    const second = fetchMock.mock.calls[1][1].body as FormData
    expect(first.has('file')).toBe(false)
    expect(first.get('text')).toBe('red gear')
    expect(second.has('file')).toBe(true)
    expect(second.has('text')).toBe(false)
  })

  it("raises the server's reason", async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'too many picture searches; wait a minute' }), {
          status: 429,
        }),
      ),
    )
    const error = await sendImageSearch(credentials, 'c1', null, 'x').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(429)
    expect((error as ApiError).detail).toContain('wait a minute')
  })
})

describe('fetchThumbnail', () => {
  it('fetches the preview with the session header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(new Blob(['jpeg']), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const blob = await fetchThumbnail(credentials, 's1', 'd1')
    expect(blob.size).toBeGreaterThan(0)
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/spaces\/s1\/documents\/d1\/thumbnail$/)
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer tok')
  })

  it('fails when the person may not see the picture', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))
    await expect(fetchThumbnail(credentials, 's1', 'd1')).rejects.toBeInstanceOf(ApiError)
  })
})

describe('related pictures beside a text answer', () => {
  it('says when the text of a picture is also one of the answer sources', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new Blob(['x']), { status: 200 })))
    const { unmount } = render(
      <ImageMatchesRail
        matches={[{ ...MATCHES[0], citation_index: 3 }]}
        railRef={{ current: null }}
        related
      />,
    )
    expect(screen.getByText('chat.imageSearch.textAlsoCited:{"index":3}')).toBeInTheDocument()
    unmount()
    render(<ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} related />)
    expect(screen.queryByText(/textAlsoCited/)).toBeNull()
  })
  it('titles the rail as possibly related, not as the answer', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new Blob(['x']), { status: 200 })))
    const { unmount } = render(
      <ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} related />,
    )
    expect(screen.getByText('chat.imageSearch.relatedTitle')).toBeInTheDocument()
    unmount()
    render(<ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} />)
    expect(screen.getByText('chat.imageSearch.railTitle')).toBeInTheDocument()
  })

  it('tells a picture answer from a text answer that carries pictures', () => {
    expect(messageIsPictureAnswer({ type: 'text', text: 'x', meta: { kind: 'image_search' } })).toBe(true)
    expect(messageIsPictureAnswer({ type: 'text', text: 'x', meta: { kind: 'image_list' } })).toBe(true)
    expect(messageIsPictureAnswer({ type: 'text', text: 'x', image_matches: MATCHES })).toBe(false)
    expect(messageIsPictureAnswer({ type: 'text', text: 'x', meta: { model_id: 'm' } })).toBe(false)
  })
})

describe('fetchQueryPicture', () => {
  it('fetches the kept picture of a question with the session header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(new Blob(['png']), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const blob = await fetchQueryPicture(credentials, 'c1', 'm1')
    expect(blob.size).toBeGreaterThan(0)
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/conversations\/c1\/messages\/m1\/picture$/)
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer tok')
  })

  it('fails when the picture is not the person\'s', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))
    await expect(fetchQueryPicture(credentials, 'c1', 'm1')).rejects.toBeInstanceOf(ApiError)
  })
})

describe('message helpers', () => {
  it('reads found pictures and the question marker, and tolerates other messages', () => {
    expect(messageImageMatches({ type: 'text', text: 'x', image_matches: MATCHES })).toHaveLength(2)
    expect(messageImageMatches({ type: 'text', text: 'x' })).toEqual([])
    expect(messageImageMatches({ type: 'text', image_matches: 'nope' })).toEqual([])
    expect(
      messageImageQuery({ type: 'text', text: '', image_query: { picture: true, description: false } }),
    ).toEqual({ picture: true, description: false, stored: false })
    expect(
      messageImageQuery({
        type: 'text',
        text: '',
        image_query: { picture: true, description: false, stored: true, picture_id: 'p1' },
      }),
    ).toEqual({ picture: true, description: false, stored: true })
    // Words alone never have a picture, whatever the marker says.
    expect(
      messageImageQuery({ type: 'text', text: 'x', image_query: { picture: false, description: true, stored: true } }),
    ).toEqual({ picture: false, description: true, stored: false })
    expect(messageImageQuery({ type: 'text', text: 'hi' })).toBeNull()
  })
})

describe('ImageMatchesRail', () => {
  beforeEach(() => {
    // jsdom has no object URLs; give it counting stand-ins.
    let n = 0
    URL.createObjectURL = vi.fn(() => `blob:fake-${++n}`)
    URL.revokeObjectURL = vi.fn()
  })

  it('shows every found picture with its name, space and similarity, best first', async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(async () => new Response(new Blob(['x']), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<ImageMatchesRail matches={MATCHES} railRef={{ current: null }} />)

    expect(screen.getByText('bracket.png')).toBeInTheDocument()
    expect(screen.getByText('flange.jpg')).toBeInTheDocument()
    expect(screen.getByText('Parts')).toBeInTheDocument()
    expect(screen.getByText('Archive')).toBeInTheDocument()
    expect(screen.getByText(/chat\.imageSearch\.score:.*0\.91/)).toBeInTheDocument()
    const names = screen.getAllByText(/\.(png|jpg)$/).map((el) => el.textContent)
    expect(names).toEqual(['bracket.png', 'flange.jpg'])
    // One preview request per picture, each against its own space.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    const urls = fetchMock.mock.calls.map((c) => c[0] as string)
    expect(urls.some((u) => u.includes('/spaces/s1/documents/d1/thumbnail'))).toBe(true)
    expect(urls.some((u) => u.includes('/spaces/s2/documents/d2/thumbnail'))).toBe(true)
    await waitFor(() => expect(screen.getAllByRole('img')).toHaveLength(2))
  })

  it('shows a picture of a closed space as a placeholder without names and fetches nothing', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(new Blob(['x']), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const closed: ImageMatch = {
      ...MATCHES[0],
      document_name: '',
      space_name: '',
      mime_type: '',
      size_bytes: 0,
      hidden: true,
    }
    render(<ImageMatchesRail matches={[closed, MATCHES[1]]} railRef={{ current: null }} />)
    expect(screen.getByText('chat.imageSearch.hiddenName')).toBeInTheDocument()
    expect(screen.getByLabelText('chat.imageSearch.unavailable')).toBeInTheDocument()
    // The open one beside it is shown as usual.
    expect(screen.getByText('flange.jpg')).toBeInTheDocument()
    expect(screen.getByText('Archive')).toBeInTheDocument()
    // The closed card cannot be opened, and only the open picture is requested.
    const [closedCard, openCard] = screen.getAllByRole('button')
    expect(closedCard).toBeDisabled()
    expect(openCard).toBeEnabled()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect((fetchMock.mock.calls[0][0] as string).includes('/spaces/s2/documents/d2/thumbnail')).toBe(true)
  })

  it('shows a quiet placeholder, not a broken image, when a preview cannot be loaded', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))
    render(<ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} />)
    expect(await screen.findByLabelText('chat.imageSearch.unavailable')).toBeInTheDocument()
    expect(screen.getByText('chat.imageSearch.unavailable')).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
  })

  it('does not claim a picture is gone when the preview failed for another reason', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 500 })))
    render(<ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} />)
    expect(await screen.findByLabelText('chat.imageSearch.noPreview')).toBeInTheDocument()
    expect(screen.queryByText('chat.imageSearch.unavailable')).toBeNull()
  })
  it('says the same in the large view when the picture is gone or no longer visible', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) =>
      url.endsWith('/download')
        ? new Response(JSON.stringify({ detail: 'document not found: d1' }), { status: 404 })
        : new Response(new Blob(['thumb']), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    render(<ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} />)
    fireEvent.click(screen.getByTitle('chat.imageSearch.open'))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('chat.imageSearch.unavailable')).toBeInTheDocument()
    expect(within(dialog).queryByText(/document not found/)).toBeNull()
  })
  it('opens the full picture on click and closes it again', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) =>
      url.endsWith('/download')
        ? new Response(new Blob(['full']), {
            status: 200,
            headers: { 'content-disposition': 'attachment; filename="bracket.png"' },
          })
        : new Response(new Blob(['thumb']), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} />)
    fireEvent.click(screen.getByTitle('chat.imageSearch.open'))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveAccessibleName('bracket.png')
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/spaces/s1/documents/d1/download'))).toBe(true),
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})
