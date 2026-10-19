// ``InvitableRole`` matches the backend's ``Literal['admin', 'member']`` on
// the invite endpoint. Editor / viewer are *space-level* roles and are
// granted separately on individual spaces, so they never appear here.
export type InvitableRole = 'admin' | 'member'

// ``MemberRole`` covers everything the list endpoint can return, including
// legacy or space-level roles the user might carry via existing bindings.
export type MemberRole = InvitableRole | 'owner' | 'editor' | 'viewer' | ''
export type MemberStatus = 'invited' | 'active' | 'suspended' | 'archived'

export interface DirectManagerRef {
  user_id: string
  display_name: string
}

export interface MemberSummary {
  id: string
  display_name: string
  email: string
  status: MemberStatus
  role_code: MemberRole
  is_pending: boolean
  created_at: string
  updated_at: string
  primary_org_unit_id?: string | null
  primary_org_unit_name?: string | null
  primary_position_id?: string | null
  primary_position_name?: string | null
  direct_managers?: DirectManagerRef[]
}

export interface MemberList {
  items: MemberSummary[]
  total: number
}

export interface InviteMemberInput {
  email: string
  display_name: string
  role: InvitableRole
}

export interface DeactivationEffects {
  oauth_grants_revoked: number
  feishu_tokens_removed: number
  feishu_sources_paused: number
  private_model_bindings_removed: number
  org_model_selections_removed: number
  other_org_selections_invalidated: number
  personal_spaces_frozen: number
}

export interface DeactivationResponse {
  member: MemberSummary
  changed: boolean
  effects: DeactivationEffects
}

export interface ReactivationResponse {
  member: MemberSummary
  changed: boolean
  personal_spaces_unfrozen: number
}

export interface OwnedSpaceItem {
  space_id: string
  name: string
  owner_type: string
  other_owners: { user_id: string; display_name: string }[]
  needs_transfer: boolean
  pending_review_count: number
}

export interface ReviewerSpaceItem {
  space_id: string
  name: string
  pending_review_count: number
  other_reviewers: number
}

export interface PendingUploadItem {
  document_id: string
  name: string
  size_bytes: number
  created_at: string
  space_id: string
  space_name: string
  reviewer_count: number
}

export interface OffboardingImpact {
  oauth_grants: number
  feishu_tokens: number
  feishu_sources: number
  private_model_bindings: number
  personal_spaces: number
  org_selections_referencing_private_keys: ('chat' | 'embedding')[]
}

export interface OffboardingChecklist {
  member: { id: string; display_name: string; email: string; status: MemberStatus; role_code: string }
  personal_space_status: string | null
  owned_spaces: OwnedSpaceItem[]
  owned_spaces_total: number
  needs_transfer_total: number
  reviewer_spaces: ReviewerSpaceItem[]
  reviewer_spaces_total: number
  uploads_as_uploader: PendingUploadItem[]
  uploads_as_uploader_total: number
  impact: OffboardingImpact
}

export interface TransferItemResult {
  space_id: string
  status: 'transferred' | 'already_done' | 'skipped' | 'failed'
  code: string | null
  pending_review_count: number
}

export interface TransferSpacesResponse {
  new_owner_user_id: string
  results: TransferItemResult[]
  transferred: number
  already_done: number
  skipped: number
  failed: number
}
