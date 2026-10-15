/**
 * Shapes for the organisation-unit endpoints.
 * Keep in lock-step with ``apps/api/src/quberi_api/api/v1/organizations/org_unit_schemas.py``.
 */

export type OrgUnitType =
  | 'department'
  | 'team'
  | 'business_unit'
  | 'legal_entity'
  | 'cost_center'
  | 'custom'

export type OrgUnitStatus = 'active' | 'archived'
export type SyncState = 'local' | 'synced' | 'synced_augmented'

export interface OrgUnitSummary {
  id: string
  organization_id: string
  parent_id: string | null
  type: OrgUnitType
  name: string
  code: string | null
  manager_user_id: string | null
  status: OrgUnitStatus
  sync_state: SyncState
  created_at: string
  updated_at: string
}

export interface OrgUnitTreeItem extends OrgUnitSummary {
  ancestor_ids: string[]
}

export interface OrgUnitDetail extends OrgUnitTreeItem {
  attributes: Record<string, unknown>
}

export interface OrgUnitTree {
  items: OrgUnitTreeItem[]
  total: number
}

export interface OrgUnitMember {
  membership_id: string
  user_id: string
  display_name: string
  email: string
  status: string
  is_primary: boolean
}

export interface OrgUnitMemberList {
  items: OrgUnitMember[]
  total: number
}

export interface CreateOrgUnitInput {
  name: string
  type?: OrgUnitType
  parent_id?: string | null
  code?: string | null
  manager_user_id?: string | null
  attributes?: Record<string, unknown>
}

export interface UpdateOrgUnitInput {
  name?: string
  type?: OrgUnitType
  code?: string | null
  manager_user_id?: string | null
  clear_manager?: boolean
  attributes?: Record<string, unknown>
}

/** Client-side tree node built by ``buildTree``. */
export interface OrgUnitNode extends OrgUnitTreeItem {
  children: OrgUnitNode[]
  depth: number
}
