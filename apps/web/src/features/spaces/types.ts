import type { AddAccessBindingInput } from '../spaceAccess/types'

export interface SpaceSummary {
  id: string
  name: string
  slug: string
  preset: 'personal' | 'collaborative' | 'managed'
  visibility: 'private' | 'organization' | 'public_link'
  trust_level: 'trusted' | 'guided' | 'exploratory'
  status: 'active' | 'archived'
  owner_type: 'user' | 'group' | 'organization_unit' | 'organization'
  owner_id: string
  is_owner: boolean
  /** Only on the organisation's space list: no owner of the space can act any more. */
  owner_unavailable?: boolean
  /** Only on the organisation's space list: can the caller read it / their pending request. */
  caller_can_read?: boolean | null
  caller_pending_request_id?: string | null
  /** More than 0 when only that many documents of this space are visible to the caller,
   * each shared with them individually. They cannot read the space as a whole. */
  shared_only_documents?: number
  /** Set only on the bilingual sample spaces; ordinary spaces have none. */
  locale?: string | null
  updated_at: string
}

export interface SpaceDetail extends SpaceSummary {
  organization_id: string
  description: string | null
  parent_space_id: string | null
  inheritance_policy: Record<string, unknown>
  attributes: Record<string, unknown>
  created_at: string
}

export interface SpaceList {
  items: SpaceSummary[]
  total: number
}

export interface CreateSpaceInput {
  name: string
  slug: string
  description?: string
  visibility?: SpaceSummary['visibility']
  trust_level?: SpaceSummary['trust_level']
  /** ``organization`` = an enterprise-owned space (org owners/admins only). */
  owner?: 'user' | 'organization'
  /** Initial access, applied in the same transaction as the space. */
  grants?: AddAccessBindingInput[]
}

/** A space the person can read as a whole (not one reached only through shared documents). */
export function isWholeSpace(space: SpaceSummary): boolean {
  return (space.shared_only_documents ?? 0) === 0
}
