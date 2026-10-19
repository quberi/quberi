import { API_BASE, apiErrorFrom, apiFetch, buildAuthHeaders } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  ConversationList,
  ConversationSummary,
  MessageCitation,
  MessageList,
  MessageSummary,
  ScopeMode,
  ScopeSource,
  ScopeSourceList,
  SendMessageResponse,
} from './types'

export function listConversations(
  credentials: DevCredentials,
  spaceId: string,
): Promise<ConversationList> {
  return apiFetch<ConversationList>(`/api/v1/spaces/${spaceId}/conversations`, {
    credentials,
  })
}

export function createConversation(
  credentials: DevCredentials,
  spaceId: string,
  title?: string | null,
): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>(`/api/v1/spaces/${spaceId}/conversations`, {
    method: 'POST',
    body: title ? { title } : {},
    credentials,
  })
}

export function listMessages(
  credentials: DevCredentials,
  conversationId: string,
): Promise<MessageList> {
  return apiFetch<MessageList>(`/api/v1/conversations/${conversationId}/messages`, {
    credentials,
  })
}

export function sendMessage(
  credentials: DevCredentials,
  conversationId: string,
  text: string,
): Promise<SendMessageResponse> {
  return apiFetch<SendMessageResponse>(
    `/api/v1/conversations/${conversationId}/messages`,
    {
      method: 'POST',
      body: { text },
      credentials,
    },
  )
}

/** Largest picture the server accepts as a query (bytes). Checked here too, to fail before uploading. */
export const MAX_QUERY_PICTURE_BYTES = 10 * 1024 * 1024
/** Longest description that can go with (or replace) a query picture. */
export const MAX_IMAGE_QUERY_TEXT = 500

async function failureOf(response: Response) {
  return apiErrorFrom(response)
}

/**
 * Find similar pictures from a picture, a description, or both. The reply is
 * an ordinary message pair; the answer carries ``image_matches``. The query
 * picture is kept by the server for the person who sent it (see ``fetchQueryPicture``).
 *
 * The first search after a server start loads the picture model, which can
 * take a while, so this waits longer than ``apiFetch`` does.
 */
export async function sendImageSearch(
  credentials: DevCredentials,
  conversationId: string,
  picture: File | null,
  text: string,
  signal?: AbortSignal,
): Promise<SendMessageResponse> {
  const form = new FormData()
  if (picture) form.append('file', picture, picture.name)
  if (text.trim()) form.append('text', text.trim())
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 180_000)
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', () => controller.abort(), { once: true })
  }
  try {
    // No Content-Type: the browser adds the multipart boundary itself.
    const response = await fetch(`${API_BASE}/api/v1/conversations/${conversationId}/image-search`, {
      method: 'POST',
      headers: { Accept: 'application/json', ...buildAuthHeaders(credentials) },
      body: form,
      signal: controller.signal,
    })
    if (!response.ok) throw await failureOf(response)
    return (await response.json()) as SendMessageResponse
  } finally {
    window.clearTimeout(timer)
  }
}

/** A small preview (JPEG, at most 320 px) of a stored picture, fetched with the person's credentials. */
export async function fetchThumbnail(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await fetch(
    `${API_BASE}/api/v1/spaces/${spaceId}/documents/${documentId}/thumbnail`,
    { headers: buildAuthHeaders(credentials), signal },
  )
  if (!response.ok) throw await failureOf(response)
  return response.blob()
}

/** The picture a question was searched with. Only its sender can read it back. */
export async function fetchQueryPicture(
  credentials: DevCredentials,
  conversationId: string,
  messageId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await fetch(
    `${API_BASE}/api/v1/conversations/${conversationId}/messages/${messageId}/picture`,
    { headers: buildAuthHeaders(credentials), signal },
  )
  if (!response.ok) throw await failureOf(response)
  return response.blob()
}

// ---------------------------------------------------------------------------
// Streaming chat (SSE over POST)
// ---------------------------------------------------------------------------

/**
 * Event shapes emitted by the backend SSE endpoint.
 * The union kind is discriminated by ``kind`` so callers can switch
 * exhaustively.
 */
