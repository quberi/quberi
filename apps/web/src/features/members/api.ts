import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  DeactivationResponse,
  InvitableRole,
  InviteMemberInput,
  MemberList,
  MemberSummary,
  OffboardingChecklist,
  ReactivationResponse,
  TransferSpacesResponse,
} from './types'

const MEMBERS_PATH = '/api/v1/organizations/current/members'

export function listMembers(credentials: DevCredentials): Promise<MemberList> {
  return apiFetch<MemberList>(MEMBERS_PATH, { credentials })
}

export function inviteMember(
  credentials: DevCredentials,
  input: InviteMemberInput,
): Promise<MemberSummary> {
  return apiFetch<MemberSummary>(MEMBERS_PATH, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function setMemberRole(
  credentials: DevCredentials,
  userId: string,
  role: InvitableRole,
): Promise<MemberSummary> {
  return apiFetch<MemberSummary>(`${MEMBERS_PATH}/${userId}`, {
    method: 'PATCH',
    body: { role },
    credentials,
  })
}

export function deactivateMember(
  credentials: DevCredentials,
  userId: string,
): Promise<DeactivationResponse> {
  return apiFetch<DeactivationResponse>(`${MEMBERS_PATH}/${userId}/deactivate`, {
    method: 'POST',
    credentials,
    timeoutMs: 30_000,
  })
}

export function reactivateMember(
  credentials: DevCredentials,
  userId: string,
): Promise<ReactivationResponse> {
  return apiFetch<ReactivationResponse>(`${MEMBERS_PATH}/${userId}/reactivate`, {
    method: 'POST',
    credentials,
  })
}

export function getOffboardingChecklist(
  credentials: DevCredentials,
  userId: string,
): Promise<OffboardingChecklist> {
  return apiFetch<OffboardingChecklist>(`${MEMBERS_PATH}/${userId}/offboarding`, {
    credentials,
    timeoutMs: 30_000,
  })
}

export function transferSpaces(
  credentials: DevCredentials,
  userId: string,
  newOwnerUserId: string,
  spaceIds: string[],
): Promise<TransferSpacesResponse> {
  return apiFetch<TransferSpacesResponse>(`${MEMBERS_PATH}/${userId}/offboarding/transfer-spaces`, {
    method: 'POST',
    body: { new_owner_user_id: newOwnerUserId, space_ids: spaceIds },
    credentials,
    timeoutMs: 60_000,
  })
}
