import type { PropsWithChildren } from 'react'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import {
  deleteDocument as apiDelete,
  downloadDocument as apiDownload,
  getDocumentsStats as apiStats,
  listDocuments as apiList,
  uploadDocument as apiUpload,
} from './api'
import type { UploadProgress } from './api'
import type {
  DocumentListFilters,
  DocumentStats,
  DocumentSummary,
  DocumentSourceKind,
  ListDocumentsParams,
} from './types'

/**
 * DocumentsProvider is scoped to a **specific** space id passed in as a
 * prop. It manages three pieces of state, each independent so the two
 * places that consume this context (the compact knowledge card and the
 * full-page space browser) can each grab what they need:
 *
 * 1. ``stats``  — cheap aggregate roll-up, loaded once on mount.
 *                 The card renders this without ever pulling documents.
 * 2. ``page``   — paginated + filterable list, loaded on demand via
 *                 ``setParams`` / ``reload``. Idle until requested.
 * 3. ``uploads``— per-file upload progress rows, cleared after success.
 *
 * Mutations (upload / delete) update ``stats`` optimistically so callers
 * that only care about counts don't need to re-fetch. The browser can
 * additionally call ``reload`` to refresh the list.
 */

type StatsState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; stats: DocumentStats }

type PageState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | {
      kind: 'ready'
      items: DocumentSummary[]
      total: number
      limit: number
      offset: number
      filters: DocumentListFilters
    }

interface UploadHandle {
  /** Stable identifier. Progress ticks recreate the surrounding object,
   * so we can't rely on object identity — a per-upload nonce lets the
   * ``done`` / ``failed`` updates always find the right row. */
  id: string
  file: File
  loaded: number
  total: number
  /** ``duplicate``: the space already has this exact file. Not a failure; nothing was added. */
  status: 'uploading' | 'done' | 'failed' | 'duplicate'
  message?: string
  /** For ``duplicate``: the existing file, when the uploader is allowed to know which one. */
  existingName?: string
}

interface DocumentsContextValue {
  spaceId: string
  // Aggregate roll-up (cheap; used by the grid card).
  stats: StatsState
  reloadStats: () => Promise<void>

  // Paginated browser (opt-in; used by the space detail page).
  page: PageState
  params: ListDocumentsParams
  setParams: (
    updater: ListDocumentsParams | ((prev: ListDocumentsParams) => ListDocumentsParams),
  ) => void
  reloadPage: () => Promise<void>

  // Uploads.
  uploads: UploadHandle[]
  upload: (file: File) => Promise<DocumentSummary | null>
  remove: (documentId: string) => Promise<void>
  download: (documentId: string) => Promise<void>
}

const DocumentsContext = createContext<DocumentsContextValue | null>(null)

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

// Bucket the mime type into the same key the API returns in
// ``by_mime_group``. Used for optimistic ``stats`` updates.
function mimeBucket(mime: string): string {
  const m = (mime || '').toLowerCase()
  if (m.startsWith('image/')) return 'image'
  if (m === 'application/pdf') return 'pdf'
  if (m.includes('word') || m.includes('opendocument.text')) return 'word'
  if (m.includes('sheet') || m.includes('excel') || m.includes('opendocument.spreadsheet'))
    return 'spreadsheet'
  if (m.includes('presentation') || m.includes('powerpoint') || m.includes('opendocument.presentation'))
    return 'presentation'
  if (m.startsWith('text/') || m === 'application/json') return 'text'
  return 'other'
}

interface ProviderProps extends PropsWithChildren {
  spaceId: string
  initialParams?: ListDocumentsParams
}

const DEFAULT_PARAMS: ListDocumentsParams = {
  order_by: 'created_at',
  order: 'desc',
  limit: 25,
  offset: 0,
}

