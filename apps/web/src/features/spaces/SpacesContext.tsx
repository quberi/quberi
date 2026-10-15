import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { localizedName } from '../../i18n/localizedName'

import { createSpace, listSpaces } from './api'
import type { CreateSpaceInput, SpaceSummary } from './types'

/**
 * Loads and mutates the caller's space list. Also maintains ``currentSlug``,
 * which the header switcher and the overview card read from and the URL
 * query-string (``?space=<slug>``) mirrors.
 */

type LoadState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: SpaceSummary[] }

interface SpacesContextValue {
  state: LoadState
  currentSlug: string | null
  currentSpace: SpaceSummary | null
  selectSlug: (slug: string) => void
  reload: () => Promise<void>
  createNewSpace: (input: CreateSpaceInput) => Promise<SpaceSummary>
}

const SpacesContext = createContext<SpacesContextValue | null>(null)

const SPACE_QUERY_PARAM = 'space'

function readSpaceFromUrl(): string | null {
  if (typeof window === 'undefined') return null
  const value = new URL(window.location.href).searchParams.get(SPACE_QUERY_PARAM)
  return value && value.length > 0 ? value : null
}

function writeSpaceToUrl(slug: string | null): void {
  const url = new URL(window.location.href)
  if (slug) url.searchParams.set(SPACE_QUERY_PARAM, slug)
  else url.searchParams.delete(SPACE_QUERY_PARAM)
  window.history.replaceState({}, '', url.toString())
}

export function SpacesProvider({ children }: PropsWithChildren) {
  const { credentials } = useAuth()
  const { i18n } = useTranslation()
  const [rawState, setState] = useState<LoadState>({ kind: 'idle' })
  const [currentSlug, setCurrentSlug] = useState<string | null>(() => readSpaceFromUrl())

  // The bilingual sample spaces: a guest sees only the one in the interface
  // language, so every title, document and citation they meet is in one
  // language. Ordinary spaces have no locale and always show.
  const uiLocale = i18n.resolvedLanguage === 'zh-CN' ? 'zh-CN' : 'en-US'
  const isGuest = credentials?.isGuest === true
  const state = useMemo<LoadState>(() => {
    if (rawState.kind !== 'ready' || !isGuest) return rawState
    return {
      kind: 'ready',
      items: rawState.items
        .filter((s) => !s.locale || s.locale === uiLocale)
        // The shared sandbox stores "中文 / English" in one string.
        .map((s) => ({
          ...s,
          name: localizedName(s.name, uiLocale),
        })),
    }
  }, [rawState, isGuest, uiLocale])

  const load = useCallback(async () => {
    if (!credentials) {
      setState({ kind: 'idle' })
      return
    }
    setState({ kind: 'loading' })
    try {
      const { items } = await listSpaces(credentials)
      setState({ kind: 'ready', items })

      // Reconcile ``currentSlug`` against the freshly-loaded list. If the URL
      // pointed at a slug we cannot see, fall back to the first row.
      setCurrentSlug((existing) => {
        if (existing && items.some((s) => s.slug === existing)) return existing
        const next = items[0]?.slug ?? null
        writeSpaceToUrl(next)
        return next
      })
    } catch (error) {
      const message = error instanceof ApiError ? error.detail || `HTTP ${error.status}` : String(error)
      setState({ kind: 'error', message })
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  // Switching language can leave the selected space filtered out: move to a
  // visible one (the other-language sample space becomes the match).
  useEffect(() => {
    if (state.kind !== 'ready') return
    if (currentSlug && state.items.some((s) => s.slug === currentSlug)) return
    const next = state.items[0]?.slug ?? null
    if (next !== currentSlug) {
      writeSpaceToUrl(next)
      setCurrentSlug(next)
    }
  }, [state, currentSlug])

  const selectSlug = useCallback((slug: string) => {
    writeSpaceToUrl(slug)
    setCurrentSlug(slug)
  }, [])

  const createNewSpace = useCallback(
    async (input: CreateSpaceInput): Promise<SpaceSummary> => {
      if (!credentials) throw new Error('not signed in')
      const detail = await createSpace(credentials, input)
      const summary: SpaceSummary = {
        id: detail.id,
        name: detail.name,
        slug: detail.slug,
        preset: detail.preset,
        visibility: detail.visibility,
        trust_level: detail.trust_level,
        status: detail.status,
        owner_type: detail.owner_type,
        owner_id: detail.owner_id,
        is_owner: detail.is_owner,
        locale: detail.locale ?? null,
        updated_at: detail.updated_at,
      }
      setState((prev) =>
        prev.kind === 'ready' ? { kind: 'ready', items: [...prev.items, summary] } : prev,
      )
      selectSlug(summary.slug)
      return summary
    },
    [credentials, selectSlug],
  )

  const currentSpace = useMemo<SpaceSummary | null>(() => {
    if (state.kind !== 'ready' || !currentSlug) return null
    return state.items.find((s) => s.slug === currentSlug) ?? null
  }, [state, currentSlug])

  const value = useMemo<SpacesContextValue>(
    () => ({
      state,
      currentSlug,
      currentSpace,
      selectSlug,
      reload: load,
      createNewSpace,
    }),
    [state, currentSlug, currentSpace, selectSlug, load, createNewSpace],
  )

  return <SpacesContext.Provider value={value}>{children}</SpacesContext.Provider>
}

export function useSpaces(): SpacesContextValue {
  const value = useContext(SpacesContext)
  if (value === null) {
    throw new Error('useSpaces must be used within a SpacesProvider')
  }
  return value
}
