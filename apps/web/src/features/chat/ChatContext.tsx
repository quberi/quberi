import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useSpaces } from '../spaces/SpacesContext'
import { isWholeSpace, type SpaceSummary } from '../spaces/types'

import {
  addScopeSource as apiAddScopeSource,
  createConversation as apiCreate,
  listConversations as apiListConversations,
  listMessages as apiListMessages,
  listScopeSources as apiListScopeSources,
  removeScopeSource as apiRemoveScopeSource,
  setConversationModel as apiSetConversationModel,
  setConversationAgent as apiSetConversationAgent,
  renameConversation as apiRenameConversation,
  cancelReview as apiCancelReview,
  previewReview as apiPreviewReview,
  sendImageSearch as apiSendImageSearch,
  streamApprovedReview,
  streamMessage as apiStreamMessage,
  updateScopeMode as apiUpdateScopeMode,
} from './api'
import type { ChatStreamEvent, ReviewEditsBody, ReviewPayload } from './api'
import { getGuestQuota, streamGuestAsk } from './guestApi'
import { forgetQueryPictures, rememberQueryPicture } from './queryPictures'
import type { GuestQuota, GuestTurn } from './guestApi'
import { SEARCH_MODES } from './types'
import type {
  AgentTraceStep,
  ConversationSummary,
  MessageCitation,
  MessageSummary,
  ScopeMode,
  ScopeSource,
  SearchMode,
} from './types'

/**
 * ``ChatContext`` holds two things:
 *
 *  * ``conversations`` for the *currently selected space*
 *  * ``messages``      for the *currently selected conversation*
 *
 * Both re-fetch when their owning key changes (space slug and conversation
 * id respectively). All mutations return the fresh row so we can optimistic-
 * update in place without a full round-trip.
 */

type ListState<T> =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: T[] }

interface SendingState {
  active: boolean
  error: string | null
}

interface ChatContextValue {
  // The Chat panel's default anchor for **new** conversations. Existing
  // conversations retain their own space; the sidebar shows every
  // conversation the caller can see, across all accessible spaces.
  // Defaults to the first accessible space. The "New conversation"
  // dialog lets the user override the anchor per-thread.
  anchorSpace: SpaceSummary | null
  setAnchorSpaceId: (id: string) => void

  conversations: ListState<ConversationSummary>
  currentConversationId: string | null
  currentConversation: ConversationSummary | null
  selectConversation: (id: string | null) => void
  /**
   * Create a new conversation. If ``spaceId`` is omitted the default
   * anchor is used; passing a value lets the caller override it (e.g.
   * from the "new conversation" dialog picker).
   */
  createNewConversation: (spaceId?: string) => Promise<ConversationSummary>
  reloadConversations: () => Promise<void>
  accessibleSpaces: SpaceSummary[]

  messages: ListState<MessageSummary>
  reloadMessages: () => Promise<void>
  /** ``opts.starter`` is the key of a clicked suggested question (guests only; counted for popularity). */
  sendUserMessage: (text: string, opts?: { starter?: string }) => Promise<void>
  sending: SendingState
  /**
   * Find similar pictures from a picture and/or a description. The answer is
   * added to the thread like any other. No language model is involved, so the
   * "check before sending" step does not apply. Signed-in users only.
   * Resolves true when the search ran, false when it failed (the error is in ``sending``).
   */
  sendImageSearch: (picture: File | null, text: string) => Promise<boolean>
  /**
   * "Check before sending": hold each prompt for the person to read and edit
   * before the model is called. Per browser; signed-in users only.
   */
  reviewBeforeSend: boolean
  setReviewBeforeSend: (on: boolean) => void
  /**
   * Where questions are looked up: documents, pictures, or both (the default). Per browser,
   * like "check before sending". Pictures sent with a question always search the pictures.
   */
  searchMode: SearchMode
  setSearchMode: (mode: SearchMode) => void
  /** The prompt currently held for review, if any. */
  pendingReview: { conversationId: string; review: ReviewPayload } | null
  /** An error about the held prompt itself (for example an edit was refused). */
  reviewError: string | null
  /** Send the held prompt with the person's edits and stream the reply. */
  approveReview: (edits: ReviewEditsBody) => Promise<void>
  /** Drop the held prompt; the model is never called. */
  cancelReview: () => Promise<void>
  /** Re-scan the held prompt for the person's current edits (nothing is sent). */
  previewReview: (edits: ReviewEditsBody, signal?: AbortSignal) => Promise<ReviewPayload>
  /**
   * Guest demo only: how many questions are left today (null until known,
   * and for signed-in users). Guests ask a stateless assistant — see
   * ``sendGuestMessage``.
   */
  guestQuota: GuestQuota | null

