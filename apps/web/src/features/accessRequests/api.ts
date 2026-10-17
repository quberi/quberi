import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

export interface AccessRequest {
  id: string
  space_id: string
  space_name: string
  requester_user_id: string
  requester_name: string
  requester_email: string
  role: 'viewer' | 'editor'
  reason: string
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'
  decided_by_name: string | null
  decision_note: string | null
  created_at: string
  decided_at: string | null
}

export interface AccessRequestList {
  items: AccessRequest[]
  total: number
}

/** Requests waiting for me to decide, as an owner of the space. */
export function listRequestsToDecide(credentials: DevCredentials): Promise<AccessRequestList> {
  return apiFetch<AccessRequestList>('/api/v1/access-requests?scope=to_decide', { credentials })
}

export function createAccessRequest(
  credentials: DevCredentials,
  input: { space_id: string; role: 'viewer' | 'editor'; reason: string },
): Promise<AccessRequest> {
  return apiFetch<AccessRequest>('/api/v1/access-requests', {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function decideAccessRequest(
  credentials: DevCredentials,
  requestId: string,
  approve: boolean,
  note: string,
): Promise<AccessRequest> {
  return apiFetch<AccessRequest>(
    `/api/v1/access-requests/${requestId}/${approve ? 'approve' : 'reject'}`,
    { method: 'POST', body: { note }, credentials },
  )
}

export function cancelAccessRequest(
  credentials: DevCredentials,
  requestId: string,
): Promise<AccessRequest> {
  return apiFetch<AccessRequest>(`/api/v1/access-requests/${requestId}/cancel`, {
    method: 'POST',
    credentials,
  })
}
