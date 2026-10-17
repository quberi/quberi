import { act, renderHook, waitFor } from '@testing-library/react'
import type { PropsWithChildren } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import type { ChatStreamEvent, ReviewEditsBody, ReviewPayload } from './api'
import { ChatProvider, useChat } from './ChatContext'

const api = vi.hoisted(() => ({
  streamMessage: vi.fn(),
  streamApprovedReview: vi.fn(),
  cancelReview: vi.fn(),
  previewReview: vi.fn(),
}))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { resolvedLanguage: 'en-US' } }),
}))
const credentials = { sessionToken: 'tok', isGuest: false }
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ credentials }) }))
const spaces = { state: { kind: 'ready', items: [{ id: 's1', preset: 'personal', slug: 's1', name: 'S' }] } }
vi.mock('../spaces/SpacesContext', () => ({ useSpaces: () => spaces }))
vi.mock('./api', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api')>()
  const conv = { id: 'c1', message_count: 0, title: 't', updated_at: '2026-01-01T00:00:00Z', last_message_at: null }
  return {
    ...original,
    listConversations: async () => ({ items: [conv] }),
    listMessages: async () => ({ items: [], conversation: conv }),
    listScopeSources: async () => ({ items: [] }),
    streamMessage: api.streamMessage,
    streamApprovedReview: api.streamApprovedReview,
    cancelReview: api.cancelReview,
    previewReview: api.previewReview,
  }
})

const wrapper = ({ children }: PropsWithChildren) => <ChatProvider>{children}</ChatProvider>

async function* events(list: ChatStreamEvent[]) {
  for (const e of list) yield e
}

const userRow = { id: 'u1', conversation_id: 'c1', role: 'user', content: { type: 'text', text: 'hi' }, status: 'completed' }
const answerRow = { id: 'a1', conversation_id: 'c1', role: 'assistant', content: { type: 'text', text: 'answer' }, status: 'completed' }
const held: ReviewPayload = {
  pending_id: 'p1', question: { text: 'hi', spans: [] }, history: [], references: [],
  messages: [], edited_parts: 0, excluded_references: 0, sensitive_count: 0,
}
const edits: ReviewEditsBody = { question: 'hi', history: [], references: [] }

async function ready() {
  const hook = renderHook(() => useChat(), { wrapper })
  await waitFor(() => expect(hook.result.current.messages.kind).toBe('ready'))
  await waitFor(() => expect(hook.result.current.currentConversation?.id).toBe('c1'))
  return hook
}
const rows = (h: { result: { current: ReturnType<typeof useChat> } }) =>
  h.result.current.messages.kind === 'ready' ? h.result.current.messages.items : []
const count = (h: { result: { current: ReturnType<typeof useChat> } }) =>
  h.result.current.currentConversation?.message_count

