/** Shapes for ``GET /organizations/current/overview``. */

export interface OverviewMembers {
  total: number
  active: number
  invited: number
  suspended: number
}

export interface OverviewSpaces {
  total: number
  enterprise: number
  team: number
  personal: number
  archived: number
  frozen: number
}

export interface OverviewDocuments {
  total: number
  indexed: number
  failed: number
  waiting: number
}

export interface OverviewExternal {
  connected_tools: number
  calls_7d: number
}

/** Things worth an admin's attention. Only present when ``count`` is above zero. */
export type AttentionCode =
  | 'pending_reviews'
  | 'access_requests'
  | 'index_failed'
  | 'suspended_owning_spaces'
  | 'org_wide_spaces'

export interface OverviewAttention {
  code: AttentionCode | string
  count: number
  names: string[]
}

export interface OverviewTrendPoint {
  /** YYYY-MM-DD in the viewer's time zone. */
  day: string
  documents: number
  questions: number
}

export interface OverviewCitedDocument {
  document_id: string
  name: string
  space_id: string
  space_name: string
  citations: number
  last_cited_at: string
}

export interface Overview {
  members: OverviewMembers
  spaces: OverviewSpaces
  documents: OverviewDocuments
  external: OverviewExternal
  attention: OverviewAttention[]
  trend: OverviewTrendPoint[]
  top_cited: OverviewCitedDocument[]
  cited_days: number
}
