import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'
import type { AccessSubjectType } from '../spaceAccess/types'

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
