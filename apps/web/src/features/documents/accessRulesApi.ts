import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'
import type { AccessSubjectType, DocumentAccessSummary } from '../spaceAccess/types'

/** One document-level rule: ``allow`` shares the document, ``deny`` hides it (deny wins). */
export type AccessEffect = 'allow' | 'deny'

export interface DocumentAccessRule {
  id: string
  effect: AccessEffect
  subject_type: AccessSubjectType
  subject_id: string
  subject_display_name: string
  subject_subtitle: string | null
  created_at: string
}

export interface DocumentAccessRuleList {
  items: DocumentAccessRule[]
  total: number
}

export interface AddDocumentAccessRuleInput {
  effect: AccessEffect
  subject_type: AccessSubjectType
  /** Omit for ``organization``: the server uses the caller's own organisation. */
  subject_id?: string
}

const base = (spaceId: string, documentId: string) =>
  `/api/v1/spaces/${spaceId}/documents/${documentId}/access-rules`

export function listAccessRules(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
): Promise<DocumentAccessRuleList> {
  return apiFetch<DocumentAccessRuleList>(base(spaceId, documentId), { credentials })
}

export function addAccessRule(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
  input: AddDocumentAccessRuleInput,
): Promise<DocumentAccessRule> {
  return apiFetch<DocumentAccessRule>(base(spaceId, documentId), {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function removeAccessRule(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
  ruleId: string,
): Promise<void> {
  return apiFetch<void>(`${base(spaceId, documentId)}/${ruleId}`, {
    method: 'DELETE',
    credentials,
  })
}

/** A document as a space manager sees it when choosing one to set access on: no content. */
export interface ManagedDocument {
  id: string
  name: string
  mime_type: string
  size_bytes: number
  updated_at: string
  allow_rules: number
  deny_rules: number
}

export interface ManagedDocumentList {
  items: ManagedDocument[]
  total: number
  limit: number
  offset: number
}

export function listManagedDocuments(
  credentials: DevCredentials,
  spaceId: string,
  params: { q?: string; offset?: number; limit?: number } = {},
): Promise<ManagedDocumentList> {
  const query = new URLSearchParams()
  if (params.q) query.set('q', params.q)
  if (params.offset) query.set('offset', String(params.offset))
  if (params.limit) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return apiFetch<ManagedDocumentList>(
    `/api/v1/spaces/${spaceId}/access-rules/documents${suffix}`,
    { credentials },
  )
}

export type AccessOutcome =
  | 'account_blocked'
  | 'denied_by_rule'
  | 'waiting_for_approval'
  | 'allowed_by_space'
  | 'allowed_by_share'
  | 'no_access'

/** One reason behind the answer. ``code`` is stable and is what the screen translates. */
export interface AccessCheckStep {
  kind: string
  effect: 'grants' | 'blocks' | 'ignored' | 'info'
  code: string
  /** A sentence for someone reading the raw answer; the screen prefers its own wording. */
  summary: string
  subject_type: string | null
  subject_id: string | null
  subject_display_name: string | null
  role_code: string | null
  hit_path: string | null
  rule_id: string | null
}

export interface AccessCheck {
  user_id: string
  user_display_name: string
  user_email: string
  user_status: string
  document_id: string
  document_name: string
  space_id: string
  space_name: string
  review_state: string
  indexing_state: string
  can_read: boolean
  outcome: AccessOutcome
  space_role: string | null
  appears_in_search: boolean
  steps: AccessCheckStep[]
}

/** Why one member can or cannot read this document, worked out from the live rules. */
export function checkDocumentAccess(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
  userId: string,
): Promise<AccessCheck> {
  const query = new URLSearchParams({ user_id: userId })
  return apiFetch<AccessCheck>(
    `/api/v1/spaces/${spaceId}/documents/${documentId}/access-check?${query.toString()}`,
    { credentials },
  )
}

/** How many people can read this document, and how many exceptions it has. */
export function getDocumentAccessSummary(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
): Promise<DocumentAccessSummary> {
  return apiFetch<DocumentAccessSummary>(
    `/api/v1/spaces/${spaceId}/documents/${documentId}/access-summary`,
    { credentials },
  )
}
