import { FileText, Loader2, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import { getDocumentText } from './api'

/**
 * Modal that re-extracts and renders a document's full plain text, with
 * one character range highlighted and auto-scrolled into view.
 *
 * Used from the chat citations panel: clicking a citation card opens the
 * source document here instead of only showing a short text preview.
 * The text is re-extracted on demand (never persisted — see
 * ``documents_service.get_document_text``) rather than rendering the
 * original file format (PDF/DOCX layout), so the highlighted span lines
 * up exactly with the ``char_start``/``char_end`` offsets recorded at
 * index time without needing a PDF/DOCX renderer.
 */
export function DocumentTextViewer({
  spaceId,
  documentId,
  documentName,
  charStart,
  charEnd,
  version,
  onClose,
}: {
  spaceId: string
  documentId: string
  documentName: string
  /** Open this version of the file (a passage quoted from an earlier version). */
  version?: number
  /** Highlight range into the re-extracted text. Omit to just open the
   * document with no highlight (e.g. a citation indexed before this
   * feature existed). */
  charStart?: number
  charEnd?: number
  onClose: () => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; message: string }
    | { kind: 'ready'; text: string }
  >({ kind: 'loading' })
  const highlightRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (!credentials) return
    let cancelled = false
    setState({ kind: 'loading' })
    getDocumentText(credentials, spaceId, documentId, version)
      .then((res) => {
        if (!cancelled) setState({ kind: 'ready', text: res.text })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setState({
          kind: 'error',
          message:
            err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
        })
      })
    return () => {
      cancelled = true
    }
  }, [credentials, spaceId, documentId, version])

  // Scroll the highlighted span into view once the text has rendered.
  useEffect(() => {
    if (state.kind !== 'ready') return
    const el = highlightRef.current
    if (!el) return
    // Let the DOM paint first so ``scrollIntoView`` has real layout to
    // work with.
    const handle = requestAnimationFrame(() => {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
    return () => cancelAnimationFrame(handle)
  }, [state])

  useEffect(() => {
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [onClose])

  const hasHighlight =
    typeof charStart === 'number' &&
    typeof charEnd === 'number' &&
    charEnd > charStart

  return createPortal(
    <div className="doc-drawer-backdrop" onClick={onClose} role="presentation">
      <aside
        aria-modal="true"
        className="doc-drawer doc-text-viewer"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
      >
        <header className="doc-drawer-head">
          <div className="doc-drawer-title">
            <FileText size={16} />
            <div>
              <strong>{documentName || t('documents.unknownUploader')}</strong>
              {hasHighlight && (
                <small>{t('chat.citations.viewerHighlightHint')}</small>
              )}
            </div>
          </div>
          <button
            aria-label={t('knowledge.detail.close')}
            className="icon-button"
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>

        <div className="doc-text-viewer-body">
          {state.kind === 'loading' && (
            <div className="state-panel">
              <Loader2 className="spin" size={16} />
              {t('chat.citations.viewerLoading')}
            </div>
          )}
          {state.kind === 'error' && (
            <div className="state-panel error-state">{state.message}</div>
          )}
          {state.kind === 'ready' && (
            <HighlightedText
              text={state.text}
              charStart={hasHighlight ? charStart : undefined}
              charEnd={hasHighlight ? charEnd : undefined}
              highlightRef={highlightRef}
            />
          )}
        </div>
      </aside>
    </div>,
    document.body,
  )
}

function HighlightedText({
  text,
  charStart,
  charEnd,
  highlightRef,
}: {
  text: string
  charStart?: number
  charEnd?: number
  highlightRef: React.RefObject<HTMLSpanElement | null>
}) {
  if (charStart === undefined || charEnd === undefined) {
    return <p className="doc-text-viewer-content">{text}</p>
  }
  // Clamp defensively — a stale citation (document re-indexed with
  // different chunking since) could point past the current text length.
  const start = Math.max(0, Math.min(charStart, text.length))
  const end = Math.max(start, Math.min(charEnd, text.length))
  return (
    <p className="doc-text-viewer-content">
      {text.slice(0, start)}
      <span className="doc-text-highlight" ref={highlightRef}>
        {text.slice(start, end)}
      </span>
      {text.slice(end)}
    </p>
  )
}
