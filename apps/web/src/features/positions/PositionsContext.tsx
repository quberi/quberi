import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import {
  archivePosition as apiArchive,
  assignOccupant as apiAssign,
  createPosition as apiCreate,
  endTenure as apiEnd,
  listOccupants as apiListOccupants,
  listPositions as apiList,
  setPrimaryPosition as apiSetPrimary,
  updatePosition as apiUpdate,
} from './api'
import type {
  AssignOccupantInput,
  CreatePositionInput,
  PositionDetail,
  PositionListItem,
  PositionOccupant,
  UpdatePositionInput,
} from './types'

type LoadState<T> =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: T }

interface PositionsContextValue {
  positions: LoadState<PositionListItem[]>
  includeArchived: boolean
  setIncludeArchived: (value: boolean) => void
  selectedId: string | null
  selectPosition: (id: string | null) => void
  reload: () => Promise<void>
  createPosition: (input: CreatePositionInput) => Promise<PositionDetail>
  updatePosition: (id: string, input: UpdatePositionInput) => Promise<PositionDetail>
  archivePosition: (id: string) => Promise<PositionDetail>

  occupants: LoadState<PositionOccupant[]>
  reloadOccupants: (positionId?: string) => Promise<void>
  assignOccupant: (
    positionId: string,
    input: AssignOccupantInput,
  ) => Promise<PositionOccupant>
  endTenure: (positionId: string, userId: string) => Promise<void>
  setPrimary: (positionId: string, userId: string) => Promise<PositionOccupant>
}

const PositionsContext = createContext<PositionsContextValue | null>(null)

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

export function PositionsProvider({ children }: PropsWithChildren) {
  const { credentials } = useAuth()
  const [positions, setPositions] = useState<LoadState<PositionListItem[]>>({
    kind: 'idle',
  })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [occupants, setOccupants] = useState<LoadState<PositionOccupant[]>>({
    kind: 'idle',
  })

  const reload = useCallback(async () => {
    if (!credentials) {
      setPositions({ kind: 'idle' })
      return
    }
    setPositions({ kind: 'loading' })
    try {
      const { items } = await apiList(credentials, { includeArchived })
      setPositions({ kind: 'ready', items })
      setSelectedId((prev) =>
        prev && items.some((p) => p.id === prev) ? prev : null,
      )
    } catch (err) {
      setPositions({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials, includeArchived])

  const reloadOccupants = useCallback(
    async (explicitId?: string) => {
      const positionId = explicitId ?? selectedId
      if (!credentials || !positionId) {
        setOccupants({ kind: 'idle' })
        return
      }
      setOccupants({ kind: 'loading' })
      try {
        const { items } = await apiListOccupants(credentials, positionId)
        setOccupants({ kind: 'ready', items })
      } catch (err) {
        setOccupants({ kind: 'error', message: toMessage(err) })
      }
    },
    [credentials, selectedId],
  )

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadOccupants()
  }, [reloadOccupants, selectedId])

  const selectPosition = useCallback((id: string | null) => {
    setSelectedId(id)
  }, [])

  const createPosition = useCallback(
    async (input: CreatePositionInput): Promise<PositionDetail> => {
      if (!credentials) throw new Error('not signed in')
      const created = await apiCreate(credentials, input)
      await reload()
      setSelectedId(created.id)
      return created
    },
    [credentials, reload],
  )

  const updatePosition = useCallback(
    async (id: string, input: UpdatePositionInput): Promise<PositionDetail> => {
      if (!credentials) throw new Error('not signed in')
      const updated = await apiUpdate(credentials, id, input)
      await reload()
      return updated
    },
    [credentials, reload],
  )

  const archivePosition = useCallback(
    async (id: string): Promise<PositionDetail> => {
      if (!credentials) throw new Error('not signed in')
      const archived = await apiArchive(credentials, id)
      await reload()
      return archived
    },
    [credentials, reload],
  )

  const assignOccupant = useCallback(
    async (positionId: string, input: AssignOccupantInput) => {
      if (!credentials) throw new Error('not signed in')
      const result = await apiAssign(credentials, positionId, input)
      await reloadOccupants(positionId)
      await reload()
      return result
    },
    [credentials, reload, reloadOccupants],
  )

  const endTenure = useCallback(
    async (positionId: string, userId: string) => {
      if (!credentials) throw new Error('not signed in')
      await apiEnd(credentials, positionId, userId)
      await reloadOccupants(positionId)
      await reload()
    },
    [credentials, reload, reloadOccupants],
  )

  const setPrimary = useCallback(
    async (positionId: string, userId: string) => {
      if (!credentials) throw new Error('not signed in')
      const result = await apiSetPrimary(credentials, positionId, userId)
      await reloadOccupants(positionId)
      return result
    },
    [credentials, reloadOccupants],
  )

  const value = useMemo<PositionsContextValue>(
    () => ({
      positions,
      includeArchived,
      setIncludeArchived,
      selectedId,
      selectPosition,
      reload,
      createPosition,
      updatePosition,
      archivePosition,
      occupants,
      reloadOccupants,
      assignOccupant,
      endTenure,
      setPrimary,
    }),
    [
      positions,
      includeArchived,
      selectedId,
      selectPosition,
      reload,
      createPosition,
      updatePosition,
      archivePosition,
      occupants,
      reloadOccupants,
      assignOccupant,
      endTenure,
      setPrimary,
    ],
  )

  return <PositionsContext.Provider value={value}>{children}</PositionsContext.Provider>
}

export function usePositions(): PositionsContextValue {
  const value = useContext(PositionsContext)
  if (value === null) {
    throw new Error('usePositions must be used within a PositionsProvider')
  }
  return value
}
