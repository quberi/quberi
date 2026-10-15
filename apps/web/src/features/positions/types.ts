/**
 * Shapes for the positions API. Mirrors
 * ``apps/api/src/quberi_api/api/v1/organizations/position_schemas.py``.
 */

export type PositionStatus = 'active' | 'frozen' | 'archived'
export type PositionSyncState = 'local' | 'synced' | 'synced_augmented'

export interface PositionSummary {
  id: string
  organization_id: string
  name: string
  code: string | null
  organization_unit_id: string | null
  reports_to_position_id: string | null
  job_family: string | null
  grade: string | null
  headcount_budget: number
  status: PositionStatus
  sync_state: PositionSyncState
  created_at: string
  updated_at: string
}

export interface PositionListItem extends PositionSummary {
  current_occupants: number
}

export interface PositionDetail extends PositionListItem {
  description: string | null
  attributes: Record<string, unknown>
}

export interface PositionList {
  items: PositionListItem[]
  total: number
}

export interface CreatePositionInput {
  name: string
  code?: string | null
  organization_unit_id?: string | null
  reports_to_position_id?: string | null
  job_family?: string | null
  grade?: string | null
  headcount_budget?: number
  description?: string | null
}

export interface UpdatePositionInput {
  name?: string
  code?: string | null
  organization_unit_id?: string | null
  clear_org_unit?: boolean
  reports_to_position_id?: string | null
  clear_reports_to?: boolean
  job_family?: string | null
  grade?: string | null
  headcount_budget?: number
  description?: string | null
  status?: 'active' | 'frozen'
}

export interface PositionOccupant {
  tenure_id: string
  user_id: string
  display_name: string
  email: string
  status: string
  is_primary: boolean
  start_date: string
  end_date: string | null
}

export interface PositionOccupantList {
  items: PositionOccupant[]
  total: number
}

export interface AssignOccupantInput {
  user_id: string
  is_primary?: boolean
  start_date?: string
}
