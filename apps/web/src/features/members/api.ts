import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type { InvitableRole, InviteMemberInput, MemberList, MemberSummary } from './types'

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
