/**
 * Shapes for the space access-bindings API.
 */

export type AccessSubjectType =
  | 'user'
  | 'group'
  | 'organization_unit'
  | 'position'
  /** Everyone in the organisation; the server fills in the org id. */
  | 'organization'
export type SpaceRoleCode = 'owner' | 'admin' | 'editor' | 'viewer'

export interface AccessBinding {
  id: string
  subject_type: AccessSubjectType
  subject_id: string
  subject_display_name: string
  subject_subtitle: string | null
  role_code: SpaceRoleCode
  created_at: string
}

export interface AccessBindingList {
  items: AccessBinding[]
  total: number
}

export interface AddAccessBindingInput {
  subject_type: AccessSubjectType
  /** Omit for ``subject_type: 'organization'`` — the server uses the caller's org. */
  subject_id?: string
  role: SpaceRoleCode
}

// --- Effective-members contract -------------------------------------------
//
// The set of ``hit_path`` values here mirrors ``HitPath`` in
// ``services/authz.py`` on the backend. Adding a new path server-side
// requires extending this union; TypeScript will otherwise reject the
// response at the ``useEffectiveSpaceMembers`` boundary.
export type HitPathCode =
  | 'direct'
  | 'position'
  | 'organization_unit'
  | 'organization_unit_ancestor'
  | 'group_via_user'
  | 'group_via_position'
  | 'group_via_organization_unit'
  | 'organization'

export type HitSubjectType =
  | 'user'
  | 'position'
  | 'organization_unit'
  | 'group'
  | 'organization'

export interface EffectiveHitPath {
  hit_path: HitPathCode
  role_code: SpaceRoleCode
  subject_type: HitSubjectType
  subject_id: string
  subject_display_name: string
}

export interface EffectiveMember {
  user_id: string
  display_name: string
  email: string
  effective_role: SpaceRoleCode
  hit_paths: EffectiveHitPath[]
}

export interface EffectiveMemberList {
  items: EffectiveMember[]
  total: number
}
