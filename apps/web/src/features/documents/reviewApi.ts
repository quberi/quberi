import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

export interface ReviewItem {
  id: string
  name: string
  mime_type: string
  size_bytes: number
  uploader_name: string
  created_at: string
  review_state: 'pending' | 'rejected'
  review_note: string | null
  reviewed_by_name: string | null
}

export interface ReviewItemList {
  items: ReviewItem[]
  total: number
}

export interface PendingSpace {
  space_id: string
  space_name: string
  pending: number
}

export interface PendingSpaceList {
  items: PendingSpace[]
  total_pending: number
}

export function setUploadReview(
  credentials: DevCredentials,
  spaceId: string,
  enabled: boolean,
): Promise<{ space_id: string; enabled: boolean }> {
  return apiFetch(`/api/v1/spaces/${spaceId}/upload-review`, {
    method: 'PUT',
    body: { enabled },
    credentials,
  })
}

export function listReviewDocuments(
  credentials: DevCredentials,
  spaceId: string,
  state: 'pending' | 'rejected',
): Promise<ReviewItemList> {
  return apiFetch<ReviewItemList>(
    `/api/v1/spaces/${spaceId}/review/documents?state=${state}`,
    { credentials },
  )
}

export function decideReview(
  credentials: DevCredentials,
  spaceId: string,
  documentIds: string[],
  action: 'approve' | 'reject',
  note: string,
): Promise<{ changed: string[] }> {
  return apiFetch(`/api/v1/spaces/${spaceId}/review/decide`, {
    method: 'POST',
    body: { document_ids: documentIds, action, note },
    credentials,
  })
}

export function listPendingSpaces(credentials: DevCredentials): Promise<PendingSpaceList> {
  return apiFetch<PendingSpaceList>('/api/v1/document-reviews/pending', { credentials })
}
