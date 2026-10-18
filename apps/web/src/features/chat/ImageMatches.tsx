import { ImageOff, Images, Loader2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { downloadDocument } from '../documents/api'

import { fetchThumbnail } from './api'
import type { ImageMatch } from './types'

type BlobState =
  | { kind: 'loading' }
  // ``status`` is the HTTP status when the server answered. 404 means the picture was deleted
  // or the person can no longer see it; the server deliberately does not say which.
  | { kind: 'failed'; status?: number }
  | { kind: 'ready'; url: string }

/** True when the failure means "gone, or no longer yours to see". */
function isUnavailable(state: BlobState): boolean {
  return state.kind === 'failed' && state.status === 404
}

/**
 * Turn a fetch of protected bytes into an object URL an ``<img>`` can show.
 *
 * Pictures sit behind the same permission check as any document, so a bare
 * ``<img src>`` (which cannot send the session header) will not do. The URL is
 * revoked when the picture goes away or changes.
 */
function useProtectedImage(load: ((signal: AbortSignal) => Promise<Blob>) | null, key: string): BlobState {
  const [state, setState] = useState<BlobState>({ kind: 'loading' })
  useEffect(() => {
    if (!load) {
      setState({ kind: 'failed' })
      return
    }
    const controller = new AbortController()
    let url: string | null = null
    setState({ kind: 'loading' })
    load(controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return
        url = URL.createObjectURL(blob)
        setState({ kind: 'ready', url })
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setState({ kind: 'failed', status: err instanceof ApiError ? err.status : undefined })
        }
      })
    return () => {
      controller.abort()
      if (url) URL.revokeObjectURL(url)
    }
    // ``load`` is rebuilt every render; ``key`` says when it really changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return state
}

/** A found picture's preview. Shows a placeholder while loading and a quiet icon if it cannot be loaded. */
export function MatchThumbnail({ match }: { match: ImageMatch }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const state = useProtectedImage(
    credentials
      ? (signal) => fetchThumbnail(credentials, match.space_id, match.document_id, signal)
      : null,
    `${match.space_id}/${match.document_id}`,
  )
  if (state.kind === 'ready') {
    return <img alt={match.document_name} className="image-match-thumb" src={state.url} />
  }
  const unavailable = isUnavailable(state)
  const hint = unavailable ? t('chat.imageSearch.unavailable') : t('chat.imageSearch.noPreview')
  return (
    <span
      aria-label={state.kind === 'failed' ? hint : undefined}
      className="image-match-thumb image-match-thumb-empty"
      role={state.kind === 'failed' ? 'img' : undefined}
    >
      {state.kind === 'loading' ? <Loader2 className="spin" size={16} /> : <ImageOff size={18} />}
      {unavailable && <small className="image-match-gone">{hint}</small>}
    </span>
  )
}

/** The full-size picture in a dialog. Fetched on open (it can be large). */
export function ImageViewer({ match, onClose }: { match: ImageMatch; onClose: () => void }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const state = useProtectedImage(
    credentials
      ? async () => {
          try {
            return (await downloadDocument(credentials, match.space_id, match.document_id)).blob
          } catch (err) {
            setError(err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err))
            throw err
          }
        }
      : null,
    `full/${match.space_id}/${match.document_id}`,
  )

  useEffect(() => {
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [onClose])

  return createPortal(
    <div className="doc-drawer-backdrop" onClick={onClose} role="presentation">
      <div
        aria-label={match.document_name}
        aria-modal="true"
        className="image-viewer"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
      >
        <header className="image-viewer-head">
          <div>
            <strong>{match.document_name}</strong>
            <small>{match.space_name}</small>
          </div>
          <button
            aria-label={t('knowledge.detail.close')}
            autoFocus
            className="icon-button"
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <div className="image-viewer-body">
          {state.kind === 'loading' && <Loader2 className="spin" size={22} />}
          {state.kind === 'failed' && (
            <div className="state-panel error-state">
              {isUnavailable(state)
                ? t('chat.imageSearch.unavailable')
                : (error ?? t('chat.imageSearch.noPreview'))}
            </div>
          )}
          {state.kind === 'ready' && (
            <img alt={match.document_name} className="image-viewer-img" src={state.url} />
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}

/**
 * Right-hand rail for a picture-search answer: the pictures found, best match
 * first, each with its name, the space it lives in and how close it is.
 */
export function ImageMatchesRail({
  matches,
  railRef,
  related = false,
}: {
  matches: ImageMatch[]
  railRef: React.RefObject<HTMLDivElement | null>
  /** The pictures ride along with a text answer: they are offered as "possibly related". */
  related?: boolean
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState<ImageMatch | null>(null)
  return (
    <aside className="chat-citations-rail image-matches-rail" ref={railRef}>
      <header className="chat-citations-rail-head">
        <Images size={14} />
        <span>{t(related ? 'chat.imageSearch.relatedTitle' : 'chat.imageSearch.railTitle')}</span>
        <small>{t('chat.imageSearch.railCount', { count: matches.length })}</small>
      </header>
      <ul className="image-matches-grid">
        {matches.map((m) => (
          <li key={m.document_id}>
            <button
              className="image-match-card"
              onClick={() => setOpen(m)}
              title={t('chat.imageSearch.open')}
              type="button"
            >
              <MatchThumbnail match={m} />
              <span className="image-match-name" title={m.document_name}>
                {m.document_name}
              </span>
              <small className="image-match-meta">
                <span title={m.space_name}>{m.space_name}</span>
                {m.score !== undefined && (
                  <>
                    <span className="dot-sep">·</span>
                    <span>{t('chat.imageSearch.score', { score: m.score.toFixed(2) })}</span>
                  </>
                )}
              </small>
            </button>
          </li>
        ))}
      </ul>
      {open && <ImageViewer match={open} onClose={() => setOpen(null)} />}
    </aside>
  )
}
