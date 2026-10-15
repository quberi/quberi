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
