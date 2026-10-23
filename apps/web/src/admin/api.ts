/**
 * Minimal typed client for the platform-admin stats API.
 *
 * Deliberately separate from ``features/api/client.ts``: that client is
 * built around tenant-scoped ``DevCredentials`` (session token or
 * org+email impersonation headers). This dashboard authenticates with a
 * single bearer token — the same ``QUBERI_PLATFORM_ADMIN_TOKEN`` already
 * used for the curl-only ``/api/v1/admin/*`` surface.
 */

import { getLang, LOCALE, type MessageRef, type Params } from './i18n'

export type { MessageRef }

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/$/, '')

export class AdminApiError extends Error {
  readonly status: number
  /** The server's own message, '' when it sent none. */
  readonly detail: string
  /** Stable message code (e.g. `admin.auth.bad_credentials`); `validation` for a 422. */
  readonly code: string | null
  readonly params: Params | undefined
  constructor(status: number, detail: string, code: string | null = null, params?: Params) {
    super(detail || `Request failed with status ${status}`)
    this.status = status
    this.detail = detail
    this.code = code
    this.params = params
  }
}

/** Read an error response: its text, its message code and the code's parameters. */
async function readErrorBody(
  response: Response,
): Promise<{ detail: string; code: string | null; params?: Params }> {
  try {
    const payload = (await response.json()) as {
      detail?: unknown
      code?: unknown
      params?: unknown
    }
    // A FastAPI 422 sends `detail` as a list of problems, not a string.
    if (Array.isArray(payload.detail)) return { detail: '', code: 'validation' }
    const params =
      typeof payload.params === 'object' && payload.params !== null
        ? (payload.params as Params)
        : undefined
    return {
      detail: typeof payload.detail === 'string' ? payload.detail : '',
      code: typeof payload.code === 'string' ? payload.code : null,
      params,
    }
  } catch {
    return { detail: '', code: null }
  }
}

/** Asks the server for messages in the console's language. Read per call, so a toggle applies next. */
function acceptLanguage(): string {
  return LOCALE[getLang()]
}

