import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import {
  addGroupMember as apiAddMember,
  archiveGroup as apiArchive,
  createGroup as apiCreate,
  listEffectiveGroupMembers as apiListEffective,
  listGroupMembers as apiListMembers,
  listGroups as apiList,
  removeGroupMember as apiRemoveMember,
  updateGroup as apiUpdate,
} from './api'
import type {
  AddGroupMemberInput,
  CreateGroupInput,
  EffectiveGroupMember,
  GroupDetail,
  GroupListItem,
  GroupMemberEntry,
  GroupMemberType,
  UpdateGroupInput,
} from './types'

type LoadState<T> =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: T }

interface GroupsContextValue {
  groups: LoadState<GroupListItem[]>
  includeArchived: boolean
  setIncludeArchived: (value: boolean) => void
  selectedId: string | null
  selectGroup: (id: string | null) => void
  reload: () => Promise<void>
  createGroup: (input: CreateGroupInput) => Promise<GroupDetail>
  updateGroup: (id: string, input: UpdateGroupInput) => Promise<GroupDetail>
  archiveGroup: (id: string) => Promise<GroupDetail>

  members: LoadState<GroupMemberEntry[]>
  reloadMembers: (groupId?: string) => Promise<void>
  addMember: (
    groupId: string,
    input: AddGroupMemberInput,
  ) => Promise<GroupMemberEntry>
  removeMember: (
    groupId: string,
    memberType: GroupMemberType,
    memberId: string,
  ) => Promise<void>

  // Effective view — the same group after expanding positions + org units
  // to their underlying users. Reloaded independently so the direct-members
  // list stays fast to render.
  effectiveMembers: LoadState<EffectiveGroupMember[]>
  reloadEffectiveMembers: (groupId?: string) => Promise<void>
}

const GroupsContext = createContext<GroupsContextValue | null>(null)

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

export function GroupsProvider({ children }: PropsWithChildren) {
  const { credentials } = useAuth()
  const [groups, setGroups] = useState<LoadState<GroupListItem[]>>({ kind: 'idle' })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [members, setMembers] = useState<LoadState<GroupMemberEntry[]>>({
    kind: 'idle',
  })
  const [effectiveMembers, setEffectiveMembers] = useState<
    LoadState<EffectiveGroupMember[]>
  >({ kind: 'idle' })

  const reload = useCallback(async () => {
    if (!credentials) {
      setGroups({ kind: 'idle' })
      return
    }
    setGroups({ kind: 'loading' })
    try {
      const { items } = await apiList(credentials, { includeArchived })
      setGroups({ kind: 'ready', items })
      setSelectedId((prev) =>
        prev && items.some((g) => g.id === prev) ? prev : null,
      )
    } catch (err) {
      setGroups({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials, includeArchived])

  const reloadMembers = useCallback(
    async (explicitId?: string) => {
      const groupId = explicitId ?? selectedId
      if (!credentials || !groupId) {
        setMembers({ kind: 'idle' })
        return
      }
      setMembers({ kind: 'loading' })
      try {
        const { items } = await apiListMembers(credentials, groupId)
        setMembers({ kind: 'ready', items })
      } catch (err) {
        setMembers({ kind: 'error', message: toMessage(err) })
      }
    },
    [credentials, selectedId],
  )

  const reloadEffectiveMembers = useCallback(
    async (explicitId?: string) => {
      const groupId = explicitId ?? selectedId
      if (!credentials || !groupId) {
        setEffectiveMembers({ kind: 'idle' })
        return
      }
      setEffectiveMembers({ kind: 'loading' })
      try {
        const { items } = await apiListEffective(credentials, groupId)
        setEffectiveMembers({ kind: 'ready', items })
      } catch (err) {
        setEffectiveMembers({ kind: 'error', message: toMessage(err) })
      }
    },
    [credentials, selectedId],
  )

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    // Whenever the selection changes, refresh both the direct-member list
    // and the effective view. They fire in parallel — the effective query
    // is heavier so it may finish second, which is fine for the UI.
    void reloadMembers()
    void reloadEffectiveMembers()
  }, [reloadMembers, reloadEffectiveMembers, selectedId])

  const selectGroup = useCallback((id: string | null) => setSelectedId(id), [])

  const createGroup = useCallback(
    async (input: CreateGroupInput): Promise<GroupDetail> => {
      if (!credentials) throw new Error('not signed in')
      const created = await apiCreate(credentials, input)
      await reload()
      setSelectedId(created.id)
      return created
    },
    [credentials, reload],
  )

  const updateGroup = useCallback(
    async (id: string, input: UpdateGroupInput): Promise<GroupDetail> => {
      if (!credentials) throw new Error('not signed in')
      const updated = await apiUpdate(credentials, id, input)
      await reload()
      return updated
    },
    [credentials, reload],
  )

  const archiveGroup = useCallback(
    async (id: string): Promise<GroupDetail> => {
      if (!credentials) throw new Error('not signed in')
      const archived = await apiArchive(credentials, id)
      await reload()
      return archived
    },
    [credentials, reload],
  )

  const addMember = useCallback(
    async (groupId: string, input: AddGroupMemberInput) => {
      if (!credentials) throw new Error('not signed in')
      const result = await apiAddMember(credentials, groupId, input)
      await Promise.all([
        reloadMembers(groupId),
        reloadEffectiveMembers(groupId),
        reload(),
      ])
      return result
    },
    [credentials, reload, reloadMembers, reloadEffectiveMembers],
  )

  const removeMember = useCallback(
    async (groupId: string, memberType: GroupMemberType, memberId: string) => {
      if (!credentials) throw new Error('not signed in')
      await apiRemoveMember(credentials, groupId, memberType, memberId)
      await Promise.all([
        reloadMembers(groupId),
        reloadEffectiveMembers(groupId),
        reload(),
      ])
    },
    [credentials, reload, reloadMembers, reloadEffectiveMembers],
  )

  const value = useMemo<GroupsContextValue>(
    () => ({
      groups,
      includeArchived,
      setIncludeArchived,
      selectedId,
      selectGroup,
      reload,
      createGroup,
      updateGroup,
      archiveGroup,
      members,
      reloadMembers,
      addMember,
      removeMember,
      effectiveMembers,
      reloadEffectiveMembers,
    }),
    [
      groups,
      includeArchived,
      selectedId,
      selectGroup,
      reload,
      createGroup,
      updateGroup,
      archiveGroup,
      members,
      reloadMembers,
      addMember,
      removeMember,
      effectiveMembers,
      reloadEffectiveMembers,
    ],
  )

  return <GroupsContext.Provider value={value}>{children}</GroupsContext.Provider>
}

export function useGroups(): GroupsContextValue {
  const value = useContext(GroupsContext)
  if (value === null) {
    throw new Error('useGroups must be used within a GroupsProvider')
  }
  return value
}
