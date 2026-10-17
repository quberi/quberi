import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type { CreateSpaceInput, SpaceDetail, SpaceList } from './types'

export function listSpaces(credentials: DevCredentials): Promise<SpaceList> {
  return apiFetch<SpaceList>('/api/v1/spaces', { credentials })
}

/** Every enterprise-owned space (org owners/admins only; 403 otherwise). */
export function listOrganizationSpaces(
  credentials: DevCredentials,
): Promise<SpaceList> {
  return apiFetch<SpaceList>('/api/v1/spaces/organization', { credentials })
}

export function archiveSpace(
  credentials: DevCredentials,
  spaceId: string,
): Promise<SpaceDetail> {
  return apiFetch<SpaceDetail>(`/api/v1/spaces/${spaceId}:archive`, {
    method: 'POST',
    credentials,
  })
}

export function createSpace(
  credentials: DevCredentials,
  input: CreateSpaceInput,
): Promise<SpaceDetail> {
  return apiFetch<SpaceDetail>('/api/v1/spaces', {
    method: 'POST',
    body: input,
    credentials,
  })
}

export interface TransferOwnerResult {
  space_id: string
  new_owner_user_id: string
  /** "handover" (an owner passes it on) or "replacement" (the organisation owner names one). */
  mode: string
}

/** Hand a space to a new owner, or (organisation owner only) name one when none can act. */
export function transferSpaceOwner(
  credentials: DevCredentials,
  spaceId: string,
  newOwnerUserId: string,
): Promise<TransferOwnerResult> {
  return apiFetch<TransferOwnerResult>(`/api/v1/spaces/${spaceId}/owner`, {
    method: 'POST',
    body: { new_owner_user_id: newOwnerUserId },
    credentials,
  })
}
