/**
 * Thin fetch wrapper used by every tenant-scoped feature.
 *
 * The caller passes ``DevCredentials`` when they exist. The wrapper adds:
 *   - ``Authorization: Bearer <adminToken>``
 *   - ``X-Quberi-Organization: <slug>``
 *   - ``X-Quberi-User-Email: <email>``
 *
 * Anything the server returns as JSON is passed back to the caller as
 * ``T``. ``ApiError`` carries the HTTP status plus the ``detail`` string
 * from the API so UI code can render targeted messages (401 vs 409 vs 500).
 */

import type { DevCredentials } from '../auth/types'
import { getGuestToken } from '../auth/guestToken'

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  /** Machine-readable reason the server gave (e.g. ``session_invalid``), when it gave one. */
  readonly code?: string

  constructor(status: number, detail: string, code?: string) {
    super(detail || `Request failed with status ${status}`)
    this.status = status
    this.detail = detail
    this.code = code
  }
}

/** Fired on ``window`` when the server says the signed-in session has ended. */
export const SESSION_INVALID_EVENT = 'quberi:session-invalid'

/**
 * Tell the app the session is over, but only for the one answer that means it:
 * 401 with ``code: "session_invalid"``. A 401 for a wrong sign-in code carries no
 * such code and must not sign anyone out.
 */
export function notifyIfSessionInvalid(status: number, code?: string): void {
  if (status === 401 && code === 'session_invalid') {
    window.dispatchEvent(new Event(SESSION_INVALID_EVENT))
  }
}

/**
 * Turn a failed ``Response`` into an ``ApiError`` (reading ``detail`` and ``code``
 * from a JSON body when there is one) and raise the session-ended signal if it applies.
 * ``fallback`` is used as the message when the server sent none.
 */
export async function apiErrorFrom(response: Response, fallback = ''): Promise<ApiError> {
  let detail = ''
  let code: string | undefined
  try {
    const payload = (await response.json()) as { detail?: unknown; code?: unknown }
    if (typeof payload.detail === 'string') detail = payload.detail
    else if (payload.detail) detail = JSON.stringify(payload.detail)
    if (typeof payload.code === 'string') code = payload.code
  } catch {
    // no JSON body: the status is all we have
  }
  notifyIfSessionInvalid(response.status, code)
  return new ApiError(response.status, detail || fallback, code)
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  credentials?: DevCredentials | null
  signal?: AbortSignal
  /** Allow non-2xx status codes without throwing (e.g. 503 for /health/ready). */
  allowUnready?: boolean
  /** Override the default 10s client-side timeout. Long-running actions
   * (e.g. a Feishu folder sync that fetches + embeds many docs) need
   * more headroom. */
  timeoutMs?: number
}

export async function apiFetch<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
  }
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }
  if (opts.credentials) {
    Object.assign(headers, buildAuthHeaders(opts.credentials))
  }

  const controller = new AbortController()
  const timeout = window.setTimeout(
    () => controller.abort(),
    opts.timeoutMs ?? 10_000,
  )

  // Chain the caller's signal into our timeout controller so both can cancel.
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort()
    else opts.signal.addEventListener('abort', () => controller.abort(), { once: true })
  }

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    })

    if (!response.ok && !(opts.allowUnready && response.status === 503)) {
      throw await apiErrorFrom(response)
    }

    // 204 (and any other body-less response) → return ``undefined`` cast as T.
    // Callers that expect void can happily receive it; callers that expect
    // JSON should never invoke a DELETE (or any 204) endpoint via a typed
    // helper that promises data.
    if (response.status === 204 || response.headers.get('content-length') === '0') {
      return undefined as T
    }
    return (await response.json()) as T
  } finally {
    window.clearTimeout(timeout)
  }
}

export const API_BASE = API_BASE_URL

/**
 * Build the auth headers for a request. A real session token is sent as a
 * plain bearer credential; the legacy dev credential sends the admin token
 * plus the impersonation headers. Shared by ``apiFetch`` and the SSE
 * streaming helpers (which hand-roll their own ``fetch``).
 */
export function buildAuthHeaders(credentials: DevCredentials): Record<string, string> {
  if (credentials.sessionToken) {
    // A guest also identifies its browser, so the guest sandbox can tell which
    // files it uploaded. Signed-in users never send this.
    return credentials.isGuest
      ? { Authorization: `Bearer ${credentials.sessionToken}`, 'X-Guest-Token': getGuestToken() }
      : { Authorization: `Bearer ${credentials.sessionToken}` }
  }
  return {
    Authorization: `Bearer ${credentials.adminToken}`,
    'X-Quberi-Organization': credentials.organizationSlug,
    'X-Quberi-User-Email': credentials.userEmail,
  }
}
