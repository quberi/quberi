/**
 * REST wrappers for documents + document sources.
 *
 * Uploads go through raw ``fetch`` (rather than the shared ``apiFetch``
 * helper) because they need multipart form-data and progress events —
 * two things ``apiFetch`` doesn't do today.
 */

import { API_BASE, ApiError, apiFetch, buildAuthHeaders } from '../api/client'
import type { DevCredentials } from '../auth/types'

import type {
  ConnectorResult,
  DocumentList,
  DocumentSource,
  DocumentSourceList,
  DocumentStats,
  DocumentSummary,
  ListDocumentsParams,
  RegisterSourceInput,
} from './types'

// ---------------------------------------------------------------------------
// Space-scoped document endpoints
// ---------------------------------------------------------------------------

export function listDocuments(
  credentials: DevCredentials,
  spaceId: string,
  params: ListDocumentsParams = {},
): Promise<DocumentList> {
  const search = new URLSearchParams()
  if (params.q) search.set('q', params.q)
  if (params.source_kind) search.set('source_kind', params.source_kind)
  if (params.file_type) search.set('file_type', params.file_type)
  if (params.order_by) search.set('order_by', params.order_by)
  if (params.order) search.set('order', params.order)
  if (params.include_archived !== undefined)
    search.set('include_archived', String(params.include_archived))
  if (params.limit !== undefined) search.set('limit', String(params.limit))
  if (params.offset !== undefined) search.set('offset', String(params.offset))
  const suffix = search.toString()
  return apiFetch<DocumentList>(
    `/api/v1/spaces/${spaceId}/documents${suffix ? `?${suffix}` : ''}`,
    { credentials },
  )
}

export function getDocumentsStats(
  credentials: DevCredentials,
  spaceId: string,
): Promise<DocumentStats> {
  return apiFetch<DocumentStats>(
    `/api/v1/spaces/${spaceId}/documents/stats`,
    { credentials },
  )
}

export function getDocument(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
): Promise<DocumentSummary> {
  return apiFetch<DocumentSummary>(
    `/api/v1/spaces/${spaceId}/documents/${documentId}`,
    { credentials },
  )
}

export function deleteDocument(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
): Promise<void> {
  // A guest deletes only files uploaded from this browser, via its own endpoint.
  const path = credentials.isGuest
    ? `/api/v1/guest/uploads/${documentId}`
    : `/api/v1/spaces/${spaceId}/documents/${documentId}`
  return apiFetch<void>(path, { method: 'DELETE', credentials })
}

/**
 * Re-extracted full plain text of a document — the same deterministic
 * extraction the RAG indexer ran, so a citation's ``char_start``/
 * ``char_end`` offsets line up with this text exactly. Not cached
 * server-side; the caller should cache per ``documentId`` client-side if
 * it's opened repeatedly in one session.
 */
export function getDocumentText(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
): Promise<{ document_id: string; text: string }> {
  return apiFetch<{ document_id: string; text: string }>(
    `/api/v1/spaces/${spaceId}/documents/${documentId}/text`,
    { credentials },
  )
}

/**
 * Build a browser-friendly download URL. Auth is passed via headers, so
 * we return an ``async`` function that fetches with the right headers and
 * hands the caller a blob-URL to click.
 */
export async function downloadDocument(
  credentials: DevCredentials,
  spaceId: string,
  documentId: string,
): Promise<{ blob: Blob; filename: string }> {
  const url = `${API_BASE}/api/v1/spaces/${spaceId}/documents/${documentId}/download`
  const response = await fetch(url, {
    headers: buildAuthHeaders(credentials),
  })
  if (!response.ok) {
    let detail = ''
    try {
      const body = (await response.json()) as { detail?: string }
      detail = body.detail ?? ''
    } catch {
      /* swallow */
    }
    throw new ApiError(response.status, detail)
  }
  const disposition = response.headers.get('content-disposition') ?? ''
  const filename = extractFilename(disposition)
  const blob = await response.blob()
  return { blob, filename }
}

