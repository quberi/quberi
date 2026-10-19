import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import { deactivateMember, inviteMember, listMembers, reactivateMember, setMemberRole } from './api'
import type {
  DeactivationResponse,
  InvitableRole,
  InviteMemberInput,
  MemberSummary,
  ReactivationResponse,
} from './types'

type LoadState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: MemberSummary[] }

interface MembersContextValue {
  state: LoadState
  reload: () => Promise<void>
  invite: (input: InviteMemberInput) => Promise<MemberSummary>
  setRole: (userId: string, role: InvitableRole) => Promise<MemberSummary>
  /** Organisation owner only. Suspends the member and ends what they could do. */
  deactivate: (userId: string) => Promise<DeactivationResponse>
  reactivate: (userId: string) => Promise<ReactivationResponse>
}

const MembersContext = createContext<MembersContextValue | null>(null)

/**
 * Loads the caller's org member list on demand. We keep this in its own
 * provider (rather than pulling in ``SpacesContext``) because members are
 * an org-level concern and don't need to re-fetch when the current space
 * changes.
 */
export function MembersProvider({ children }: PropsWithChildren) {
  const { credentials } = useAuth()
  const [state, setState] = useState<LoadState>({ kind: 'idle' })

  const load = useCallback(async () => {
    if (!credentials) {
      setState({ kind: 'idle' })
      return
    }
    setState({ kind: 'loading' })
    try {
      const { items } = await listMembers(credentials)
      setState({ kind: 'ready', items })
    } catch (error) {
      const message =
        error instanceof ApiError ? error.detail || `HTTP ${error.status}` : String(error)
      setState({ kind: 'error', message })
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  const invite = useCallback(
    async (input: InviteMemberInput): Promise<MemberSummary> => {
      if (!credentials) throw new Error('not signed in')
      const created = await inviteMember(credentials, input)
      setState((prev) =>
        prev.kind === 'ready' ? { kind: 'ready', items: [...prev.items, created] } : prev,
      )
      return created
    },
    [credentials],
  )

  const setRole = useCallback(
    async (userId: string, role: InvitableRole): Promise<MemberSummary> => {
      if (!credentials) throw new Error('not signed in')
      const updated = await setMemberRole(credentials, userId, role)
      setState((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.map((m) => (m.id === updated.id ? updated : m)),
            }
          : prev,
      )
      return updated
    },
    [credentials],
  )

  const replaceMember = useCallback((updated: MemberSummary) => {
    setState((prev) =>
      prev.kind === 'ready'
        ? { kind: 'ready', items: prev.items.map((m) => (m.id === updated.id ? updated : m)) }
        : prev,
    )
  }, [])

  const deactivate = useCallback(
    async (userId: string): Promise<DeactivationResponse> => {
      if (!credentials) throw new Error('not signed in')
      const result = await deactivateMember(credentials, userId)
      replaceMember(result.member)
      return result
    },
    [credentials, replaceMember],
  )

  const reactivate = useCallback(
    async (userId: string): Promise<ReactivationResponse> => {
      if (!credentials) throw new Error('not signed in')
      const result = await reactivateMember(credentials, userId)
      replaceMember(result.member)
      return result
    },
    [credentials, replaceMember],
  )

  const value = useMemo<MembersContextValue>(
    () => ({ state, reload: load, invite, setRole, deactivate, reactivate }),
    [state, load, invite, setRole, deactivate, reactivate],
  )

  return <MembersContext.Provider value={value}>{children}</MembersContext.Provider>
}

export function useMembers(): MembersContextValue {
  const value = useContext(MembersContext)
  if (value === null) {
    throw new Error('useMembers must be used within a MembersProvider')
  }
  return value
}
