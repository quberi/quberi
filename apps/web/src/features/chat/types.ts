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
  /** The passage is empty because its space is closed to this reader. */
  text_hidden?: boolean
  /**
   * Where the passage sits in the file, when it is known: first and last page, so a reader can
   * open the original and check. Absent for formats without pages and for answers saved before
   * pages were recorded.
   */
  page_start?: number | null
  page_end?: number | null
  unit_start?: number | null
  unit_end?: number | null
  /** 'ocr_block' when any of it was read from a picture of the page (it may contain errors). */
  unit_label?: string
  /** 'image_ocr' for text read out of a picture, 'image_caption' for a model's description of one; 'text' otherwise. */
  source_kind?: string
  /**
   * Which version of the file the passage was read from, and the file's newest version
   * (null when it has no version record). A lower number than the newest means the passage
   * comes from a version that has since been replaced.
   */
  version_number?: number
  latest_version?: number | null
}

/** One picture found by a picture search (see ``POST /conversations/{id}/image-search``). */
export interface ImageMatch {
  document_id: string
  document_name: string
  space_id: string
  space_name: string
  mime_type: string
  size_bytes: number
  score: number
  /** Names are emptied because the space is closed to this reader. */
  hidden?: boolean
  /**
   * When the text read out of this picture is also one of the answer's sources, that source's
   * number: the same file, found by what it says as well as by what it shows.
   */
  citation_index?: number
}

/**
 * What a question is looked up in. ``both`` (the default) searches the documents and the
 * pictures; ``text`` only the documents; ``image`` only the pictures (no model is called).
 */
export type SearchMode = 'text' | 'image' | 'both'
export const SEARCH_MODES: readonly SearchMode[] = ['text', 'image', 'both']

/**
 * What a picture-search question was made of. ``stored`` says the server kept the
 * picture for its sender (questions asked before pictures were kept, or when keeping
 * it failed, have none).
 */
export interface ImageQueryUsed {
  picture: boolean
  description: boolean
  stored: boolean
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
  /** Name and slug are emptied because the space is closed to this reader. */
  hidden?: boolean
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

/** The pictures a picture-search answer found (empty for any other message). */
export function messageImageMatches(content: MessageContent): ImageMatch[] {
  const raw = (content as { image_matches?: unknown }).image_matches
  return Array.isArray(raw) ? (raw as ImageMatch[]) : []
}

/**
 * True when the whole answer is pictures (a picture search or a list), false when pictures
 * only ride along with an ordinary text answer as "possibly related".
 */
export function messageIsPictureAnswer(content: MessageContent): boolean {
  const kind = (content as { meta?: { kind?: unknown } }).meta?.kind
  return kind === 'image_search' || kind === 'image_list'
}

/** What the person asked with, if this message is a picture-search question. */
export function messageImageQuery(content: MessageContent): ImageQueryUsed | null {
  const raw = (content as { image_query?: unknown }).image_query
  if (!raw || typeof raw !== 'object') return null
  const q = raw as Partial<ImageQueryUsed>
  return {
    picture: q.picture === true,
    description: q.description === true,
    stored: q.picture === true && q.stored === true,
  }
}