  // Knowledge scope (Slice F). Sources are fetched on demand per
  // conversation; ``scope_mode`` lives on ``currentConversation`` itself.
  scopeSources: ListState<ScopeSource>
  reloadScopeSources: () => Promise<void>
  setScopeMode: (mode: ScopeMode) => Promise<void>
  addScopeSource: (spaceId: string) => Promise<ScopeSource | null>
  removeScopeSource: (spaceId: string) => Promise<void>

  // Per-conversation model override (nulls = clear → org default).
  setConversationModel: (
    modelId: string | null,
    bindingId: string | null,
  ) => Promise<void>
  // Rename a conversation.
  renameConversation: (conversationId: string, title: string) => Promise<void>
  // Bind (or clear, with null) the conversation's agent.
  setConversationAgent: (agentId: string | null) => Promise<void>
}

const ChatContext = createContext<ChatContextValue | null>(null)


const REVIEW_STORAGE_KEY = 'quberi.reviewBeforeSend'
const SEARCH_MODE_STORAGE_KEY = 'quberi.searchMode'

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

export function ChatProvider({ children }: PropsWithChildren) {
  const { credentials } = useAuth()
  const { i18n } = useTranslation()
  const { state: spacesState } = useSpaces()

  // Pictures searched with are held in this tab only; let them go when the
  // person signs out or the chat is closed.
  const signedIn = credentials != null
  useEffect(() => {
    if (!signedIn) forgetQueryPictures()
    return () => forgetQueryPictures()
  }, [signedIn])

  // Spaces reached only through individually shared documents have no conversations
  // of their own and cannot anchor one; search still covers them (auto scope).
  const accessibleSpaces = useMemo(
    () => (spacesState.kind === 'ready' ? spacesState.items.filter(isWholeSpace) : []),
    [spacesState],
  )

  // ``anchorSpaceId`` is the space a NEW conversation gets filed under.
  // Ordinary users never pick this: it defaults to their personal space
  // (``preset === 'personal'``) and falls back to the first accessible
  // space. In ``auto`` scope mode retrieval searches every accessible
  // space regardless, so the anchor only decides where the thread lives.
  const [anchorSpaceId, setAnchorSpaceIdState] = useState<string | null>(null)
  useEffect(() => {
    if (anchorSpaceId && accessibleSpaces.some((s) => s.id === anchorSpaceId)) {
      return
    }
    const personal = accessibleSpaces.find((s) => s.preset === 'personal')
    setAnchorSpaceIdState(personal?.id ?? accessibleSpaces[0]?.id ?? null)
  }, [anchorSpaceId, accessibleSpaces])

  const anchorSpace = useMemo<SpaceSummary | null>(() => {
    if (!anchorSpaceId) return null
    return accessibleSpaces.find((s) => s.id === anchorSpaceId) ?? null
  }, [anchorSpaceId, accessibleSpaces])

  const setAnchorSpaceId = useCallback((id: string) => {
    setAnchorSpaceIdState(id)
  }, [])

  const [conversations, setConversations] =
    useState<ListState<ConversationSummary>>({ kind: 'idle' })
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ListState<MessageSummary>>({ kind: 'idle' })
  const [scopeSources, setScopeSources] = useState<ListState<ScopeSource>>({
    kind: 'idle',
  })
  const [sending, setSending] = useState<SendingState>({ active: false, error: null })
  const [reviewBeforeSend, setReviewBeforeSendState] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(REVIEW_STORAGE_KEY) === '1'
    } catch {
      return false
    }
  })
  const [searchMode, setSearchModeState] = useState<SearchMode>(() => {
    try {
      const saved = window.localStorage.getItem(SEARCH_MODE_STORAGE_KEY)
      return SEARCH_MODES.find((m) => m === saved) ?? 'both'
    } catch {
      return 'both'
    }
  })
  const setSearchMode = useCallback((mode: SearchMode) => {
    setSearchModeState(mode)
    try {
      window.localStorage.setItem(SEARCH_MODE_STORAGE_KEY, mode)
    } catch {
      /* private mode: the choice just lasts for this page */
    }
  }, [])
  const [pendingReview, setPendingReview] = useState<{
    conversationId: string
    review: ReviewPayload
  } | null>(null)
  const [reviewError, setReviewError] = useState<string | null>(null)
  const setReviewBeforeSend = useCallback((on: boolean) => {
    setReviewBeforeSendState(on)
    try {
      window.localStorage.setItem(REVIEW_STORAGE_KEY, on ? '1' : '0')
    } catch {
      /* private mode: the choice just lasts for this page */
    }
  }, [])
  const [guestQuota, setGuestQuota] = useState<GuestQuota | null>(null)

  // Guests: learn today's remaining questions up front, so the composer can
  // show it before the first question is asked.
  const isGuestSession = credentials?.isGuest === true
  useEffect(() => {
    if (!credentials || !isGuestSession) {
      setGuestQuota(null)
      return
    }
    let cancelled = false
    getGuestQuota(credentials)
      .then((q) => {
        if (!cancelled) setGuestQuota(q)
      })
      .catch(() => {
        /* non-critical: the hint just stays hidden */
      })
    return () => {
      cancelled = true
    }
  }, [credentials, isGuestSession])

  // Track which fetch generation is "live", to guard against a slow
  // response overwriting a newer selection. The generation is bumped on
  // every ``reloadConversations`` call and on space-set changes.
  const conversationsGenRef = useRef(0)
  const loadedConversationRef = useRef<string | null>(null)
  const loadedScopeConversationRef = useRef<string | null>(null)

  // Comma-joined slug list, used as a stable dependency for the fan-out
  // fetch below — a plain array reference would churn on every render.
  const spaceIdsKey = accessibleSpaces.map((s) => s.id).sort().join(',')

  const reloadConversations = useCallback(async () => {
    if (!credentials || accessibleSpaces.length === 0) {
      setConversations({ kind: 'idle' })
      return
    }
    const gen = ++conversationsGenRef.current
    setConversations({ kind: 'loading' })
    try {
      // Fan out one request per accessible space; the caller sees the
      // union sorted by most-recent activity. If any request fails we
      // still surface whatever landed successfully, because a single
      // 403 on one space shouldn't blank the whole sidebar.
      const results = await Promise.allSettled(
        accessibleSpaces.map((s) => apiListConversations(credentials, s.id)),
      )
      if (conversationsGenRef.current !== gen) return // superseded
      const items: ConversationSummary[] = []
      for (const r of results) {
        if (r.status === 'fulfilled') items.push(...r.value.items)
      }
      // Newest activity first; ``updated_at`` is the fallback so freshly
      // created but empty conversations still float to the top.
      items.sort((a, b) => {
        const at = a.last_message_at ?? a.updated_at
        const bt = b.last_message_at ?? b.updated_at
        return bt.localeCompare(at)
      })
      setConversations({ kind: 'ready', items })
      // Reconcile current selection: keep if it still exists, else pick
      // the first row (newest), else clear.
      setCurrentConversationId((existing) => {
        if (existing && items.some((c) => c.id === existing)) return existing
        return items[0]?.id ?? null
      })
    } catch (error) {
      if (conversationsGenRef.current !== gen) return
      setConversations({ kind: 'error', message: toMessage(error) })
    }
    // ``spaceIdsKey`` is the stable identity for the space-id set; we
    // depend on it (not the ``accessibleSpaces`` reference) so re-renders
    // don't spuriously refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credentials, spaceIdsKey])

  const reloadMessages = useCallback(async () => {
    if (!credentials || !currentConversationId) {
      setMessages({ kind: 'idle' })
      return
    }
    const convId = currentConversationId
    loadedConversationRef.current = convId
    setMessages({ kind: 'loading' })
    try {
      const { items, conversation } = await apiListMessages(credentials, convId)
      if (loadedConversationRef.current !== convId) return
      setMessages({ kind: 'ready', items })
      // Keep the conversation summary in the list in sync with the fresh
      // one (title / message_count may have drifted).
      setConversations((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.map((c) => (c.id === conversation.id ? conversation : c)),
            }
          : prev,
      )
    } catch (error) {
      if (loadedConversationRef.current !== convId) return
      setMessages({ kind: 'error', message: toMessage(error) })
    }
  }, [credentials, currentConversationId])

  const reloadScopeSources = useCallback(async () => {
    if (!credentials || !currentConversationId) {
      setScopeSources({ kind: 'idle' })
      return
    }
    const convId = currentConversationId
    loadedScopeConversationRef.current = convId
    setScopeSources({ kind: 'loading' })
    try {
      const { items } = await apiListScopeSources(credentials, convId)
      if (loadedScopeConversationRef.current !== convId) return
      setScopeSources({ kind: 'ready', items })
    } catch (error) {
      if (loadedScopeConversationRef.current !== convId) return
      setScopeSources({ kind: 'error', message: toMessage(error) })
    }
  }, [credentials, currentConversationId])

  // Re-fetch conversations whenever the space changes.
  useEffect(() => {
    void reloadConversations()
  }, [reloadConversations])

  // Re-fetch messages + scope sources whenever the selected conversation
  // changes.
  useEffect(() => {
    void reloadMessages()
    void reloadScopeSources()
  }, [reloadMessages, reloadScopeSources])

  const selectConversation = useCallback((id: string | null) => {
    setCurrentConversationId(id)
    setSending({ active: false, error: null })
  }, [])

  const createNewConversation = useCallback(
    async (spaceId?: string): Promise<ConversationSummary> => {
      if (!credentials) throw new Error('not signed in')
      const target = spaceId ?? anchorSpace?.id
      if (!target) throw new Error('no space selected')
      const created = await apiCreate(credentials, target)
      setConversations((prev) =>
        prev.kind === 'ready'
          ? { kind: 'ready', items: [created, ...prev.items] }
          : { kind: 'ready', items: [created] },
      )
      setCurrentConversationId(created.id)
      return created
    },
    [credentials, anchorSpace],
  )

  /**
   * Guest turn. The server keeps nothing, so the conversation lives only in
   * this page's state (a refresh clears it): we append local rows, send the
   * last few local turns as context, and stream the answer into a
   * provisional row exactly like the signed-in path does.
   */
  const sendGuestMessage = useCallback(
    async (text: string, starter?: string) => {
      if (!credentials) return
      const convId = currentConversationId ?? 'guest'
      setSending({ active: true, error: null })

      const now = () => new Date().toISOString()
      const userRow: MessageSummary = {
        id: `local-user-${crypto.randomUUID()}`,
        conversation_id: convId,
        role: 'user',
        content: { type: 'text', text, guestLocal: true },
        author_user_id: null,
        status: 'completed',
        correlation_id: null,
        created_at: now(),
      }
      const provisionalId = `local-pending-${crypto.randomUUID()}`
      const assistantRow = (
        answer: string,
        citations: MessageCitation[],
        status: 'pending' | 'completed',
      ): MessageSummary => ({
        id: provisionalId,
        conversation_id: convId,
        role: 'assistant',
        content: {
          type: 'text',
          text: answer,
          guestLocal: true,
          ...(citations.length ? { citations } : {}),
          ...(status === 'pending' ? { placeholder: true } : {}),
        },
        author_user_id: null,
        status,
        correlation_id: null,
        created_at: now(),
      })

      // Context for the model: only this page's own completed turns.
      const history: GuestTurn[] =
        messages.kind === 'ready'
          ? messages.items
              .filter(
                (m) =>
                  (m.content as { guestLocal?: unknown }).guestLocal === true &&
                  m.status === 'completed' &&
                  (m.role === 'user' || m.role === 'assistant'),
              )
              .map((m) => ({
                role: m.role as 'user' | 'assistant',
                text: String((m.content as { text?: unknown }).text ?? ''),
              }))
          : []

      const put = (row: MessageSummary) =>
        setMessages((prev) => {
          const items = prev.kind === 'ready' ? prev.items : []
          return {
            kind: 'ready',
            items: [...items.filter((m) => m.id !== row.id), row],
          }
        })
      setMessages((prev) => ({
        kind: 'ready',
        items: [...(prev.kind === 'ready' ? prev.items : []), userRow, assistantRow('', [], 'pending')],
      }))

      let citations: MessageCitation[] = []
      let answer = ''
      try {
        for await (const event of streamGuestAsk(
          credentials,
          text,
          history,
          i18n.resolvedLanguage ?? 'en-US',
          starter,
        )) {
          switch (event.kind) {
            case 'quota':
              setGuestQuota(event.quota)
              break
            case 'retrieved':
              citations = event.citations
              put(assistantRow(answer, citations, 'pending'))
              break
            case 'delta':
              answer += event.text
              put(assistantRow(answer, citations, 'pending'))
              break
            case 'error':
              setSending({ active: false, error: event.message })
              break
            case 'done': {
              // ``done`` carries the final text and which citations were
              // actually used ([N] markers), same as the signed-in path.
              const finalText = event.answer ?? answer
              if (!finalText) {
                // Nothing came back (the server reported an error first):
                // drop this turn instead of leaving an empty bubble.
                setMessages((prev) =>
                  prev.kind === 'ready'
                    ? {
                        kind: 'ready',
                        items: prev.items.filter(
                          (m) => m.id !== userRow.id && m.id !== provisionalId,
                        ),
                      }
                    : prev,
                )
              } else {
                put(assistantRow(finalText, event.citations ?? citations, 'completed'))
              }
              break
            }
          }
        }
        setSending((prev) => ({ active: false, error: prev.error }))
      } catch (error) {
        // Roll the whole turn back so a rejected question (daily limit,
        // too long, network) doesn't leave an unanswered bubble that would
        // also be sent as context next time.
        setMessages((prev) =>
          prev.kind === 'ready'
            ? {
                kind: 'ready',
                items: prev.items.filter(
                  (m) => m.id !== userRow.id && m.id !== provisionalId,
                ),
              }
            : prev,
        )
        // A 429 means today's allowance is gone; reflect that in the hint.
        if (error instanceof ApiError && error.status === 429) {
          setGuestQuota((q) => (q ? { ...q, remaining: 0 } : q))
        }
        setSending({ active: false, error: toMessage(error) })
      }
    },
    [credentials, currentConversationId, messages, i18n.resolvedLanguage],
  )

  /**
   * Run one assistant turn's event stream into the thread: add the user's
   * row, show the answer as it arrives, and swap in the saved messages.
   * Shared by a normal send and by approving a held prompt.
   *
   * When the server holds the prompt for review instead of answering, the
   * placeholder is removed and the review card takes its place.
   */
  const runStream = useCallback(
    async (
      convId: string,
      events: () => AsyncIterable<ChatStreamEvent>,
      opts: { approving?: boolean } = {},
    ): Promise<{ ok: true } | { ok: false; error: unknown }> => {
      setSending({ active: true, error: null })
      // Provisional assistant message (id starts as null; we swap the
      // real row in when the ``assistant_message`` event arrives). While
      // the delta stream is running the UI already renders the answer as
      // it arrives, chunk by chunk.
      const provisionalId = `pending-${crypto.randomUUID()}`
      let pendingCitations: MessageCitation[] = []
      let pendingText = ''
      let pendingTrace: AgentTraceStep[] = []
      let heldForReview = false
      const upsertProvisional = (
        nextText: string,
        citations: MessageCitation[],
        trace: AgentTraceStep[],
      ) => {
        setMessages((prev) => {
          if (prev.kind !== 'ready') return prev
          const filtered = prev.items.filter((m) => m.id !== provisionalId)
          const streamRow: MessageSummary = {
            id: provisionalId,
            conversation_id: convId,
            role: 'assistant',
            content: {
              type: 'text',
              text: nextText,
              ...(citations.length ? { citations } : {}),
              ...(trace.length ? { meta: { trace } } : {}),
              placeholder: true,
            },
            author_user_id: null,
            status: 'pending',
            correlation_id: null,
            created_at: new Date().toISOString(),
          }
          return { kind: 'ready', items: [...filtered, streamRow] }
        })
      }
      // Approving a held prompt has no ``user_message`` event (it was shown
      // when the prompt was held), so open the answer row straight away.
      if (opts.approving) upsertProvisional('', [], pendingTrace)
      try {
        for await (const event of events()) {
          switch (event.kind) {
            case 'user_message':
              setMessages((prev) =>
                prev.kind === 'ready'
                  ? { kind: 'ready', items: [...prev.items, event.message] }
                  : { kind: 'ready', items: [event.message] },
              )
              upsertProvisional('', [], pendingTrace)
              break
            case 'review_required':
              heldForReview = true
              setMessages((prev) =>
                prev.kind === 'ready'
                  ? {
                      kind: 'ready',
                      items: prev.items.filter((m) => m.id !== provisionalId),
                    }
                  : prev,
              )
              setPendingReview({ conversationId: convId, review: event.review })
              break
            case 'retrieved':
              pendingCitations = event.citations
              upsertProvisional(pendingText, pendingCitations, pendingTrace)
              break
            case 'delta':
              pendingText += event.text
              upsertProvisional(pendingText, pendingCitations, pendingTrace)
              break
            case 'agent_step':
              pendingTrace = [...pendingTrace, event.step as AgentTraceStep]
              upsertProvisional(pendingText, pendingCitations, pendingTrace)
              break
            case 'error':
              setSending({ active: false, error: event.message })
              break
            case 'assistant_message':
              setMessages((prev) => {
                if (prev.kind !== 'ready') return prev
                return {
                  kind: 'ready',
                  items: prev.items.map((m) =>
                    m.id === provisionalId ? event.message : m,
                  ),
                }
              })
              break
            case 'done':
              break
          }
        }
        // The server counts a turn (+2) when the answer is saved; a held
        // prompt has no answer yet, so nothing changes until then.
        if (!heldForReview) {
          setConversations((prev) => {
            if (prev.kind !== 'ready') return prev
            return {
              kind: 'ready',
              items: prev.items.map((c) => {
                if (c.id !== convId) return c
                return {
                  ...c,
                  message_count: c.message_count + 2,
                  last_message_at: new Date().toISOString(),
                }
              }),
            }
          })
        }
        setSending((prev) => ({ active: false, error: prev.error }))
        return { ok: true }
      } catch (error) {
        // Roll back the provisional row so the user isn't stuck with a
        // half-typed answer if the stream itself blew up.
        setMessages((prev) =>
          prev.kind === 'ready'
            ? {
                kind: 'ready',
                items: prev.items.filter((m) => m.id !== provisionalId),
              }
            : prev,
        )
        setSending({ active: false, error: toMessage(error) })
        return { ok: false, error }
      }
    },
    [],
  )
  const sendUserMessage = useCallback(
    async (text: string, opts?: { starter?: string }) => {
      if (credentials?.isGuest) {
        await sendGuestMessage(text, opts?.starter)
        return
      }
      if (!credentials || !currentConversationId) return
      // One held prompt at a time: decide it before asking the next question.
      if (pendingReview) return
      setReviewError(null)
      await runStream(currentConversationId, () =>
        apiStreamMessage(
          credentials,
          currentConversationId,
          text,
          undefined,
          reviewBeforeSend,
          searchMode !== 'text',
        ),
      )
    },
    [
      credentials,
      currentConversationId,
      sendGuestMessage,
      runStream,
      reviewBeforeSend,
      searchMode,
      pendingReview,
    ],
  )
  const sendImageSearch = useCallback(
    async (picture: File | null, text: string): Promise<boolean> => {
      if (!credentials || credentials.isGuest || !currentConversationId) return false
      if (!picture && !text.trim()) return false
      const convId = currentConversationId
      setSending({ active: true, error: null })
      try {
        const res = await apiSendImageSearch(credentials, convId, picture, text)
        // The server does not keep the picture; hold it here so the thread can show it.
        if (picture) rememberQueryPicture(res.user_message.id, picture)
        // Only show it if the person is still looking at this conversation;
        // otherwise it is saved and appears when they come back.
        if (loadedConversationRef.current === convId) {
          setMessages((prev) => ({
            kind: 'ready',
            items: [
              ...(prev.kind === 'ready' ? prev.items : []),
              res.user_message,
              res.assistant_message,
            ],
          }))
        }
        setConversations((prev) =>
          prev.kind === 'ready'
            ? {
                kind: 'ready',
                items: prev.items.map((c) => (c.id === res.conversation.id ? res.conversation : c)),
              }
            : prev,
        )
        setSending({ active: false, error: null })
        return true
      } catch (error) {
        setSending({ active: false, error: toMessage(error) })
        return false
      }
    },
    [credentials, currentConversationId],
  )
  const approveReview = useCallback(
    async (edits: ReviewEditsBody) => {
      const held = pendingReview
      if (!credentials || !held?.review.pending_id) return
      setReviewError(null)
      const { conversationId } = held
      const pendingId = held.review.pending_id
      // The card gives way to the answer; it comes back if the server refuses.
      setPendingReview(null)
      const result = await runStream(
        conversationId,
        () => streamApprovedReview(credentials, conversationId, pendingId, edits),
        { approving: true },
      )
      if (!result.ok && result.error instanceof ApiError) {
        if (result.error.status === 422) {
          // A refused edit: keep the card so it can be fixed.
          setPendingReview(held)
          setSending({ active: false, error: null })
          setReviewError(toMessage(result.error))
        }
        // 404: it expired or was already handled; the error is already shown.
      }
    },
    [credentials, pendingReview, runStream],
  )
  const cancelReview = useCallback(async () => {
    const held = pendingReview
    if (!credentials || !held?.review.pending_id) return
    setReviewError(null)
    try {
      const note = await apiCancelReview(
        credentials,
        held.conversationId,
        held.review.pending_id,
      )
      setPendingReview(null)
      setMessages((prev) =>
        prev.kind === 'ready'
          ? { kind: 'ready', items: [...prev.items, note] }
          : { kind: 'ready', items: [note] },
      )
      setConversations((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.map((c) =>
                c.id === held.conversationId
                  ? { ...c, message_count: c.message_count + 2 }
                  : c,
              ),
            }
          : prev,
      )
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) setPendingReview(null)
      setReviewError(toMessage(error))
    }
  }, [credentials, pendingReview])
  const previewReview = useCallback(
    (edits: ReviewEditsBody, signal?: AbortSignal) => {
      if (!credentials || !pendingReview?.review.pending_id) {
        return Promise.reject(new Error('no held prompt'))
      }
      return apiPreviewReview(
        credentials,
        pendingReview.conversationId,
        pendingReview.review.pending_id,
        edits,
        signal,
      )
    },
    [credentials, pendingReview],
  )

  const setScopeMode = useCallback(
    async (mode: ScopeMode) => {
      if (!credentials || !currentConversationId) return
      const updated = await apiUpdateScopeMode(
        credentials,
        currentConversationId,
        mode,
      )
      // Reflect the new scope_mode on the conversation list so the chip
      // strip re-renders immediately.
      setConversations((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.map((c) => (c.id === updated.id ? updated : c)),
            }
          : prev,
      )
    },
    [credentials, currentConversationId],
  )

  const addScopeSource = useCallback(
    async (spaceId: string): Promise<ScopeSource | null> => {
      if (!credentials || !currentConversationId) return null
      const created = await apiAddScopeSource(
        credentials,
        currentConversationId,
        spaceId,
      )
      setScopeSources((prev) =>
        prev.kind === 'ready'
          ? { kind: 'ready', items: [...prev.items, created] }
          : { kind: 'ready', items: [created] },
      )
      return created
    },
    [credentials, currentConversationId],
  )

  const removeScopeSource = useCallback(
    async (spaceId: string) => {
      if (!credentials || !currentConversationId) return
      await apiRemoveScopeSource(credentials, currentConversationId, spaceId)
      setScopeSources((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.filter((s) => s.space_id !== spaceId),
            }
          : prev,
      )
    },
    [credentials, currentConversationId],
  )

  const setConversationModel = useCallback(
    async (modelId: string | null, bindingId: string | null) => {
      if (!credentials || !currentConversationId) return
      const updated = await apiSetConversationModel(
        credentials,
        currentConversationId,
        modelId,
        bindingId,
      )
      setConversations((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.map((c) => (c.id === updated.id ? updated : c)),
            }
          : prev,
      )
    },
    [credentials, currentConversationId],
  )

  const renameConversation = useCallback(
    async (conversationId: string, title: string) => {
      if (!credentials) return
      const updated = await apiRenameConversation(credentials, conversationId, title)
      setConversations((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.map((c) => (c.id === updated.id ? updated : c)),
            }
          : prev,
      )
    },
    [credentials],
  )

  const setConversationAgent = useCallback(
    async (agentId: string | null) => {
      if (!credentials || !currentConversationId) return
      const updated = await apiSetConversationAgent(
        credentials,
        currentConversationId,
        agentId,
      )
      setConversations((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.map((c) => (c.id === updated.id ? updated : c)),
            }
          : prev,
      )
    },
    [credentials, currentConversationId],
  )

  const currentConversation = useMemo<ConversationSummary | null>(() => {
    if (conversations.kind !== 'ready' || !currentConversationId) return null
    return conversations.items.find((c) => c.id === currentConversationId) ?? null
  }, [conversations, currentConversationId])

  const value = useMemo<ChatContextValue>(
    () => ({
      anchorSpace,
      setAnchorSpaceId,
      accessibleSpaces,
      conversations,
      currentConversationId,
      currentConversation,
      selectConversation,
      createNewConversation,
      reloadConversations,
      messages,
      reloadMessages,
      sendUserMessage,
      sendImageSearch,
      sending,
      reviewBeforeSend,
      setReviewBeforeSend,
      searchMode,
      setSearchMode,
      pendingReview,
      reviewError,
      approveReview,
      cancelReview,
      previewReview,
      guestQuota,
      scopeSources,
      reloadScopeSources,
      setScopeMode,
      addScopeSource,
      removeScopeSource,
      setConversationModel,
      renameConversation,
      setConversationAgent,
    }),
    [
      anchorSpace,
      setAnchorSpaceId,
      accessibleSpaces,
      conversations,
      currentConversationId,
      currentConversation,
      selectConversation,
      createNewConversation,
      reloadConversations,
      messages,
      reloadMessages,
      sendUserMessage,
      sendImageSearch,
      sending,
      reviewBeforeSend,
      setReviewBeforeSend,
      searchMode,
      setSearchMode,
      pendingReview,
      reviewError,
      approveReview,
      cancelReview,
      previewReview,
      guestQuota,
      scopeSources,
      reloadScopeSources,
      setScopeMode,
      addScopeSource,
      removeScopeSource,
      setConversationModel,
      renameConversation,
      setConversationAgent,
    ],
  )

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>
}

export function useChat(): ChatContextValue {
  const value = useContext(ChatContext)
  if (value === null) {
    throw new Error('useChat must be used within a ChatProvider')
  }
  return value
}
