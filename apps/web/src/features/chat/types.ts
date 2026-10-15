/** Roles the backend ships in ``messages.role``. */
export type MessageRole = 'user' | 'assistant' | 'system' | 'tool'
export type MessageStatus = 'pending' | 'completed' | 'failed'
export type ConversationStatus = 'active' | 'archived'

/**
 * One numbered reference under an assistant message. When ``used=true``
 * the assistant explicitly cited it via ``[N]`` in the answer body; the
 * UI can visually deprioritise unused entries.
 *
 * ``char_start``/``char_end`` are offsets into the source document's
 * re-extracted plain text (see ``documents.getDocumentText``) — they
 * let the UI jump to and highlight the exact span a citation came from.
 */
export interface MessageCitation {
  index: number
  document_id: string
  space_id: string
  document_name: string
  chunk_index: number
  text: string
  score: number
  used: boolean
  char_start: number
  char_end: number
}

/** One step in an agent's reasoning trace (tool call / result / note). */
export interface AgentTraceStep {
  kind?: string // 'tool_call' | 'tool_result' | 'note' | 'answer'
  tool?: string
  arguments?: unknown
  result?: unknown
  text?: string
  [key: string]: unknown
}

export interface AssistantMeta {
  model_id?: string
  prompt_tokens?: number
  completion_tokens?: number
  scope_mode?: 'auto' | 'custom'
  scope_space_ids?: string[]
  retrieved_count?: number
  system_note?: string
  error?: string
  // Agent runs only:
  agent_id?: string
  iterations?: number
  tool_call_count?: number
  trace?: AgentTraceStep[]
}

/**
 * Content payload. ``text`` is always present; ``citations`` and
 * ``meta`` land only on assistant messages produced by the RAG slice.
 */
export interface TextContent {
  type: 'text'
  text: string
  placeholder?: boolean
  citations?: MessageCitation[]
  meta?: AssistantMeta
  [key: string]: unknown
}

export type MessageContent = TextContent | Record<string, unknown>

export interface MessageSummary {
  id: string
  conversation_id: string
  role: MessageRole
  content: MessageContent
  author_user_id: string | null
  status: MessageStatus
  correlation_id: string | null
  created_at: string
}

export type ScopeMode = 'auto' | 'custom'

export interface ConversationSummary {
  id: string
  space_id: string
  title: string | null
  status: ConversationStatus
  scope_mode: ScopeMode
  message_count: number
  last_message_at: string | null
  created_at: string
  updated_at: string
  is_owner: boolean
  /** Per-conversation model override, or null for the org default. */
  model_choice?: { model_id: string; binding_id: string } | null
  /** Per-conversation agent binding, or null for plain RAG. */
  agent_choice?: { agent_id: string } | null
}

export interface ScopeSource {
  id: string
  conversation_id: string
  space_id: string
  space_name: string
  space_slug: string
  added_by_user_id: string | null
  created_at: string
}

export interface ScopeSourceList {
  conversation_id: string
  scope_mode: ScopeMode
  items: ScopeSource[]
  total: number
}

export interface ConversationList {
  items: ConversationSummary[]
  total: number
}

export interface MessageList {
  conversation: ConversationSummary
  items: MessageSummary[]
  total: number
}

export interface SendMessageResponse {
  conversation: ConversationSummary
  user_message: MessageSummary
  assistant_message: MessageSummary
}
