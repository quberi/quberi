import { API_BASE, ApiError, apiFetch, buildAuthHeaders } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type { MessageCitation } from './types'

/**
 * Client for the guest demo assistant (``/api/v1/guest``).
 *
 * Stateless on the server: nothing a visitor asks is stored, so the browser
 * keeps the conversation and sends the last few turns with each question.
 */

export interface GuestQuota {
  /** Questions allowed per day; 0 = uncapped. */
  limit: number
  /** Questions left today; -1 = uncapped. */
  remaining: number
}

export interface GuestTurn {
  role: 'user' | 'assistant'
  text: string
}

export type GuestStreamEvent =
  | { kind: 'quota'; quota: GuestQuota }
  | { kind: 'retrieved'; citations: MessageCitation[] }
  | { kind: 'delta'; text: string }
  | { kind: 'error'; message: string }
  | { kind: 'done'; answer: string | null; citations: MessageCitation[] | null }

export interface GuestStarter {
  key: string
  /** Asks over the last 30 days. */
  count: number
  /** Among the most-asked, with enough asks to mean something. */
  hot: boolean
}

/** Suggested questions, most-asked first. */
export async function getGuestStarters(credentials: DevCredentials): Promise<GuestStarter[]> {
  const res = await apiFetch<{ items: GuestStarter[] }>('/api/v1/guest/starters', {
    credentials,
  })
  return res.items
}

export async function getGuestQuota(credentials: DevCredentials): Promise<GuestQuota> {
  const res = await apiFetch<{ limit: number; remaining: number }>('/api/v1/guest/quota', {
    credentials,
  })
  return { limit: res.limit, remaining: res.remaining }
}

/** Ask a question; yields events until the answer is complete. */
export async function* streamGuestAsk(
  credentials: DevCredentials,
  text: string,
  history: GuestTurn[],
  locale: string,
  /** Key of the suggested question the visitor clicked, if any (for popularity). */
  starter?: string,
  signal?: AbortSignal,
): AsyncGenerator<GuestStreamEvent> {
  const response = await fetch(`${API_BASE}/api/v1/guest/ask/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...buildAuthHeaders(credentials),
    },
    body: JSON.stringify({ text, history, locale, starter }),
    signal,
  })

  if (!response.ok || !response.body) {
    let detail = ''
    try {
      const body = (await response.json()) as { detail?: unknown }
      // FastAPI sends a string for our errors, a list for validation errors.
      detail = typeof body.detail === 'string' ? body.detail : ''
    } catch {
      /* fall through */
    }
    throw new ApiError(response.status, detail || 'guest question failed')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const frame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const parsed = parseFrame(frame)
        if (parsed) yield parsed
        sep = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}

function parseFrame(frame: string): GuestStreamEvent | null {
  let kind = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) kind = line.slice('event:'.length).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice('data:'.length).trim())
  }
  if (dataLines.length === 0) return null
  let data: Record<string, unknown>
  try {
    data = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
  } catch {
    return null
  }
  switch (kind) {
    case 'quota':
      return {
        kind: 'quota',
        quota: { limit: Number(data.limit ?? 0), remaining: Number(data.remaining ?? -1) },
      }
    case 'retrieved':
      return { kind: 'retrieved', citations: (data.citations as MessageCitation[]) ?? [] }
    case 'delta':
      return { kind: 'delta', text: String(data.text ?? '') }
    case 'error':
      return { kind: 'error', message: String(data.message ?? 'unknown') }
    case 'done':
      return {
        kind: 'done',
        answer: typeof data.answer === 'string' ? data.answer : null,
        citations: Array.isArray(data.citations) ? (data.citations as MessageCitation[]) : null,
      }
    default:
      return null
  }
}
