import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  AssignOccupantInput,
  CreatePositionInput,
  PositionDetail,
  PositionList,
  PositionOccupant,
  PositionOccupantList,
  UpdatePositionInput,
} from './types'

const ROOT = '/api/v1/organizations/current/positions'

export function listPositions(
  credentials: DevCredentials,
  { includeArchived = false }: { includeArchived?: boolean } = {},
): Promise<PositionList> {
  const suffix = includeArchived ? '?include_archived=true' : ''
  return apiFetch<PositionList>(`${ROOT}${suffix}`, { credentials })
}

export function createPosition(
  credentials: DevCredentials,
  input: CreatePositionInput,
): Promise<PositionDetail> {
  return apiFetch<PositionDetail>(ROOT, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function updatePosition(
  credentials: DevCredentials,
  positionId: string,
  input: UpdatePositionInput,
): Promise<PositionDetail> {
  return apiFetch<PositionDetail>(`${ROOT}/${positionId}`, {
    method: 'PATCH',
    body: input,
    credentials,
  })
}

export function archivePosition(
  credentials: DevCredentials,
  positionId: string,
): Promise<PositionDetail> {
  return apiFetch<PositionDetail>(`${ROOT}/${positionId}:archive`, {
    method: 'POST',
    credentials,
  })
}

export function listOccupants(
  credentials: DevCredentials,
  positionId: string,
  { includeEnded = false }: { includeEnded?: boolean } = {},
): Promise<PositionOccupantList> {
  const suffix = includeEnded ? '?include_ended=true' : ''
  return apiFetch<PositionOccupantList>(
    `${ROOT}/${positionId}/occupants${suffix}`,
    { credentials },
  )
}

export function assignOccupant(
  credentials: DevCredentials,
  positionId: string,
  input: AssignOccupantInput,
): Promise<PositionOccupant> {
  return apiFetch<PositionOccupant>(`${ROOT}/${positionId}/occupants`, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function endTenure(
  credentials: DevCredentials,
  positionId: string,
  userId: string,
): Promise<void> {
  return apiFetch<void>(`${ROOT}/${positionId}/occupants/${userId}`, {
    method: 'DELETE',
    credentials,
  })
}

export function setPrimaryPosition(
  credentials: DevCredentials,
  positionId: string,
  userId: string,
): Promise<PositionOccupant> {
  return apiFetch<PositionOccupant>(
    `${ROOT}/${positionId}/occupants/${userId}:set-primary`,
    { method: 'POST', credentials },
  )
}
