/**
 * Shapes for the ``GET /organizations/current/recent-events`` endpoint.
 * Kept in lock-step with ``apps/api/src/quberi_api/api/v1/organizations/audit.py``.
 */

export type ActorType = 'user' | 'service_account' | 'system'

export interface RecentEvent {
  id: string
  created_at: string
  actor_type: ActorType
  actor_id: string | null
  actor_display_name: string
  action: string
  resource_type: string
  resource_id: string | null
  resource_display_name: string
  space_id: string | null
  space_display_name: string | null
  payload: Record<string, unknown>
  /** Documents retrieval put in front of the model (``message.sent`` only). */
  retrieved_documents?: RetrievedDocument[]
}

export interface RetrievedDocument {
  id: string
  /** ``null`` once the document has been deleted. */
  name: string | null
}

export interface AuditEventPage {
  items: RecentEvent[]
  next_cursor: string | null
}

export interface AuditFilters {
  action?: string
  actorId?: string
  spaceId?: string
  since?: string
  until?: string
}

export interface RecentEventList {
  items: RecentEvent[]
  total: number
}
