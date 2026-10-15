import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import {
  archiveOrgUnit as apiArchive,
  assignUserToOrgUnit as apiAssign,
  createOrgUnit as apiCreate,
  listOrgUnitMembers as apiListMembers,
  listOrgUnits as apiList,
  moveOrgUnit as apiMove,
  removeUserFromOrgUnit as apiRemove,
  setPrimaryOrgUnit as apiSetPrimary,
  updateOrgUnit as apiUpdate,
} from './api'
import { buildTree } from './tree'
import type {
  CreateOrgUnitInput,
  OrgUnitDetail,
  OrgUnitMember,
  OrgUnitNode,
  OrgUnitTreeItem,
  UpdateOrgUnitInput,
} from './types'

/**
 * Central store for the organisation tree.
 *
 * Responsibilities
 * ----------------
 * * Fetch the flat list from the API and expose both flat + tree views.
 * * Track the currently-selected unit and expose focused mutation helpers
 *   that also refresh the affected slice of the data.
 * * Cache the last-loaded member list per unit; refresh on demand.
 */

type LoadState<T> =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: T }

interface OrgUnitsContextValue {
  units: LoadState<OrgUnitTreeItem[]>
  tree: OrgUnitNode[]
  includeArchived: boolean
  setIncludeArchived: (value: boolean) => void
  selectedId: string | null
  selectUnit: (id: string | null) => void
  reload: () => Promise<void>
  createUnit: (input: CreateOrgUnitInput) => Promise<OrgUnitDetail>
  updateUnit: (id: string, input: UpdateOrgUnitInput) => Promise<OrgUnitDetail>
  moveUnit: (id: string, newParentId: string | null) => Promise<OrgUnitDetail>
  archiveUnit: (id: string) => Promise<OrgUnitDetail>

  members: LoadState<OrgUnitMember[]>
  reloadMembers: (unitId?: string) => Promise<void>
  addMember: (unitId: string, userId: string, makePrimary?: boolean) => Promise<OrgUnitMember>
  removeMember: (unitId: string, userId: string) => Promise<void>
  setMemberPrimary: (unitId: string, userId: string) => Promise<OrgUnitMember>
}

const OrgUnitsContext = createContext<OrgUnitsContextValue | null>(null)

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

export function OrgUnitsProvider({ children }: PropsWithChildren) {
  const { credentials } = useAuth()
  const [units, setUnits] = useState<LoadState<OrgUnitTreeItem[]>>({ kind: 'idle' })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [members, setMembers] = useState<LoadState<OrgUnitMember[]>>({ kind: 'idle' })

  const reload = useCallback(async () => {
    if (!credentials) {
      setUnits({ kind: 'idle' })
      return
    }
    setUnits({ kind: 'loading' })
    try {
      const { items } = await apiList(credentials, { includeArchived })
      setUnits({ kind: 'ready', items })
      // Keep selection valid; drop it if the row disappeared.
      setSelectedId((prev) => (prev && items.some((u) => u.id === prev) ? prev : null))
    } catch (err) {
      setUnits({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials, includeArchived])

  const reloadMembers = useCallback(
    async (explicitId?: string) => {
      const unitId = explicitId ?? selectedId
      if (!credentials || !unitId) {
        setMembers({ kind: 'idle' })
        return
      }
      setMembers({ kind: 'loading' })
      try {
        const { items } = await apiListMembers(credentials, unitId)
        setMembers({ kind: 'ready', items })
      } catch (err) {
        setMembers({ kind: 'error', message: toMessage(err) })
      }
    },
    [credentials, selectedId],
  )

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    // Whenever the selected unit changes, refresh its members.
    void reloadMembers()
  }, [reloadMembers, selectedId])

  const selectUnit = useCallback((id: string | null) => {
    setSelectedId(id)
  }, [])

  const createUnit = useCallback(
    async (input: CreateOrgUnitInput): Promise<OrgUnitDetail> => {
      if (!credentials) throw new Error('not signed in')
      const created = await apiCreate(credentials, input)
      await reload()
      setSelectedId(created.id)
      return created
    },
    [credentials, reload],
  )

  const updateUnit = useCallback(
    async (id: string, input: UpdateOrgUnitInput): Promise<OrgUnitDetail> => {
      if (!credentials) throw new Error('not signed in')
      const updated = await apiUpdate(credentials, id, input)
      await reload()
      return updated
    },
    [credentials, reload],
  )

  const moveUnit = useCallback(
    async (id: string, newParentId: string | null): Promise<OrgUnitDetail> => {
      if (!credentials) throw new Error('not signed in')
      const moved = await apiMove(credentials, id, newParentId)
      await reload()
      return moved
    },
    [credentials, reload],
  )

  const archiveUnit = useCallback(
    async (id: string): Promise<OrgUnitDetail> => {
      if (!credentials) throw new Error('not signed in')
      const archived = await apiArchive(credentials, id)
      await reload()
      return archived
    },
    [credentials, reload],
  )

  const addMember = useCallback(
    async (unitId: string, userId: string, makePrimary = false) => {
      if (!credentials) throw new Error('not signed in')
      const result = await apiAssign(credentials, unitId, userId, makePrimary)
      await reloadMembers(unitId)
      return result
    },
    [credentials, reloadMembers],
  )

  const removeMember = useCallback(
    async (unitId: string, userId: string) => {
      if (!credentials) throw new Error('not signed in')
      await apiRemove(credentials, unitId, userId)
      await reloadMembers(unitId)
    },
    [credentials, reloadMembers],
  )

  const setMemberPrimary = useCallback(
    async (unitId: string, userId: string) => {
      if (!credentials) throw new Error('not signed in')
      const result = await apiSetPrimary(credentials, unitId, userId)
      await reloadMembers(unitId)
      return result
    },
    [credentials, reloadMembers],
  )

  const tree = useMemo<OrgUnitNode[]>(() => {
    return units.kind === 'ready' ? buildTree(units.items) : []
  }, [units])

  const value = useMemo<OrgUnitsContextValue>(
    () => ({
      units,
      tree,
      includeArchived,
      setIncludeArchived,
      selectedId,
      selectUnit,
      reload,
      createUnit,
      updateUnit,
      moveUnit,
      archiveUnit,
      members,
      reloadMembers,
      addMember,
      removeMember,
      setMemberPrimary,
    }),
    [
      units,
      tree,
      includeArchived,
      selectedId,
      selectUnit,
      reload,
      createUnit,
      updateUnit,
      moveUnit,
      archiveUnit,
      members,
      reloadMembers,
      addMember,
      removeMember,
      setMemberPrimary,
    ],
  )

  return <OrgUnitsContext.Provider value={value}>{children}</OrgUnitsContext.Provider>
}

export function useOrgUnits(): OrgUnitsContextValue {
  const value = useContext(OrgUnitsContext)
  if (value === null) {
    throw new Error('useOrgUnits must be used within an OrgUnitsProvider')
  }
  return value
}
