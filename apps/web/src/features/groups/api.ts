import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  AddGroupMemberInput,
  CreateGroupInput,
  EffectiveGroupMemberList,
  GroupDetail,
  GroupList,
  GroupMemberEntry,
  GroupMemberList,
  GroupMemberType,
  UpdateGroupInput,
  UserGroupList,
} from './types'

const ROOT = '/api/v1/organizations/current/groups'

export function listGroups(
  credentials: DevCredentials,
  { includeArchived = false }: { includeArchived?: boolean } = {},
): Promise<GroupList> {
  const suffix = includeArchived ? '?include_archived=true' : ''
  return apiFetch<GroupList>(`${ROOT}${suffix}`, { credentials })
}

export function createGroup(
  credentials: DevCredentials,
  input: CreateGroupInput,
): Promise<GroupDetail> {
  return apiFetch<GroupDetail>(ROOT, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function updateGroup(
  credentials: DevCredentials,
  groupId: string,
  input: UpdateGroupInput,
): Promise<GroupDetail> {
  return apiFetch<GroupDetail>(`${ROOT}/${groupId}`, {
    method: 'PATCH',
    body: input,
    credentials,
  })
}

export function archiveGroup(
  credentials: DevCredentials,
  groupId: string,
): Promise<GroupDetail> {
  return apiFetch<GroupDetail>(`${ROOT}/${groupId}:archive`, {
    method: 'POST',
    credentials,
  })
}

export function listGroupMembers(
  credentials: DevCredentials,
  groupId: string,
): Promise<GroupMemberList> {
  return apiFetch<GroupMemberList>(`${ROOT}/${groupId}/members`, { credentials })
}

export function addGroupMember(
  credentials: DevCredentials,
  groupId: string,
  input: AddGroupMemberInput,
): Promise<GroupMemberEntry> {
  return apiFetch<GroupMemberEntry>(`${ROOT}/${groupId}/members`, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function removeGroupMember(
  credentials: DevCredentials,
  groupId: string,
  memberType: GroupMemberType,
  memberId: string,
): Promise<void> {
  return apiFetch<void>(
    `${ROOT}/${groupId}/members/${memberType}/${memberId}`,
    { method: 'DELETE', credentials },
  )
}

export function listEffectiveGroupMembers(
  credentials: DevCredentials,
  groupId: string,
): Promise<EffectiveGroupMemberList> {
  return apiFetch<EffectiveGroupMemberList>(
    `${ROOT}/${groupId}/effective-members`,
    { credentials },
  )
}

export function listGroupsForUser(
  credentials: DevCredentials,
  userId: string,
  { includeArchived = false }: { includeArchived?: boolean } = {},
): Promise<UserGroupList> {
  const suffix = includeArchived ? '?include_archived=true' : ''
  return apiFetch<UserGroupList>(`${ROOT}/for-user/${userId}${suffix}`, {
    credentials,
  })
}
