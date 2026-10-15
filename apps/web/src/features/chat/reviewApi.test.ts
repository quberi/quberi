import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  cancelReview,
  previewReview,
  streamApprovedReview,
  streamMessage,
} from './api'
import type { ChatStreamEvent } from './api'

const credentials = { sessionToken: 'tok' } as never

function sse(frames: [string, unknown][]): Response {
  const body = frames.map(([k, d]) => `event: ${k}\ndata: ${JSON.stringify(d)}\n\n`).join('')
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

async function collect(gen: AsyncGenerator<ChatStreamEvent>) {
  const out: ChatStreamEvent[] = []
  for await (const e of gen) out.push(e)
  return out
}

afterEach(() => vi.unstubAllGlobals())

describe('review API calls', () => {
  it('asks the server to hold the prompt only when review is on', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => sse([['done', {}]]))
    vi.stubGlobal('fetch', fetchMock)
    await collect(streamMessage(credentials, 'c1', 'hi', undefined, true))
    await collect(streamMessage(credentials, 'c1', 'hi'))
    const bodies = fetchMock.mock.calls.map((c) => JSON.parse(c[1].body as string))
    expect(bodies).toEqual([
      { text: 'hi', review: true },
      { text: 'hi', review: false },
    ])
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/conversations\/c1\/messages\/stream$/)
  })

  it('turns a review_required frame into a review event', async () => {
    const review = { pending_id: 'p1', question: { text: 'q', spans: [] }, history: [], references: [] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sse([
      ['user_message', { id: 'u1' }],
      ['review_required', review],
      ['done', {}],
    ])))
    const events = await collect(streamMessage(credentials, 'c1', 'hi', undefined, true))
    expect(events.map((e) => e.kind)).toEqual(['user_message', 'review_required', 'done'])
    expect((events[1] as { review: { pending_id: string } }).review.pending_id).toBe('p1')
  })

  it('approves with the exact edited texts, to the approve endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(sse([['delta', { text: 'ok' }], ['done', {}]]))
    vi.stubGlobal('fetch', fetchMock)
    const edits = {
      question: '改过的问题',
      history: ['h'],
      references: [{ index: 1, include: false, text: 't' }],
    }
    const events = await collect(streamApprovedReview(credentials, 'c1', 'p1', edits))
    expect(events.map((e) => e.kind)).toEqual(['delta', 'done'])
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toMatch(/\/conversations\/c1\/messages\/review\/p1\/approve$/)
    expect(JSON.parse(init.body)).toEqual(edits)
  })

  it('surfaces a refused approval as an error with its status, before any event', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: '问题不能为空' }), { status: 422 }),
    ))
    await expect(
      collect(streamApprovedReview(credentials, 'c1', 'p1', { question: '', history: [], references: [] })),
    ).rejects.toMatchObject({ status: 422 })
  })

  it('previews and cancels through their own endpoints', async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    await previewReview(credentials, 'c1', 'p1', { question: 'q', history: [], references: [] })
    await cancelReview(credentials, 'c1', 'p1')
    const urls = fetchMock.mock.calls.map((c) => String(c[0]))
    expect(urls[0]).toMatch(/\/messages\/review\/p1\/preview$/)
    expect(urls[1]).toMatch(/\/messages\/review\/p1\/cancel$/)
    expect(fetchMock.mock.calls.every((c) => c[1].method === 'POST')).toBe(true)
  })
})