/**
 * Multipart upload with progress. Uses XHR because the fetch API still
 * doesn't expose native upload-progress events in browsers.
 */
export interface UploadProgress {
  loaded: number
  total: number
}

export function uploadDocument(
  credentials: DevCredentials,
  spaceId: string,
  file: File,
  {
    onProgress,
    signal,
  }: {
    onProgress?: (p: UploadProgress) => void
    signal?: AbortSignal
  } = {},
): Promise<DocumentSummary> {
  return new Promise<DocumentSummary>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    // Guests upload through a dedicated, rate-limited endpoint (the ordinary one
    // is blocked for them) into the shared sandbox; the file is tagged with
    // their browser token by the ``X-Guest-Token`` header added below.
    const url = credentials.isGuest
      ? `${API_BASE}/api/v1/guest/uploads`
      : `${API_BASE}/api/v1/spaces/${spaceId}/documents`
    xhr.open('POST', url, true)
    // Use the shared auth-header builder so real session tokens (and the
    // legacy dev headers) both work — hand-rolling ``Bearer adminToken``
    // breaks real logins whose adminToken is empty.
    for (const [key, value] of Object.entries(buildAuthHeaders(credentials))) {
      xhr.setRequestHeader(key, value)
    }
    xhr.responseType = 'json'

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress({ loaded: event.loaded, total: event.total })
      }
    }
    xhr.onerror = () => reject(new ApiError(0, 'network error'))
    xhr.onabort = () => reject(new ApiError(0, 'upload aborted'))
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response as DocumentSummary)
        // (For guests the response is a small subset; callers re-read the list.)
      } else {
        const body = xhr.response as { detail?: string } | undefined
        reject(new ApiError(xhr.status, body?.detail ?? ''))
      }
    }
    if (signal) {
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }

    const form = new FormData()
    form.append('file', file, file.name)
    xhr.send(form)
  })
}

// ---------------------------------------------------------------------------
// Org-scoped document-source endpoints
// ---------------------------------------------------------------------------

export function listSources(
  credentials: DevCredentials,
): Promise<DocumentSourceList> {
  return apiFetch<DocumentSourceList>(
    '/api/v1/organizations/current/document-sources',
    { credentials },
  )
}

export function registerSource(
  credentials: DevCredentials,
  input: RegisterSourceInput,
): Promise<DocumentSource> {
  return apiFetch<DocumentSource>(
    '/api/v1/organizations/current/document-sources',
    { method: 'POST', body: input, credentials },
  )
}

export function testSource(
  credentials: DevCredentials,
  sourceId: string,
): Promise<ConnectorResult> {
  return apiFetch<ConnectorResult>(
    `/api/v1/organizations/current/document-sources/${sourceId}:test`,
    { method: 'POST', body: {}, credentials },
  )
}

export function syncSource(
  credentials: DevCredentials,
  sourceId: string,
): Promise<ConnectorResult> {
  // A folder sync fetches + embeds every doc synchronously; give it a
  // generous 5-minute client-side ceiling. Webhook-driven incremental
  // sync (wave B) removes the need for this long blocking call.
  return apiFetch<ConnectorResult>(
    `/api/v1/organizations/current/document-sources/${sourceId}:sync`,
    { method: 'POST', body: {}, credentials, timeoutMs: 300_000 },
  )
}

export function deleteSource(
  credentials: DevCredentials,
  sourceId: string,
): Promise<void> {
  return apiFetch<void>(
    `/api/v1/organizations/current/document-sources/${sourceId}`,
    { method: 'DELETE', credentials },
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function extractFilename(disposition: string): string {
  // ``filename*=UTF-8''name`` (RFC 5987) wins over the plain filename.
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      /* fall through */
    }
  }
  const plain = disposition.match(/filename="([^"]+)"/i)
  return plain ? plain[1] : 'download'
}
