/**
 * Minimal typed client for the platform-admin stats API.
 *
 * Deliberately separate from ``features/api/client.ts``: that client is
 * built around tenant-scoped ``DevCredentials`` (session token or
 * org+email impersonation headers). This dashboard authenticates with a
 * single bearer token — the same ``QUBERI_PLATFORM_ADMIN_TOKEN`` already
 * used for the curl-only ``/api/v1/admin/*`` surface.
 */

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/$/, '')

export class AdminApiError extends Error {
  readonly status: number
  constructor(status: number, detail: string) {
    super(detail || `Request failed with status ${status}`)
    this.status = status
  }
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
      Authorization: `Bearer ${token}`,
      ...(init.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  })
  if (!response.ok) {
    let detail = ''
    try {
      const payload = (await response.json()) as { detail?: string }
      detail = payload.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new AdminApiError(response.status, detail)
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
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!response.ok) {
    let detail = ''
    try {
      detail = ((await response.json()) as { detail?: string }).detail ?? ''
    } catch {
      /* ignore */
    }
    throw new AdminApiError(response.status, detail)
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
