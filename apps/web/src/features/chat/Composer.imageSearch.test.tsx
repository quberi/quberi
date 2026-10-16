import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { MAX_QUERY_PICTURE_BYTES } from './api'
import { Composer } from './ChatPanel'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars ? `${key}:${JSON.stringify(vars)}` : key,
    i18n: { language: 'en-US' },
  }),
}))

const { chat, auth } = vi.hoisted(() => {
  const noop = () => undefined
  return {
    chat: {
      sending: { active: false, error: null as string | null },
      sendUserMessage: vi.fn(async (_text: string) => undefined),
      sendImageSearch: vi.fn(async (_file: File | null, _text: string) => true),
      guestQuota: null,
      currentConversationId: 'c1',
      currentConversation: { id: 'c1', scope_mode: 'auto', agent_choice: null },
      accessibleSpaces: [],
      scopeSources: { kind: 'idle' },
      reloadScopeSources: vi.fn(async () => undefined),
      setScopeMode: vi.fn(noop),
      addScopeSource: vi.fn(noop),
      removeScopeSource: vi.fn(noop),
      setConversationAgent: vi.fn(noop),
      setConversationModel: vi.fn(noop),
      reviewBeforeSend: false,
      setReviewBeforeSend: vi.fn(noop),
      pendingReview: null as unknown,
    },
    // One stable object: a new one each render would re-run the composer's effects forever.
    auth: { credentials: { sessionToken: 'tok' }, isGuest: false },
  }
})
vi.mock('./ChatContext', () => ({ useChat: () => chat }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('../auth/AuthModalContext', () => ({ useAuthModal: () => ({ openAuth: vi.fn() }) }))
vi.mock('../settings/api', () => ({
  getModelSelections: vi.fn(async () => ({ chat: { model_id: null, binding_id: null } })),
  getUsage: vi.fn(async () => ({ metered: false, limit: 0, remaining: 0 })),
  listUsableChatModels: vi.fn(async () => ({ items: [] })),
}))
vi.mock('../agent/api', () => ({ listAgents: vi.fn(async () => ({ items: [] })) }))

function picture(name = 'q.png', type = 'image/png', size = 100): File {
  const file = new File([new Uint8Array(1)], name, { type })
  Object.defineProperty(file, 'size', { value: size })
  return file
}

function textbox() {
  return screen.getByRole('textbox') as HTMLTextAreaElement
}

function pickWithDialog(file: File) {
  fireEvent.click(screen.getByLabelText('chat.attach.label'))
  const input = screen.getByLabelText('chat.imageSearch.pick', { selector: 'input' })
  fireEvent.change(input, { target: { files: [file] } })
}

beforeEach(() => {
  auth.isGuest = false
  chat.sending = { active: false, error: null }
  chat.pendingReview = null
  chat.sendUserMessage.mockClear()
  chat.sendImageSearch.mockClear()
  chat.sendImageSearch.mockResolvedValue(true)
  let n = 0
  URL.createObjectURL = vi.fn(() => `blob:fake-${++n}`)
  URL.revokeObjectURL = vi.fn()
})

describe('Composer picture search', () => {
  it('searches with a picked picture and the typed words, then clears itself', async () => {
    render(<Composer />)
    const file = picture()
    pickWithDialog(file)
    expect(screen.getByText('q.png')).toBeInTheDocument()
    expect(screen.getByText('chat.imageSearch.notSaved')).toBeInTheDocument()

    fireEvent.change(textbox(), { target: { value: ' the flange ' } })
    fireEvent.click(screen.getByLabelText('chat.send'))

    await waitFor(() => expect(chat.sendImageSearch).toHaveBeenCalledWith(file, 'the flange'))
    expect(chat.sendUserMessage).not.toHaveBeenCalled() // not a chat question
    await waitFor(() => expect(screen.queryByText('q.png')).toBeNull())
    expect(textbox().value).toBe('')
  })

  it('can send a picture alone', async () => {
    render(<Composer />)
    const file = picture()
    pickWithDialog(file)
    fireEvent.click(screen.getByLabelText('chat.send'))
    await waitFor(() => expect(chat.sendImageSearch).toHaveBeenCalledWith(file, ''))
  })

  it('keeps the picture and words when the search fails, so it can be retried', async () => {
    chat.sendImageSearch.mockResolvedValue(false)
    render(<Composer />)
    pickWithDialog(picture())
    fireEvent.change(textbox(), { target: { value: 'gear' } })
    fireEvent.click(screen.getByLabelText('chat.send'))
    await waitFor(() => expect(chat.sendImageSearch).toHaveBeenCalled())
    expect(screen.getByText('q.png')).toBeInTheDocument()
    expect(textbox().value).toBe('gear')
  })

  it('searches by description alone and needs some words to do so', async () => {
    render(<Composer />)
    fireEvent.click(screen.getByLabelText('chat.attach.label'))
    fireEvent.click(screen.getByText('chat.imageSearch.byWordsTitle'))
    expect(screen.getByLabelText('chat.send')).toBeDisabled()

    fireEvent.change(textbox(), { target: { value: 'red valve' } })
    expect(screen.getByLabelText('chat.send')).toBeEnabled()
    fireEvent.click(screen.getByLabelText('chat.send'))
    await waitFor(() => expect(chat.sendImageSearch).toHaveBeenCalledWith(null, 'red valve'))
  })

  it('refuses a file that is not a picture and one that is too big', () => {
    render(<Composer />)
    pickWithDialog(picture('notes.pdf', 'application/pdf'))
    expect(screen.getByText('chat.imageSearch.notPicture')).toBeInTheDocument()
    expect(screen.queryByText('notes.pdf')).toBeNull()

    pickWithDialog(picture('huge.png', 'image/png', MAX_QUERY_PICTURE_BYTES + 1))
    expect(screen.getByText(/chat\.imageSearch\.tooBig/)).toBeInTheDocument()
    expect(screen.queryByText('huge.png')).toBeNull()
  })

  it('takes a pasted picture, but leaves pasted text alone', () => {
    render(<Composer />)
    const file = picture('shot.png')
    fireEvent.paste(textbox(), { clipboardData: { files: [file], types: ['Files'] } })
    expect(screen.getByText('shot.png')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('chat.imageSearch.remove'))
    expect(screen.queryByText('shot.png')).toBeNull()

    fireEvent.paste(textbox(), { clipboardData: { files: [], types: ['text/plain'] } })
    expect(screen.queryByLabelText('chat.imageSearch.remove')).toBeNull()
  })

  it('takes a dropped picture', () => {
    render(<Composer />)
    const file = picture('dropped.png')
    const form = textbox().closest('form') as HTMLFormElement
    fireEvent.drop(form, { dataTransfer: { files: [file], types: ['Files'] } })
    expect(screen.getByText('dropped.png')).toBeInTheDocument()
  })

  it('still sends an ordinary question as chat when no picture is involved', async () => {
    render(<Composer />)
    fireEvent.change(textbox(), { target: { value: 'what is our leave policy?' } })
    fireEvent.click(screen.getByLabelText('chat.send'))
    await waitFor(() => expect(chat.sendUserMessage).toHaveBeenCalledWith('what is our leave policy?'))
    expect(chat.sendImageSearch).not.toHaveBeenCalled()
  })

  it('does not let a held prompt block a picture search', async () => {
    chat.pendingReview = { conversationId: 'c1', review: {} }
    render(<Composer />)
    pickWithDialog(picture())
    expect(screen.getByLabelText('chat.send')).toBeEnabled()
  })

  it('offers guests no picture search: no pasting', () => {
    auth.isGuest = true
    render(<Composer />)
    expect(screen.getByLabelText('chat.attach.label')).toBeDisabled()
    fireEvent.paste(textbox(), { clipboardData: { files: [picture('shot.png')], types: ['Files'] } })
    expect(screen.queryByText('shot.png')).toBeNull()
  })
})
