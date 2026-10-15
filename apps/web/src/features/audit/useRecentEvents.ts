import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import { listRecentEvents } from './api'
import type { RecentEvent } from './types'

/**
 * Live-org-pulse hook.
 *
 * * ``fetch on mount`` gives an instant seed
 * * ``poll every pollInterval ms`` keeps the sidebar warm without user input
 * * ``reload()`` gives the UI an explicit refresh affordance
 *
 * The hook is intentionally *not* wired to any global provider — each
 * consumer gets its own independent subscription. The poll cost is a
 * single lightweight GET, so duplication is cheaper than the ceremony of
 * a shared context.
 */

type LoadState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: RecentEvent[]; fetchedAt: Date }

export interface UseRecentEventsOptions {
  limit?: number
  pollIntervalMs?: number
  /** ``false`` disables background polling entirely. */
  enabled?: boolean
}

export interface UseRecentEventsResult {
  state: LoadState
  reload: () => Promise<void>
}

const DEFAULT_POLL_MS = 45_000

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

export function useRecentEvents(
  opts: UseRecentEventsOptions = {},
): UseRecentEventsResult {
  const { credentials } = useAuth()
  const { limit = 20, pollIntervalMs = DEFAULT_POLL_MS, enabled = true } = opts
  const [state, setState] = useState<LoadState>({ kind: 'idle' })

  // The ``inflight`` flag prevents overlapping fetches when the network
  // is slow and the poll timer fires before the previous request lands.
  const inflightRef = useRef(false)

  const reload = useCallback(async () => {
    if (!credentials || !enabled) {
      setState({ kind: 'idle' })
      return
    }
    if (inflightRef.current) return
    inflightRef.current = true
    setState((prev) =>
      prev.kind === 'ready' ? prev : { kind: 'loading' },
    )
    try {
      const { items } = await listRecentEvents(credentials, { limit })
      setState({ kind: 'ready', items, fetchedAt: new Date() })
    } catch (err) {
      setState({ kind: 'error', message: toMessage(err) })
    } finally {
      inflightRef.current = false
    }
  }, [credentials, enabled, limit])

  // First fetch + reload whenever inputs change.
  useEffect(() => {
    void reload()
  }, [reload])

  // Background poll. ``document.hidden`` gate avoids hammering the API
  // while the tab is backgrounded.
  useEffect(() => {
    if (!enabled || !credentials) return
    const tick = () => {
      if (document.hidden) return
      void reload()
    }
    const handle = window.setInterval(tick, pollIntervalMs)
    return () => window.clearInterval(handle)
  }, [enabled, credentials, pollIntervalMs, reload])

  return useMemo(() => ({ state, reload }), [state, reload])
}