describe('ChatContext: check before sending', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
  })

  it('sends normally when the check is off, without asking the server to hold anything', async () => {
    api.streamMessage.mockImplementation(() => events([
      { kind: 'user_message', message: userRow as never },
      { kind: 'delta', text: 'ans' },
      { kind: 'assistant_message', message: answerRow as never },
      { kind: 'done' },
    ]))
    const h = await ready()
    await act(async () => { await h.result.current.sendUserMessage('hi') })
    expect(api.streamMessage.mock.calls[0][4]).toBe(false)
    expect(rows(h).map((m) => m.id)).toEqual(['u1', 'a1'])
    expect(h.result.current.pendingReview).toBeNull()
    expect(count(h)).toBe(2)
  })

  it('shows a picture answer at once even with the check on: no hold, no streamed text', async () => {
    const pictures = { ...answerRow, content: { type: 'text', text: '知识库里共有 1 张图片。', image_matches: [{ document_id: 'd1' }] } }
    api.streamMessage.mockImplementation(() => events([
      { kind: 'user_message', message: userRow as never },
      { kind: 'assistant_message', message: pictures as never },
      { kind: 'done' },
    ]))
    const h = await ready()
    act(() => h.result.current.setReviewBeforeSend(true))
    await act(async () => { await h.result.current.sendUserMessage('库里有哪些图片') })
    expect(rows(h).map((m) => m.id)).toEqual(['u1', 'a1'])
    expect(rows(h)[1].status).toBe('completed')
    expect(h.result.current.pendingReview).toBeNull()
    expect(count(h)).toBe(2)
  })

  it('holds the prompt: no answer row, a pending review, and no count change yet', async () => {
    api.streamMessage.mockImplementation(() => events([
      { kind: 'user_message', message: userRow as never },
      { kind: 'review_required', review: held },
      { kind: 'done' },
    ]))
    const h = await ready()
    act(() => h.result.current.setReviewBeforeSend(true))
    await act(async () => { await h.result.current.sendUserMessage('hi') })
    expect(api.streamMessage.mock.calls[0][4]).toBe(true)
    expect(rows(h).map((m) => m.id)).toEqual(['u1'])
    expect(h.result.current.pendingReview?.review.pending_id).toBe('p1')
    expect(count(h)).toBe(0)
    expect(h.result.current.sending.active).toBe(false)

    // Another question is not sent while this one is undecided.
    await act(async () => { await h.result.current.sendUserMessage('second') })
    expect(api.streamMessage).toHaveBeenCalledTimes(1)
  })

  it('approving streams the reply into the thread and clears the held prompt', async () => {
    api.streamMessage.mockImplementation(() => events([
      { kind: 'user_message', message: userRow as never },
      { kind: 'review_required', review: held },
      { kind: 'done' },
    ]))
    api.streamApprovedReview.mockImplementation(() => events([
      { kind: 'delta', text: 'ans' },
      { kind: 'assistant_message', message: answerRow as never },
      { kind: 'done' },
    ]))
    const h = await ready()
    act(() => h.result.current.setReviewBeforeSend(true))
    await act(async () => { await h.result.current.sendUserMessage('hi') })
    await act(async () => { await h.result.current.approveReview(edits) })

    expect(api.streamApprovedReview.mock.calls[0].slice(1, 4)).toEqual(['c1', 'p1', edits])
    expect(rows(h).map((m) => m.id)).toEqual(['u1', 'a1'])
    expect(h.result.current.pendingReview).toBeNull()
    expect(count(h)).toBe(2)
  })

  it('a refused edit brings the card back with the reason, and shows no answer', async () => {
    api.streamMessage.mockImplementation(() => events([
      { kind: 'user_message', message: userRow as never },
      { kind: 'review_required', review: held },
      { kind: 'done' },
    ]))
    api.streamApprovedReview.mockImplementation(() => {
      // eslint-disable-next-line require-yield
      return (async function* () { throw new ApiError(422, '问题不能为空') })()
    })
    const h = await ready()
    act(() => h.result.current.setReviewBeforeSend(true))
    await act(async () => { await h.result.current.sendUserMessage('hi') })
    await act(async () => { await h.result.current.approveReview({ ...edits, question: ' ' }) })

    expect(h.result.current.pendingReview?.review.pending_id).toBe('p1')
    expect(h.result.current.reviewError).toBe('问题不能为空')
    expect(rows(h).map((m) => m.id)).toEqual(['u1'])
    expect(h.result.current.sending.error).toBeNull()
  })

  it('an expired prompt closes the card and reports it', async () => {
    api.streamMessage.mockImplementation(() => events([
      { kind: 'user_message', message: userRow as never },
      { kind: 'review_required', review: held },
      { kind: 'done' },
    ]))
    api.streamApprovedReview.mockImplementation(() =>
      (async function* () { throw new ApiError(404, 'expired') })())
    const h = await ready()
    act(() => h.result.current.setReviewBeforeSend(true))
    await act(async () => { await h.result.current.sendUserMessage('hi') })
    await act(async () => { await h.result.current.approveReview(edits) })
    expect(h.result.current.pendingReview).toBeNull()
    expect(h.result.current.sending.error).toBe('expired')
  })

  it('cancelling adds the note and never starts a reply', async () => {
    api.streamMessage.mockImplementation(() => events([
      { kind: 'user_message', message: userRow as never },
      { kind: 'review_required', review: held },
      { kind: 'done' },
    ]))
    api.cancelReview.mockResolvedValue({
      ...answerRow, id: 'n1', content: { type: 'text', text: 'x', meta: { review: { cancelled: true } } },
    })
    const h = await ready()
    act(() => h.result.current.setReviewBeforeSend(true))
    await act(async () => { await h.result.current.sendUserMessage('hi') })
    await act(async () => { await h.result.current.cancelReview() })
    expect(api.streamApprovedReview).not.toHaveBeenCalled()
    expect(rows(h).map((m) => m.id)).toEqual(['u1', 'n1'])
    expect(h.result.current.pendingReview).toBeNull()
    expect(count(h)).toBe(2)
  })

  it('remembers the choice in this browser', async () => {
    const h = await ready()
    expect(h.result.current.reviewBeforeSend).toBe(false)
    act(() => h.result.current.setReviewBeforeSend(true))
    expect(window.localStorage.getItem('quberi.reviewBeforeSend')).toBe('1')
    const again = await ready()
    expect(again.result.current.reviewBeforeSend).toBe(true)
  })
})
