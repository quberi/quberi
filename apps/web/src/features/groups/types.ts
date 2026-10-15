/**
 * Shapes for the groups API. Mirrors
 * ``apps/api/src/quberi_api/api/v1/organizations/group_schemas.py``.
 */

export type GroupType = 'static' | 'dynamic' | 'security' | 'project'
export type GroupStatus = 'active' | 'archived'
export type GroupSyncState = 'local' | 'synced' | 'synced_augmented'
export type GroupMemberType =
  | 'user'
  | 'organization_unit'
  | 'position'
  | 'service_account'

export interface GroupSummary {
  id: string
  organization_id: string
  type: GroupType
  name: string
  description: string | null
  status: GroupStatus
  sync_state: GroupSyncState
  created_at: string
  updated_at: string
}

export interface GroupListItem extends GroupSummary {
  member_count: number
}

export interface GroupDetail extends GroupListItem {
  attributes: Record<string, unknown>
}

export interface GroupList {
  items: GroupListItem[]
  total: number
}

export interface GroupMemberEntry {
  id: string // GroupMember row id
  member_type: GroupMemberType
  member_id: string
  display_name: string
  subtitle: string | null
  created_at: string
}

export interface GroupMemberList {
  items: GroupMemberEntry[]
  total: number
}

export interface CreateGroupInput {
  name: string
  type?: GroupType
  description?: string | null
}

export interface UpdateGroupInput {
  name?: string
  type?: GroupType
  description?: string | null
}

export interface AddGroupMemberInput {
  member_type: GroupMemberType
  member_id: string
}

// --- Effective-members contract --------------------------------------------
//
// Every distinct user this group actually contains after expanding position
// occupants and org-unit direct members. Group membership does *not* walk
// org-unit ancestor chains (per Slice B design).
export type GroupHitPathCode =
  | 'direct'
  | 'via_position'
  | 'via_organization_unit'

export type GroupHitSubjectType = 'user' | 'position' | 'organization_unit'

export interface EffectiveGroupHitPath {
  hit_path: GroupHitPathCode
  subject_type: GroupHitSubjectType
  subject_id: string
  subject_display_name: string
}

export interface EffectiveGroupMember {
  user_id: string
  display_name: string
  email: string
  hit_paths: EffectiveGroupHitPath[]
}

export interface EffectiveGroupMemberList {
  items: EffectiveGroupMember[]
  total: number
}

// --- Reverse lookup: which groups does a user belong to? -------------------
export interface UserGroupList {
  items: GroupListItem[]
  total: number
}
