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
