import { API_BASE, ApiError, apiFetch, buildAuthHeaders } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  AgentAccessGrant,
  AgentAccessList,
  AgentDetail,
  AgentInput,
  AgentList,
  AgentSubjectType,
  ToolCatalog,
} from './types'

// ---------------------------------------------------------------------------
// Agent config CRUD
// ---------------------------------------------------------------------------

export function listAgents(credentials: DevCredentials): Promise<AgentList> {
  return apiFetch<AgentList>('/api/v1/agents', { credentials })
}

export function getAgent(
  credentials: DevCredentials,
  agentId: string,
): Promise<AgentDetail> {
  return apiFetch<AgentDetail>(`/api/v1/agents/${agentId}`, { credentials })
}

export function listTools(credentials: DevCredentials): Promise<ToolCatalog> {
  return apiFetch<ToolCatalog>('/api/v1/agents/tools', { credentials })
}

export function createAgent(
  credentials: DevCredentials,
  input: AgentInput,
): Promise<AgentDetail> {
  return apiFetch<AgentDetail>('/api/v1/agents', {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function updateAgent(
  credentials: DevCredentials,
  agentId: string,
  input: Partial<AgentInput>,
): Promise<AgentDetail> {
  return apiFetch<AgentDetail>(`/api/v1/agents/${agentId}`, {
    method: 'PATCH',
    body: input,
    credentials,
  })
}

export function deleteAgent(
  credentials: DevCredentials,
  agentId: string,
): Promise<void> {
  return apiFetch<void>(`/api/v1/agents/${agentId}`, {
    method: 'DELETE',
    credentials,
  })
}

// ---------------------------------------------------------------------------
// Agent access grants (who may use a restricted agent)
// ---------------------------------------------------------------------------

export function listAgentAccess(
  credentials: DevCredentials,
  agentId: string,
): Promise<AgentAccessList> {
  return apiFetch<AgentAccessList>(`/api/v1/agents/${agentId}/access`, {
    credentials,
  })
}

export function grantAgentAccess(
  credentials: DevCredentials,
  agentId: string,
  subjectType: AgentSubjectType,
  subjectId: string,
): Promise<AgentAccessGrant> {
  return apiFetch<AgentAccessGrant>(`/api/v1/agents/${agentId}/access`, {
    method: 'POST',
    body: { subject_type: subjectType, subject_id: subjectId },
    credentials,
  })
}

export function revokeAgentAccess(
  credentials: DevCredentials,
  agentId: string,
  bindingId: string,
): Promise<void> {
  return apiFetch<void>(`/api/v1/agents/${agentId}/access/${bindingId}`, {
    method: 'DELETE',
    credentials,
  })
}

/**
 * Agent run — SSE over POST.
 *
 * The backend streams the agent's *reasoning* (each tool call and its
 * result) followed by the final answer, token by token. Frame form is
 * ``event: <kind>\ndata: <json>\n\n`` — same wire format as the chat
 * stream, so we reuse the parse-by-blank-line approach.
 */

export interface ToolCallArgs {
  [key: string]: unknown
}

export type AgentStreamEvent =
  | { kind: 'note'; text: string }
  | { kind: 'tool_call'; tool: string; arguments: ToolCallArgs }
  | { kind: 'tool_result'; tool: string; result: Record<string, unknown> }
  | { kind: 'delta'; text: string }
  | { kind: 'error'; message: string }
  | {
      kind: 'done'
      iterations?: number
      tool_call_count?: number
      model_id?: string
    }

export async function* streamAgentRun(
  credentials: DevCredentials,
  agentId: string,
  query: string,
  signal?: AbortSignal,
): AsyncGenerator<AgentStreamEvent> {
  const response = await fetch(
    `${API_BASE}/api/v1/agents/${agentId}/run/stream`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...buildAuthHeaders(credentials),
      },
      body: JSON.stringify({ query }),
      signal,
    },
  )
  if (!response.ok || !response.body) {
    let detail = ''
    try {
      const body = (await response.json()) as { detail?: string }
      detail = body.detail ?? ''
    } catch {
      /* fall through */
    }
    throw new ApiError(response.status, detail || 'agent stream open failed')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sepIdx = buffer.indexOf('\n\n')
      while (sepIdx !== -1) {
        const frame = buffer.slice(0, sepIdx)
        buffer = buffer.slice(sepIdx + 2)
        const parsed = parseFrame(frame)
        if (parsed) yield parsed
        sepIdx = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}

function parseFrame(frame: string): AgentStreamEvent | null {
  let eventKind = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      eventKind = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trim())
    }
  }
  if (dataLines.length === 0) return null
  let data: Record<string, unknown> = {}
  try {
    data = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
  } catch {
    return null
  }
  switch (eventKind) {
    case 'note':
      return { kind: 'note', text: String(data.text ?? '') }
    case 'tool_call':
      return {
        kind: 'tool_call',
        tool: String(data.tool ?? ''),
        arguments: (data.arguments as ToolCallArgs | undefined) ?? {},
      }
    case 'tool_result':
      return {
        kind: 'tool_result',
        tool: String(data.tool ?? ''),
        result: (data.result as Record<string, unknown> | undefined) ?? {},
      }
    case 'delta':
      return { kind: 'delta', text: String(data.text ?? '') }
    case 'error':
      return { kind: 'error', message: String(data.message ?? 'unknown') }
    case 'done':
      return {
        kind: 'done',
        iterations: data.iterations as number | undefined,
        tool_call_count: data.tool_call_count as number | undefined,
        model_id: data.model_id as string | undefined,
      }
    default:
      return null
  }
}