/** A span of text the server thinks may be sensitive (offsets into the text it came with). */
export interface ReviewSpan {
  start: number
  end: number
  kind: 'phone' | 'id_card' | 'bank_card' | 'email' | 'secret' | 'password' | 'internal_ip'
}

export interface ReviewPart {
  text: string
  spans: ReviewSpan[]
}

export interface ReviewHistoryTurn extends ReviewPart {
  role: string
}

export interface ReviewReference extends ReviewPart {
  index: number
  document_name: string
  chunk_index: number
  included: boolean
}

/** The prompt held for the person to read and edit before the model is called. */
export interface ReviewPayload {
  pending_id?: string
  expires_in?: number
  /** ``vendor`` is null for a model run locally. */
  model?: {
    model_id: string
    display_name: string
    vendor: string | null
    /** The model the conversation asked for is gone; the default one is used instead. */
    model_fallback?: boolean
  }
  question: ReviewPart
  history: ReviewHistoryTurn[]
  references: ReviewReference[]
  /** The final messages as the model would receive them (null: no model call). */
  messages: { role: string; content: string }[] | null
  edited_parts: number
  excluded_references: number
  sensitive_count: number
}

/** The person's version: the exact text of every piece. */
export interface ReviewEditsBody {
  question: string
  history: string[]
  references: { index: number; include: boolean; text: string }[]
}

export type ChatStreamEvent =
  | { kind: 'user_message'; message: MessageSummary }
  | { kind: 'review_required'; review: ReviewPayload }
  | { kind: 'retrieved'; model_id: string; citations: MessageCitation[] }
  | { kind: 'delta'; text: string }
  | {
      kind: 'agent_step'
      step: { kind: string; tool?: string; text?: string; [k: string]: unknown }
    }
  | { kind: 'error'; message: string }
  | { kind: 'assistant_message'; message: MessageSummary }
  | { kind: 'done' }

/**
 * POST a user turn to the streaming endpoint and yield ``ChatStreamEvent``s.
 *
 * ``EventSource`` in the browser only speaks GET, so we roll our own
 * SSE frame parser over ``fetch``'s ``ReadableStream``. Frame form is
 * ``event: <kind>\\ndata: <json>\\n\\n`` — the parser accumulates by
 * blank-line delimiter and dispatches whichever ``event:`` was seen.
 */
export async function* streamMessage(
  credentials: DevCredentials,
  conversationId: string,
  text: string,
  signal?: AbortSignal,
  review = false,
  /** False: text only (no picture lookup, no picture shortcut). */
  pictures = true,
): AsyncGenerator<ChatStreamEvent> {
  yield* postSse(
    credentials,
    `/api/v1/conversations/${conversationId}/messages/stream`,
    pictures ? { text, review } : { text, review, pictures: false },
    signal,
  )
}

/** Send the reviewed prompt to the model and stream the reply. */
export async function* streamApprovedReview(
  credentials: DevCredentials,
  conversationId: string,
  pendingId: string,
  edits: ReviewEditsBody,
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  yield* postSse(
    credentials,
    `/api/v1/conversations/${conversationId}/messages/review/${pendingId}/approve`,
    edits,
    signal,
  )
}

/** The held prompt for the given edits (nothing is sent or used up). */
export function previewReview(
  credentials: DevCredentials,
  conversationId: string,
  pendingId: string,
  edits: ReviewEditsBody,
  signal?: AbortSignal,
): Promise<ReviewPayload> {
  return apiFetch<ReviewPayload>(
    `/api/v1/conversations/${conversationId}/messages/review/${pendingId}/preview`,
    { method: 'POST', body: edits, credentials, signal },
  )
}

/** Drop the held prompt without calling the model; resolves with the note saved in the chat. */
export function cancelReview(
  credentials: DevCredentials,
  conversationId: string,
  pendingId: string,
): Promise<MessageSummary> {
  return apiFetch<MessageSummary>(
    `/api/v1/conversations/${conversationId}/messages/review/${pendingId}/cancel`,
    { method: 'POST', body: {}, credentials },
  )
}

