import { FileText, ImageOff, Loader2, X } from 'lucide-react'
import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/AuthContext'
import { downloadDocument, downloadDocumentVersion } from '../documents/api'

import { isUnavailable, useProtectedImage } from './ImageMatches'
import type { MessageCitation } from './types'

/** True when the cited passage came out of a picture, so there is no document text to open. */
export function isPictureSource(c: Pick<MessageCitation, 'source_kind'>): boolean {
  return c.source_kind === 'image_ocr' || c.source_kind === 'image_caption'
}

/**
 * What opens when a source is a picture: the picture itself, and under it the passage that
 * was cited (the text read from it, or a model's description of it).
 *
 * A picture has no document text to re-extract, so the text viewer cannot open it. The
 * passage shown is the one stored with the citation, not a fresh read of the file.
 */
export function PictureSourceViewer({
  citation,
  onClose,
}: {
  citation: MessageCitation
  onClose: () => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const described = citation.source_kind === 'image_caption'
  const hidden = citation.text_hidden === true

  // A source the reader can no longer see is not fetched at all.
  const state = useProtectedImage(
    credentials && !hidden
      ? async () => {
          const got =
            citation.version_number != null
              ? await downloadDocumentVersion(
                  credentials,
                  citation.space_id,
                  citation.document_id,
                  citation.version_number,
                )
              : await downloadDocument(credentials, citation.space_id, citation.document_id)
          return got.blob
        }
      : null,
    `source/${citation.space_id}/${citation.document_id}/${citation.version_number ?? ''}`,
  )

  useEffect(() => {
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [onClose])

  const name = citation.document_name || t('documents.unknownUploader')

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
              <strong>{name}</strong>
            </div>
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

        <div className="doc-text-viewer-body">
          {hidden ? (
            <div className="state-panel">{t('chat.citations.textHidden')}</div>
          ) : (
            <>
              {state.kind === 'loading' && (
                <div className="state-panel">
                  <Loader2 className="spin" size={16} />
                  {t('chat.citations.pictureLoading')}
                </div>
              )}
              {state.kind === 'failed' && (
                <div className="state-panel error-state" role="alert">
                  <ImageOff size={16} />
                  {isUnavailable(state)
                    ? t('chat.imageSearch.unavailable')
                    : t('chat.citations.pictureFailed')}
                </div>
              )}
              {state.kind === 'ready' && (
                <img alt={name} className="picture-source-img" src={state.url} />
              )}
              <section className="picture-source-passage">
                <h3>
                  {described
                    ? t('chat.citations.pictureDescribedTitle')
                    : t('chat.citations.pictureTextTitle')}
                </h3>
                <small>
                  {described ? t('chat.citations.describedHint') : t('chat.citations.scannedHint')}
                </small>
                <pre className="doc-text-viewer-content">{citation.text}</pre>
              </section>
            </>
          )}
        </div>
      </aside>
    </div>,
    document.body,
  )
}
