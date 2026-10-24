import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatPanel } from './ChatPanel'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: 'zh-CN', resolvedLanguage: 'zh-CN' },
  }),
}))

const { chat, auth } = vi.hoisted(() => {
  const noop = () => undefined
  const chat = {
    anchorSpace: { id: 's1', name: 'Guest space' },
    accessibleSpaces: [{ id: 's1', name: 'Guest space' }],
    conversations: { kind: 'ready', items: [] },
    messages: { kind: 'ready', items: [] as unknown[] },
    sending: { active: false, error: null as string | null },
    guestQuota: null,
    currentConversationId: 'c1',
    currentConversation: { id: 'c1', scope_mode: 'auto', agent_choice: null },
    scopeSources: { kind: 'idle' },
    reviewBeforeSend: false,
    searchMode: 'text',
    pendingReview: null as unknown,
    sendUserMessage: vi.fn(async () => undefined),
    sendImageSearch: vi.fn(async () => true),
    reloadScopeSources: vi.fn(async () => undefined),
    selectConversation: vi.fn(noop),
    renameConversation: vi.fn(noop),
    deleteConversation: vi.fn(noop),
    createConversation: vi.fn(noop),
    setScopeMode: vi.fn(noop),
    addScopeSource: vi.fn(noop),
    removeScopeSource: vi.fn(noop),
    setConversationAgent: vi.fn(noop),
    setConversationModel: vi.fn(noop),
    setReviewBeforeSend: vi.fn(noop),
    setSearchMode: vi.fn(noop),
  }
  return { chat, auth: { credentials: { sessionToken: 'tok', isGuest: true }, isGuest: true } }
})

vi.mock('./ChatContext', () => ({ useChat: () => chat }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('../auth/AuthModalContext', () => ({ useAuthModal: () => ({ openAuth: vi.fn() }) }))
vi.mock('./ScopeChipStrip', () => ({ ScopeChipStrip: () => null }))
vi.mock('./guestApi', () => ({ getGuestStarters: vi.fn(async () => ({ items: [] })) }))
vi.mock('../settings/api', () => ({
  getModelSelections: vi.fn(async () => ({ chat: { model_id: null, binding_id: null } })),
  getUsage: vi.fn(async () => ({ metered: false, limit: 0, remaining: 0 })),
  listUsableChatModels: vi.fn(async () => ({ items: [] })),
}))
vi.mock('../agent/api', () => ({ listAgents: vi.fn(async () => ({ items: [] })) }))

const message = (role: 'user' | 'assistant', id: string) => ({
  id,
  role,
  content: 'hello',
  created_at: '2026-10-19T00:00:00Z',
})

beforeEach(() => {
  auth.isGuest = true
  auth.credentials = { sessionToken: 'tok', isGuest: true }
  chat.messages = { kind: 'ready', items: [message('assistant', 'm0')] }
})

describe('the guest chat page', () => {
  it('puts the advantages above the chat and drops the page header the strip replaces', () => {
    const { container } = render(<ChatPanel />)
    expect(screen.getByRole('heading', { level: 2 }).textContent).toContain('chat.guestAdv.title')
    expect(screen.getAllByRole('listitem').length).toBeGreaterThanOrEqual(8)
    expect(container.querySelector('.chat-panel-header')).toBeNull()
    const strip = container.querySelector('.guest-adv')
    const panel = container.querySelector('.chat-panel')
    expect(strip && panel && strip.compareDocumentPosition(panel)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
  })

  it('keeps all eight chips before the guest has asked anything, even with a welcome message', () => {
    const { container } = render(<ChatPanel />)
    expect(container.querySelectorAll('.guest-adv-chip')).toHaveLength(8)
  })

  it('folds the advantages to one line once the guest has asked something', () => {
    chat.messages = { kind: 'ready', items: [message('assistant', 'm0'), message('user', 'm1')] }
    const { container } = render(<ChatPanel />)
    expect(container.querySelector('.guest-adv-collapsed')).not.toBeNull()
    expect(container.querySelectorAll('.guest-adv-chip')).toHaveLength(0)
  })

  it('does not fold while the thread is still loading', () => {
    chat.messages = { kind: 'loading' } as unknown as typeof chat.messages
    const { container } = render(<ChatPanel />)
    expect(container.querySelectorAll('.guest-adv-chip')).toHaveLength(8)
  })
})

describe('the signed-in chat page', () => {
  beforeEach(() => {
    auth.isGuest = false
    auth.credentials = { sessionToken: 'tok', isGuest: false }
  })

  it('shows no advantages and keeps its own header', () => {
    const { container } = render(<ChatPanel />)
    expect(container.querySelector('.guest-adv')).toBeNull()
    expect(container.querySelector('.chat-panel-header')).not.toBeNull()
    expect(screen.queryByText('chat.guestAdv.title')).toBeNull()
  })
})
