import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  AccessBinding,
  AccessBindingList,
  AddAccessBindingInput,
  EffectiveMemberList,
} from './types'

export function listAccessBindings(
  credentials: DevCredentials,
  spaceId: string,
): Promise<AccessBindingList> {
  return apiFetch<AccessBindingList>(
    `/api/v1/spaces/${spaceId}/access-bindings`,
    { credentials },
  )
}

export function addAccessBinding(
  credentials: DevCredentials,
  spaceId: string,
  input: AddAccessBindingInput,
): Promise<AccessBinding> {
  return apiFetch<AccessBinding>(`/api/v1/spaces/${spaceId}/access-bindings`, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function removeAccessBinding(
  credentials: DevCredentials,
  spaceId: string,
  bindingId: string,
): Promise<void> {
  return apiFetch<void>(
    `/api/v1/spaces/${spaceId}/access-bindings/${bindingId}`,
    { method: 'DELETE', credentials },
  )
}

export function listEffectiveMembers(
  credentials: DevCredentials,
  spaceId: string,
): Promise<EffectiveMemberList> {
  return apiFetch<EffectiveMemberList>(
    `/api/v1/spaces/${spaceId}/effective-members`,
    { credentials },
  )
}
