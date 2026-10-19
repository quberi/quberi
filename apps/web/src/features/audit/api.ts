import { API_BASE, apiErrorFrom, apiFetch, buildAuthHeaders } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type { AuditEventPage, AuditFilters, RecentEventList } from './types'

export function listRecentEvents(
  credentials: DevCredentials,
  { limit = 20 }: { limit?: number } = {},
): Promise<RecentEventList> {
  return apiFetch<RecentEventList>(
    `/api/v1/organizations/current/recent-events?limit=${limit}`,
    { credentials },
  )
}

function filterQuery(filters: AuditFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.action) params.set('action', filters.action)
  if (filters.actorId) params.set('actor_id', filters.actorId)
  if (filters.spaceId) params.set('space_id', filters.spaceId)
  if (filters.since) params.set('since', filters.since)
  if (filters.until) params.set('until', filters.until)
  return params
}

/** One newest-first page of the audit log; pass ``cursor`` for the next one. */
export function searchAuditEvents(
  credentials: DevCredentials,
  filters: AuditFilters,
  { cursor, limit = 50 }: { cursor?: string | null; limit?: number } = {},
): Promise<AuditEventPage> {
  const params = filterQuery(filters)
  params.set('limit', String(limit))
  if (cursor) params.set('cursor', cursor)
  return apiFetch<AuditEventPage>(
    `/api/v1/organizations/current/audit-events?${params.toString()}`,
    { credentials },
  )
}

export function listAuditActions(credentials: DevCredentials): Promise<{ items: string[] }> {
  return apiFetch<{ items: string[] }>(
    '/api/v1/organizations/current/audit-events/actions',
    { credentials },
  )
}

/** Download the filtered log as CSV. Resolves with the file as a Blob. */
export async function exportAuditCsv(
  credentials: DevCredentials,
  filters: AuditFilters,
): Promise<Blob> {
  const query = filterQuery(filters).toString()
  const response = await fetch(
    `${API_BASE}/api/v1/organizations/current/audit-events.csv${query ? `?${query}` : ''}`,
    { headers: buildAuthHeaders(credentials) },
  )
  if (!response.ok) throw await apiErrorFrom(response)
  return response.blob()
}