async function adminFetch<T>(
  token: string,
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: init.method ?? 'GET',
    headers: {
      Accept: 'application/json',
      'Accept-Language': acceptLanguage(),
      Authorization: `Bearer ${token}`,
      ...(init.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  })
  if (!response.ok) {
    const { detail, code, params } = await readErrorBody(response)
    throw new AdminApiError(response.status, detail, code, params)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export interface AdminSession {
  token: string
  expires_at: number
  username: string
  must_change_password: boolean
}

/** Sign in with a platform admin account (no bearer token yet, so no adminFetch). */
export async function adminLogin(
  username: string,
  password: string,
): Promise<AdminSession> {
  const response = await fetch(`${API_BASE_URL}/api/v1/admin/auth/login`, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Accept-Language': acceptLanguage(),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ username, password }),
  })
  if (!response.ok) {
    const { detail, code, params } = await readErrorBody(response)
    throw new AdminApiError(response.status, detail, code, params)
  }
  return (await response.json()) as AdminSession
}

/** The signed-in account; used to learn whether a password change is pending. */
export function getAdminMe(
  token: string,
): Promise<{ username: string; must_change_password: boolean }> {
  return adminFetch(token, '/api/v1/admin/auth/me')
}

/** Change the password; returns a fresh session (older ones stop working). */
export function changeAdminPassword(
  token: string,
  currentPassword: string,
  newPassword: string,
): Promise<AdminSession> {
  return adminFetch(token, '/api/v1/admin/auth/change-password', {
    method: 'POST',
    body: { current_password: currentPassword, new_password: newPassword },
  })
}

export interface PlatformTotals {
  organizations: number
  organizations_personal: number
  organizations_team: number
  users: number
  identities: number
  spaces: number
  documents: number
  documents_indexed: number
  conversations: number
  messages: number
}

export interface SignupPoint {
  day: string
  organizations: number
  identities: number
}

export interface PlatformOverview {
  totals: PlatformTotals
  new_organizations_24h: number
  new_organizations_7d: number
  new_identities_24h: number
  new_identities_7d: number
  active_organizations_7d: number
  signups_by_day: SignupPoint[]
}

export function getOverview(token: string): Promise<PlatformOverview> {
  return adminFetch(token, '/api/v1/admin/stats/overview')
}

export interface OrganizationStatsRow {
  id: string
  name: string
  slug: string
  tier: string
  status: string
  created_at: string
  member_count: number
  space_count: number
  document_count: number
  conversation_count: number
  message_count: number
  owner_email: string | null
  is_demo: boolean
}

export interface OrganizationStatsList {
  items: OrganizationStatsRow[]
  total: number
}

export function getOrganizations(
  token: string,
  opts: { limit?: number; offset?: number } = {},
): Promise<OrganizationStatsList> {
  const q = new URLSearchParams()
  if (opts.limit) q.set('limit', String(opts.limit))
  if (opts.offset) q.set('offset', String(opts.offset))
  const qs = q.toString()
  return adminFetch(token, `/api/v1/admin/stats/organizations${qs ? `?${qs}` : ''}`)
}

export interface PlatformBindingRow {
  id: string
  provider_kind: string
  display_name: string
  base_url: string | null
  status: string
  models: string[]
}

export function getPlatformBindings(token: string): Promise<PlatformBindingRow[]> {
  return adminFetch(token, '/api/v1/admin/stats/platform-bindings')
}

// ---------------------------------------------------------------------------
// Platform model management (mirrors apps/api/.../admin/ai_providers.py)
// ---------------------------------------------------------------------------

export interface PlatformBindingDetail {
  id: string
  provider_kind: string
  vendor: string | null
  vendor_display_name: string
  display_name: string
  base_url: string | null
  api_key_present: boolean
  status: string
  priority: number
  models: string[]
  created_at: string
  updated_at: string
}

export function listPlatformBindings(
  token: string,
): Promise<{ items: PlatformBindingDetail[] }> {
  return adminFetch(token, '/api/v1/admin/ai-providers')
}

export function createPlatformBinding(
  token: string,
  input: {
    provider_kind: string
    vendor?: string | null
    display_name: string
    base_url?: string | null
    api_key?: string | null
    priority?: number
    models?: string[]
  },
): Promise<PlatformBindingDetail> {
  return adminFetch(token, '/api/v1/admin/ai-providers', { method: 'POST', body: input })
}

export function updatePlatformBinding(
  token: string,
  bindingId: string,
  input: {
    display_name?: string
    base_url?: string | null
    api_key?: string
    status?: string
    priority?: number
    models?: string[]
  },
): Promise<PlatformBindingDetail> {
  return adminFetch(token, `/api/v1/admin/ai-providers/${bindingId}`, {
    method: 'PATCH',
    body: input,
  })
}

export function deletePlatformBinding(token: string, bindingId: string): Promise<void> {
  return adminFetch(token, `/api/v1/admin/ai-providers/${bindingId}`, { method: 'DELETE' })
}

export interface ModelCatalogEntry {
  model_id: string
  provider_kind: string
  purpose: 'chat' | 'embedding' | 'reranker'
  display_name: string
  vendor: string | null
  vendor_display_name: string
  context_window: number
  notes: string
  /** Whether the model reads pictures, so it can be offered as a vision model. */
  supports_vision?: boolean
}

export function getModelCatalog(token: string): Promise<ModelCatalogEntry[]> {
  return adminFetch(token, '/api/v1/admin/ai-models')
}

export interface PlatformModelVisibility {
  organization_id: string
  mode: 'all' | 'none' | 'allowlist'
  model_ids: string[]
}

export function getOrgPlatformModelVisibility(
  token: string,
  organizationId: string,
): Promise<PlatformModelVisibility> {
  return adminFetch(
    token,
    `/api/v1/admin/organizations/${organizationId}/platform-model-visibility`,
  )
}

export function setOrgPlatformModelVisibility(
  token: string,
  organizationId: string,
  input: { mode: 'all' | 'none' | 'allowlist'; model_ids?: string[] },
): Promise<PlatformModelVisibility> {
  return adminFetch(
    token,
    `/api/v1/admin/organizations/${organizationId}/platform-model-visibility`,
    { method: 'PUT', body: input },
  )
}

// ---------------------------------------------------------------------------
// Outside tools allowed to ask for access (OAuth clients)
// ---------------------------------------------------------------------------

export type OAuthClientKind = 'registered' | 'metadata_url'

export interface OAuthClientRow {
  id: string
  client_id: string
  kind: OAuthClientKind
  name: string
  redirect_uris: string[]
  enabled: boolean
  allow_mcp: boolean
  note: string | null
  created_at: string
  updated_at: string
}

export interface RejectedAttempt {
  client_id: string
  redirect_uri: string
  reason: 'unknown_client' | 'redirect_not_allowed' | string
  at: string
}

export interface OAuthClientList {
  items: OAuthClientRow[]
  rejected: RejectedAttempt[]
  advertises_metadata_documents: boolean
}

export interface OAuthClientInput {
  client_id: string
  kind: OAuthClientKind
  name: string
  redirect_uris: string[]
  enabled: boolean
  allow_mcp: boolean
  note: string | null
}

export function listOAuthClients(token: string): Promise<OAuthClientList> {
  return adminFetch(token, '/api/v1/admin/oauth-clients')
}

export function createOAuthClient(token: string, input: OAuthClientInput): Promise<OAuthClientRow> {
  return adminFetch(token, '/api/v1/admin/oauth-clients', { method: 'POST', body: input })
}

export function updateOAuthClient(
  token: string,
  id: string,
  input: Partial<OAuthClientInput>,
): Promise<OAuthClientRow> {
  return adminFetch(token, `/api/v1/admin/oauth-clients/${id}`, { method: 'PATCH', body: input })
}

export function deleteOAuthClient(token: string, id: string): Promise<void> {
  return adminFetch(token, `/api/v1/admin/oauth-clients/${id}`, { method: 'DELETE' })
}

// ---------------------------------------------------------------------------
// Traffic (mirrors apps/api/.../admin/traffic.py)
// ---------------------------------------------------------------------------

export type TrafficKind =
  | 'direct_or_unknown_host'
  | 'search_engine'
  | 'mcp_directory_or_probe'
  | 'scanner_or_tool'
  | 'mcp_client'
  | 'browser_like'
  | 'other'

export type TrafficDisabledReason = 'not_configured' | 'file_missing' | 'unreadable'

export interface TrafficSummary {
  enabled: boolean
  reason: TrafficDisabledReason | null
  window: { from: string; to: string }
  files_read: number
  truncated_by_cap: boolean
  address_note: string
  /** Message code for `address_note`; the console renders it from its own dictionary. */
  address_note_code?: string
  totals: {
    requests: number
    distinct_visitors: number
    automated_requests: number
    automated_share: number
  }
  by_site: { host: string; requests: number }[]
  by_kind: { kind: TrafficKind; requests: number }[]
  by_status_class: { status_class: string; requests: number }[]
  by_country: { country: string; requests: number }[]
  mcp: {
    top_user_agents: { user_agent: string; kind: TrafficKind; requests: number }[]
    top_paths: { method: string; path: string; status: number; requests: number }[]
  }
  web_pages: { path: string; browser_like: number; search_engine: number; total: number }[]
  signals: {
    guest_questions: number
    sign_in_requests: number
    oauth_requests: number
    mcp_successful_posts: number
  }
  top_visitors: {
    address: string
    address_source: 'header' | 'remote_ip'
    kind: TrafficKind
    country: string
    user_agent: string
    requests: number
  }[]
  hourly: { hour: string; requests: number; automated_requests: number }[]
}

/** Summary of the proxy's access log for the last ``hours`` hours (1 to 336). */
export function getTraffic(
  token: string,
  opts: { hours: number; excludeIps?: string },
): Promise<TrafficSummary> {
  const params = new URLSearchParams({ hours: String(opts.hours) })
  if (opts.excludeIps) params.set('exclude_ips', opts.excludeIps)
  return adminFetch(token, `/api/v1/admin/traffic?${params.toString()}`)
}
