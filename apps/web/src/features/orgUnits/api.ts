import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  CreateOrgUnitInput,
  OrgUnitDetail,
  OrgUnitMember,
  OrgUnitMemberList,
  OrgUnitTree,
  UpdateOrgUnitInput,
} from './types'

const ROOT = '/api/v1/organizations/current/org-units'

export function listOrgUnits(
  credentials: DevCredentials,
  { includeArchived = false }: { includeArchived?: boolean } = {},
): Promise<OrgUnitTree> {
  const suffix = includeArchived ? '?include_archived=true' : ''
  return apiFetch<OrgUnitTree>(`${ROOT}${suffix}`, { credentials })
}

export function createOrgUnit(
  credentials: DevCredentials,
  input: CreateOrgUnitInput,
): Promise<OrgUnitDetail> {
  return apiFetch<OrgUnitDetail>(ROOT, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function updateOrgUnit(
  credentials: DevCredentials,
  unitId: string,
  input: UpdateOrgUnitInput,
): Promise<OrgUnitDetail> {
  return apiFetch<OrgUnitDetail>(`${ROOT}/${unitId}`, {
    method: 'PATCH',
    body: input,
    credentials,
  })
}

export function moveOrgUnit(
  credentials: DevCredentials,
  unitId: string,
  newParentId: string | null,
): Promise<OrgUnitDetail> {
  return apiFetch<OrgUnitDetail>(`${ROOT}/${unitId}:move`, {
    method: 'POST',
    body: { new_parent_id: newParentId },
    credentials,
  })
}

export function archiveOrgUnit(
  credentials: DevCredentials,
  unitId: string,
): Promise<OrgUnitDetail> {
  return apiFetch<OrgUnitDetail>(`${ROOT}/${unitId}:archive`, {
    method: 'POST',
    credentials,
  })
}

export function listOrgUnitMembers(
  credentials: DevCredentials,
  unitId: string,
): Promise<OrgUnitMemberList> {
  return apiFetch<OrgUnitMemberList>(`${ROOT}/${unitId}/members`, {
    credentials,
  })
}

export function assignUserToOrgUnit(
  credentials: DevCredentials,
  unitId: string,
  userId: string,
  makePrimary = false,
): Promise<OrgUnitMember> {
  return apiFetch<OrgUnitMember>(`${ROOT}/${unitId}/members`, {
    method: 'POST',
    body: { user_id: userId, make_primary: makePrimary },
    credentials,
  })
}

export function removeUserFromOrgUnit(
  credentials: DevCredentials,
  unitId: string,
  userId: string,
): Promise<void> {
  return apiFetch<void>(`${ROOT}/${unitId}/members/${userId}`, {
    method: 'DELETE',
    credentials,
  })
}

export function setPrimaryOrgUnit(
  credentials: DevCredentials,
  unitId: string,
  userId: string,
): Promise<OrgUnitMember> {
  return apiFetch<OrgUnitMember>(
    `${ROOT}/${unitId}/members/${userId}:set-primary`,
    {
      method: 'POST',
      credentials,
    },
  )
}