export function DocumentsProvider({
  children,
  spaceId,
  initialParams,
}: ProviderProps) {
  const { credentials } = useAuth()

  const [stats, setStats] = useState<StatsState>({ kind: 'idle' })
  const [page, setPage] = useState<PageState>({ kind: 'idle' })
  const [params, setParamsState] = useState<ListDocumentsParams>(
    () => ({ ...DEFAULT_PARAMS, ...initialParams }),
  )
  const [uploads, setUploads] = useState<UploadHandle[]>([])

  // Guard against stale responses when the browser is being poked
  // rapidly (typing in a search box for instance).
  const pageGenRef = useRef(0)

  // -----------------------------------------------------------------
  // Stats
  // -----------------------------------------------------------------

  const reloadStats = useCallback(async () => {
    if (!credentials || !spaceId) {
      setStats({ kind: 'idle' })
      return
    }
    setStats({ kind: 'loading' })
    try {
      const fresh = await apiStats(credentials, spaceId)
      setStats({ kind: 'ready', stats: fresh })
    } catch (err) {
      setStats({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials, spaceId])

  useEffect(() => {
    void reloadStats()
  }, [reloadStats])

  // -----------------------------------------------------------------
  // Paginated browser (opt-in)
  // -----------------------------------------------------------------

  const reloadPage = useCallback(async () => {
    if (!credentials || !spaceId) {
      setPage({ kind: 'idle' })
      return
    }
    const gen = ++pageGenRef.current
    setPage({ kind: 'loading' })
    try {
      const fresh = await apiList(credentials, spaceId, params)
      if (pageGenRef.current !== gen) return
      setPage({
        kind: 'ready',
        items: fresh.items,
        total: fresh.total,
        limit: fresh.limit,
        offset: fresh.offset,
        filters: fresh.filters,
      })
    } catch (err) {
      if (pageGenRef.current !== gen) return
      setPage({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials, spaceId, params])

  const setParams = useCallback(
    (
      updater:
        | ListDocumentsParams
        | ((prev: ListDocumentsParams) => ListDocumentsParams),
    ) => {
      setParamsState((prev) =>
        typeof updater === 'function' ? updater(prev) : { ...prev, ...updater },
      )
    },
    [],
  )

  // Refetch the page whenever params change, but only if the caller
  // has ever asked for it. The card only wants stats — it never
  // triggers a page load. We use ``page.kind !== 'idle'`` as the signal
  // that this provider is in "browser mode".
  useEffect(() => {
    if (page.kind === 'idle') return
    void reloadPage()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadPage])

  // -----------------------------------------------------------------
  // Upload
  // -----------------------------------------------------------------

  const upload = useCallback(
    async (file: File): Promise<DocumentSummary | null> => {
      if (!credentials || !spaceId) return null
      const id =
        typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`
      const initial: UploadHandle = {
        id,
        file,
        loaded: 0,
        total: file.size,
        status: 'uploading',
      }
      setUploads((prev) => [...prev, initial])
      try {
        const doc = await apiUpload(credentials, spaceId, file, {
          onProgress: (p: UploadProgress) => {
            setUploads((prev) =>
              prev.map((u) =>
                u.id === id ? { ...u, loaded: p.loaded, total: p.total } : u,
              ),
            )
          },
        })
        setUploads((prev) =>
          prev.map((u) =>
            u.id === id ? { ...u, loaded: u.total, status: 'done' } : u,
          ),
        )

        // Optimistic stats update — keeps the card count moving without
        // paying for a full refetch. Server truth arrives on the next
        // manual reload.
        setStats((prev) => {
          if (prev.kind !== 'ready') return prev
          const kind = (doc.source_kind as DocumentSourceKind) || 'local'
          const bucket = mimeBucket(doc.mime_type)
          return {
            kind: 'ready',
            stats: {
              ...prev.stats,
              total: prev.stats.total + 1,
              total_size_bytes: prev.stats.total_size_bytes + doc.size_bytes,
              last_uploaded_at: doc.created_at,
              by_source: {
                ...prev.stats.by_source,
                [kind]: (prev.stats.by_source[kind] ?? 0) + 1,
              },
              by_mime_group: {
                ...prev.stats.by_mime_group,
                [bucket]: (prev.stats.by_mime_group[bucket] ?? 0) + 1,
              },
              by_indexing_state: {
                ...prev.stats.by_indexing_state,
                [doc.indexing_state]:
                  (prev.stats.by_indexing_state[doc.indexing_state] ?? 0) + 1,
              },
            },
          }
        })

        // Optimistic page prepend — only if the browser is active AND
        // the current sort would put the new row at the top (default:
        // created_at desc). Anything more clever is not worth it; a
        // reload from the caller is the escape hatch.
        setPage((prev) => {
          if (prev.kind !== 'ready') return prev
          const opts = prev.filters
          if (opts.order_by === 'created_at' && opts.order === 'desc') {
            return {
              ...prev,
              items: [doc, ...prev.items].slice(0, prev.limit),
              total: prev.total + 1,
            }
          }
          return { ...prev, total: prev.total + 1 }
        })

        // Auto-clear a successful handle after a beat.
        setTimeout(() => {
          setUploads((prev) => prev.filter((u) => u.id !== id))
        }, 1800)
        return doc
      } catch (err) {
        if (err instanceof ApiError && err.status === 409 && err.code === 'duplicate_document') {
          const existing = (err.rawDetail as { existing_name?: unknown } | undefined)?.existing_name
          setUploads((prev) =>
            prev.map((u) =>
              u.id === id
                ? {
                    ...u,
                    loaded: u.total,
                    status: 'duplicate',
                    existingName: typeof existing === 'string' ? existing : undefined,
                  }
                : u,
            ),
          )
          setTimeout(() => {
            setUploads((prev) => prev.filter((u) => u.id !== id))
          }, 8000)
          return null
        }
        const msg = toMessage(err)
        setUploads((prev) =>
          prev.map((u) =>
            u.id === id ? { ...u, status: 'failed', message: msg } : u,
          ),
        )
        return null
      }
    },
    [credentials, spaceId],
  )

  const remove = useCallback(
    async (documentId: string) => {
      if (!credentials || !spaceId) return
      // Grab the row first so we can update stats optimistically.
      let removed: DocumentSummary | null = null
      setPage((prev) => {
        if (prev.kind !== 'ready') return prev
        const found = prev.items.find((d) => d.id === documentId)
        if (found) removed = found
        return {
          ...prev,
          items: prev.items.filter((d) => d.id !== documentId),
          total: Math.max(0, prev.total - 1),
        }
      })
      await apiDelete(credentials, spaceId, documentId)
      if (removed) {
        setStats((prev) => {
          if (prev.kind !== 'ready') return prev
          const kind = (removed!.source_kind as DocumentSourceKind) || 'local'
          const bucket = mimeBucket(removed!.mime_type)
          const state = removed!.indexing_state
          return {
            kind: 'ready',
            stats: {
              ...prev.stats,
              total: Math.max(0, prev.stats.total - 1),
              total_size_bytes: Math.max(
                0,
                prev.stats.total_size_bytes - removed!.size_bytes,
              ),
              by_source: {
                ...prev.stats.by_source,
                [kind]: Math.max(0, (prev.stats.by_source[kind] ?? 1) - 1),
              },
              by_mime_group: {
                ...prev.stats.by_mime_group,
                [bucket]: Math.max(
                  0,
                  (prev.stats.by_mime_group[bucket] ?? 1) - 1,
                ),
              },
              by_indexing_state: {
                ...prev.stats.by_indexing_state,
                [state]: Math.max(
                  0,
                  (prev.stats.by_indexing_state[state] ?? 1) - 1,
                ),
              },
            },
          }
        })
      } else {
        // Card-mode delete: just refresh stats.
        await reloadStats()
      }
    },
    [credentials, spaceId, reloadStats],
  )

  const download = useCallback(
    async (documentId: string) => {
      if (!credentials || !spaceId) return
      const { blob, filename } = await apiDownload(
        credentials,
        spaceId,
        documentId,
      )
      const objectUrl = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = objectUrl
      link.download = filename
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
    },
    [credentials, spaceId],
  )

  const value = useMemo<DocumentsContextValue>(
    () => ({
      spaceId,
      stats,
      reloadStats,
      page,
      params,
      setParams,
      reloadPage,
      uploads,
      upload,
      remove,
      download,
    }),
    [
      spaceId,
      stats,
      reloadStats,
      page,
      params,
      setParams,
      reloadPage,
      uploads,
      upload,
      remove,
      download,
    ],
  )

  return (
    <DocumentsContext.Provider value={value}>
      {children}
    </DocumentsContext.Provider>
  )
}

export function useDocuments(): DocumentsContextValue {
  const value = useContext(DocumentsContext)
  if (value === null) {
    throw new Error('useDocuments must be used within a DocumentsProvider')
  }
  return value
}

export type { UploadHandle, StatsState, PageState }
