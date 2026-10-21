import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { getOverview } from './api'
import type { Overview } from './types'

type State =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; overview: Overview }

const POLL_MS = 60_000

/** The Overview numbers: loaded on mount, refreshed every minute and on demand. */
export function useOverview() {
  const { credentials } = useAuth()
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [refreshing, setRefreshing] = useState(false)
  const inflight = useRef(false)

  const reload = useCallback(async () => {
    if (!credentials || inflight.current) return
    inflight.current = true
    setRefreshing(true)
    try {
      setState({ kind: 'ready', overview: await getOverview(credentials) })
    } catch (err) {
      const message = err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
      // Keep showing the last good numbers if a refresh fails.
      setState((prev) => (prev.kind === 'ready' ? prev : { kind: 'error', message }))
    } finally {
      inflight.current = false
      setRefreshing(false)
    }
  }, [credentials])

  useEffect(() => {
    void reload()
    const timer = window.setInterval(() => void reload(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [reload])

  return useMemo(() => ({ state, refreshing, reload }), [state, refreshing, reload])
}
