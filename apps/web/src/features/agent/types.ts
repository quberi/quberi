/** How an agent resolves which knowledge spaces its search tool reads. */
export type KnowledgeScopeMode = 'user' | 'intersect' | 'fixed'

/** Who may see + run an agent. */
export type AgentVisibility = 'organization' | 'restricted'

/** An agent persona (config record) as returned by the API. */
/** builtin = in-house tool loop; external = forward to a 3rd-party endpoint. */
export type AgentKind = 'builtin' | 'external'

export interface AgentSummary {
  id: string
  name: string
  description: string | null
  icon: string
  enabled_tools: string[]
  knowledge_scope_mode: KnowledgeScopeMode
  scope_space_ids: string[]
  visibility: AgentVisibility
  is_default: boolean
  created_at: string
  kind: AgentKind
  external_protocol: string | null
  external_endpoint: string | null
  external_model: string | null
  external_bot_id: string | null
  external_api_key_present: boolean
}

/** Full config, including the editable system prompt. */
export interface AgentDetail extends AgentSummary {
  system_prompt: string
}

/** A subject that may be granted access to a restricted agent. */
export type AgentSubjectType = 'user' | 'group' | 'organization_unit' | 'position'

/** One access grant on a restricted agent. */
export interface AgentAccessGrant {
  id: string
  subject_type: AgentSubjectType
  subject_id: string
  subject_display_name: string
  subject_subtitle: string | null
  created_at: string
}

export interface AgentAccessList {
  items: AgentAccessGrant[]
  total: number
}

export interface AgentList {
  items: AgentSummary[]
  total: number
}

/** One built-in tool the agent can be given. */
export interface ToolInfo {
  name: string
  description: string
}

export interface ToolCatalog {
  items: ToolInfo[]
}

/** Payload for creating/updating an agent. */
export interface AgentInput {
  name: string
  description?: string | null
  icon?: string
  system_prompt?: string
  enabled_tools: string[]
  knowledge_scope_mode?: KnowledgeScopeMode
  scope_space_ids?: string[]
  visibility?: AgentVisibility
  // External agent (kind='external'):
  kind?: AgentKind
  external_protocol?: string
  external_endpoint?: string
  external_model?: string
  external_bot_id?: string
  external_extra?: Record<string, unknown>
  external_api_key?: string
}
