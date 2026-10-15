import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import {
  addAccessBinding,
  listAccessBindings,
  listEffectiveMembers,
  removeAccessBinding,
} from './api'
import type {
  AccessBinding,
  AddAccessBindingInput,
  EffectiveMember,
} from './types'

/**
 * Hook version rather than a Provider, since the panel is space-scoped and
 * we prefer a local subscription rather than an app-wide context. The hook
 * re-fetches automatically when ``spaceId`` changes.
 */

type LoadState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: AccessBinding[] }

export interface UseSpaceAccessResult {
  state: LoadState
  reload: () => Promise<void>
  grant: (input: AddAccessBindingInput) => Promise<AccessBinding>
  revoke: (bindingId: string) => Promise<void>
}

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

export function useSpaceAccess(spaceId: string | null): UseSpaceAccessResult {
  const { credentials } = useAuth()
  const [state, setState] = useState<LoadState>({ kind: 'idle' })
  const loadedForRef = useRef<string | null>(null)

  const reload = useCallback(async () => {
    if (!credentials || !spaceId) {
      setState({ kind: 'idle' })
      return
    }
    loadedForRef.current = spaceId
    setState({ kind: 'loading' })
    try {
      const { items } = await listAccessBindings(credentials, spaceId)
      if (loadedForRef.current !== spaceId) return // superseded
      setState({ kind: 'ready', items })
    } catch (err) {
      if (loadedForRef.current !== spaceId) return
      setState({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials, spaceId])

  useEffect(() => {
    void reload()
  }, [reload])

  const grant = useCallback(
    async (input: AddAccessBindingInput): Promise<AccessBinding> => {
      if (!credentials || !spaceId) throw new Error('no space selected')
      const created = await addAccessBinding(credentials, spaceId, input)
      setState((prev) =>
        prev.kind === 'ready'
          ? { kind: 'ready', items: [...prev.items, created] }
          : prev,
      )
      return created
    },
    [credentials, spaceId],
  )

  const revoke = useCallback(
    async (bindingId: string): Promise<void> => {
      if (!credentials || !spaceId) throw new Error('no space selected')
      await removeAccessBinding(credentials, spaceId, bindingId)
      setState((prev) =>
        prev.kind === 'ready'
          ? {
              kind: 'ready',
              items: prev.items.filter((b) => b.id !== bindingId),
            }
          : prev,
      )
    },
    [credentials, spaceId],
  )

  return useMemo(
    () => ({ state, reload, grant, revoke }),
    [state, reload, grant, revoke],
  )
}

// ---------------------------------------------------------------------------
// Effective-members hook
// ---------------------------------------------------------------------------
//
// Split from ``useSpaceAccess`` because the two lists refresh on different
// triggers: direct grants change on grant/revoke, effective members ALSO
// change when someone joins/leaves a group, changes primary position, or
// moves org units. A component can subscribe to either or both, and can
// call ``reload()`` on the effective hook after mutating any of the four
// upstream contexts.

type EffectiveState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: EffectiveMember[] }

export interface UseEffectiveMembersResult {
  state: EffectiveState
  reload: () => Promise<void>
}

function effectiveToMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

export function useEffectiveSpaceMembers(
  spaceId: string | null,
): UseEffectiveMembersResult {
  const { credentials } = useAuth()
  const [state, setState] = useState<EffectiveState>({ kind: 'idle' })
  const loadedForRef = useRef<string | null>(null)

  const reload = useCallback(async () => {
    if (!credentials || !spaceId) {
      setState({ kind: 'idle' })
      return
    }
    loadedForRef.current = spaceId
    setState({ kind: 'loading' })
    try {
      const { items } = await listEffectiveMembers(credentials, spaceId)
      if (loadedForRef.current !== spaceId) return
      setState({ kind: 'ready', items })
    } catch (err) {
      if (loadedForRef.current !== spaceId) return
      setState({ kind: 'error', message: effectiveToMessage(err) })
    }
  }, [credentials, spaceId])

  useEffect(() => {
    void reload()
  }, [reload])

  return useMemo(() => ({ state, reload }), [state, reload])
}
