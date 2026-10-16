import { act, renderHook, waitFor } from '@testing-library/react'
import type { PropsWithChildren } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'

import { ChatProvider, useChat } from './ChatContext'

const api = vi.hoisted(() => ({ sendImageSearch: vi.fn() }))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { resolvedLanguage: 'en-US' } }),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 'tok', isGuest: false } }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
const spaces = { state: { kind: 'ready', items: [{ id: 's1', preset: 'personal', slug: 's1', name: 'S' }] } }
vi.mock('../spaces/SpacesContext', () => ({ useSpaces: () => spaces }))
const conv = { id: 'c1', message_count: 0, title: 't', updated_at: '2026-01-01T00:00:00Z', last_message_at: null }
vi.mock('./api', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api')>()
  return {
    ...original,
    listConversations: async () => ({ items: [conv] }),
    listMessages: async () => ({ items: [], conversation: conv }),
    listScopeSources: async () => ({ items: [] }),
    sendImageSearch: api.sendImageSearch,
  }
})

const wrapper = ({ children }: PropsWithChildren) => <ChatProvider>{children}</ChatProvider>
const question = { id: 'u1', conversation_id: 'c1', role: 'user', content: { type: 'text', text: '', image_query: { picture: true, description: false } }, status: 'completed' }
const answer = {
  id: 'a1', conversation_id: 'c1', role: 'assistant', status: 'completed',
  content: { type: 'text', text: 'found', image_matches: [{ document_id: 'd1' }], meta: { kind: 'image_search' } },
}
const reply = { conversation: { ...conv, message_count: 2 }, user_message: question, assistant_message: answer }
const file = new File([new Uint8Array(1)], 'q.png', { type: 'image/png' })

async function ready() {
  const hook = renderHook(() => useChat(), { wrapper })
  await waitFor(() => expect(hook.result.current.messages.kind).toBe('ready'))
  await waitFor(() => expect(hook.result.current.currentConversation?.id).toBe('c1'))
  return hook
}
const ids = (h: Awaited<ReturnType<typeof ready>>) =>
  h.result.current.messages.kind === 'ready' ? h.result.current.messages.items.map((m) => m.id) : []

describe('ChatContext: picture search', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    auth.credentials = { sessionToken: 'tok', isGuest: false }
  })

  it('adds the question and the answer to the thread and updates the conversation', async () => {
    api.sendImageSearch.mockResolvedValue(reply)
    const h = await ready()
    let ok = false
    await act(async () => { ok = await h.result.current.sendImageSearch(file, 'gear') })
    expect(ok).toBe(true)
    expect(api.sendImageSearch).toHaveBeenCalledWith(auth.credentials, 'c1', file, 'gear')
    expect(ids(h)).toEqual(['u1', 'a1'])
    expect(h.result.current.currentConversation?.message_count).toBe(2)
    expect(h.result.current.sending).toEqual({ active: false, error: null })
  })

  it('reports the reason and adds nothing when the search is refused', async () => {
    api.sendImageSearch.mockRejectedValue(new ApiError(429, 'too many picture searches; wait a minute'))
    const h = await ready()
    let ok = true
    await act(async () => { ok = await h.result.current.sendImageSearch(file, '') })
    expect(ok).toBe(false)
    expect(ids(h)).toEqual([])
    expect(h.result.current.sending).toEqual({ active: false, error: 'too many picture searches; wait a minute' })
  })

  it('does nothing without a picture or words, and never for a guest', async () => {
    const h = await ready()
    await act(async () => { expect(await h.result.current.sendImageSearch(null, '   ')).toBe(false) })
    expect(api.sendImageSearch).not.toHaveBeenCalled()

    const g = renderHook(() => useChat(), { wrapper })
    auth.credentials = { sessionToken: 'tok', isGuest: true }
    g.rerender()
    await act(async () => { expect(await g.result.current.sendImageSearch(file, 'x')).toBe(false) })
    expect(api.sendImageSearch).not.toHaveBeenCalled()
  })
})
