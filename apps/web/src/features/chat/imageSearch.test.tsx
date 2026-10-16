import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'

import { fetchThumbnail, sendImageSearch } from './api'
import { ImageMatchesRail } from './ImageMatches'
import { messageImageMatches, messageImageQuery } from './types'
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

describe('message helpers', () => {
  it('reads found pictures and the question marker, and tolerates other messages', () => {
    expect(messageImageMatches({ type: 'text', text: 'x', image_matches: MATCHES })).toHaveLength(2)
    expect(messageImageMatches({ type: 'text', text: 'x' })).toEqual([])
    expect(messageImageMatches({ type: 'text', image_matches: 'nope' })).toEqual([])
    expect(
      messageImageQuery({ type: 'text', text: '', image_query: { picture: true, description: false } }),
    ).toEqual({ picture: true, description: false })
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

  it('shows a quiet placeholder, not a broken image, when a preview cannot be loaded', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))
    render(<ImageMatchesRail matches={[MATCHES[0]]} railRef={{ current: null }} />)
    expect(await screen.findByLabelText('chat.imageSearch.noPreview')).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
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