async function* postSse(
  credentials: DevCredentials,
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...buildAuthHeaders(credentials),
    },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok || !response.body) {
    throw await apiErrorFrom(response, 'stream open failed')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // Events are delimited by blank lines. Consume all complete
      // frames; keep the trailing partial in the buffer.
      let sepIdx = buffer.indexOf('\n\n')
      while (sepIdx !== -1) {
        const frame = buffer.slice(0, sepIdx)
        buffer = buffer.slice(sepIdx + 2)
        const parsed = parseSseFrame(frame)
        if (parsed) yield parsed
        sepIdx = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}
function parseSseFrame(frame: string): ChatStreamEvent | null {
  let eventKind = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      eventKind = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trim())
    }
    // ignore ``:`` comments and unknown fields
  }
  if (dataLines.length === 0) return null
  let data: Record<string, unknown> = {}
  try {
    data = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
  } catch {
    return null
  }
  switch (eventKind) {
    case 'user_message':
      return { kind: 'user_message', message: data as unknown as MessageSummary }
    case 'review_required':
      return { kind: 'review_required', review: data as unknown as ReviewPayload }
    case 'assistant_message':
      return {
        kind: 'assistant_message',
        message: data as unknown as MessageSummary,
      }
    case 'retrieved':
      return {
        kind: 'retrieved',
        model_id: String(data.model_id ?? ''),
        citations: (data.citations as MessageCitation[] | undefined) ?? [],
      }
    case 'delta':
      return { kind: 'delta', text: String(data.text ?? '') }
    case 'agent_step':
      return {
        kind: 'agent_step',
        step: data as { kind: string; tool?: string; text?: string },
      }
    case 'error':
      return { kind: 'error', message: String(data.message ?? 'unknown') }
    case 'done':
      return { kind: 'done' }
    default:
      return null
  }
}

/** Set (or clear, with nulls) the conversation's model override. */
export function setConversationModel(
  credentials: DevCredentials,
  conversationId: string,
  modelId: string | null,
  bindingId: string | null,
): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>(
    `/api/v1/conversations/${conversationId}/model`,
    {
      method: 'PATCH',
      body: { model_id: modelId, binding_id: bindingId },
      credentials,
    },
  )
}

/** Rename a conversation. */
export function renameConversation(
  credentials: DevCredentials,
  conversationId: string,
  title: string,
): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>(
    `/api/v1/conversations/${conversationId}/title`,
    {
      method: 'PATCH',
      body: { title },
      credentials,
    },
  )
}

/** Bind (or clear, with null) the conversation's agent. */
export function setConversationAgent(
  credentials: DevCredentials,
  conversationId: string,
  agentId: string | null,
): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>(
    `/api/v1/conversations/${conversationId}/agent`,
    {
      method: 'PATCH',
      body: { agent_id: agentId },
      credentials,
    },
  )
}

// ---------------------------------------------------------------------------
// Knowledge scope (Slice F)
// ---------------------------------------------------------------------------

export function updateScopeMode(
  credentials: DevCredentials,
  conversationId: string,
  mode: ScopeMode,
): Promise<ConversationSummary> {
  return apiFetch<ConversationSummary>(
    `/api/v1/conversations/${conversationId}/scope-mode`,
    {
      method: 'PATCH',
      body: { mode },
      credentials,
    },
  )
}

export function listScopeSources(
  credentials: DevCredentials,
  conversationId: string,
): Promise<ScopeSourceList> {
  return apiFetch<ScopeSourceList>(
    `/api/v1/conversations/${conversationId}/scope-sources`,
    { credentials },
  )
}

export function addScopeSource(
  credentials: DevCredentials,
  conversationId: string,
  spaceId: string,
): Promise<ScopeSource> {
  return apiFetch<ScopeSource>(
    `/api/v1/conversations/${conversationId}/scope-sources`,
    {
      method: 'POST',
      body: { space_id: spaceId },
      credentials,
    },
  )
}

export function removeScopeSource(
  credentials: DevCredentials,
  conversationId: string,
  spaceId: string,
): Promise<void> {
  return apiFetch<void>(
    `/api/v1/conversations/${conversationId}/scope-sources/${spaceId}`,
    { method: 'DELETE', credentials },
  )
}
