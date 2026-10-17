import {
  Archive,
  Cloud,
  Database,
  Download,
  File as FileIcon,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileType,
  Presentation,
  Trash2,
} from 'lucide-react'
import type { ComponentType } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import { useDocuments } from './DocumentsContext'
import type { DocumentSourceKind, DocumentSummary } from './types'
import { useFormatDate } from '../../i18n/formatDate'

/**
 * One row per document. Extracted from the old ``DocumentsPanel`` so the
 * Knowledge page's per-space cards can render the same row shape without
 * pulling in the whole panel chrome.
 */

const MIME_ICON: Array<[RegExp, ComponentType<{ size?: number }>]> = [
  [/^image\//, FileImage],
  [/pdf/, FileText],
  [/word|document|opendocument\.text/, FileText],
  [/sheet|excel|opendocument\.spreadsheet/, FileSpreadsheet],
  [/presentation|powerpoint|opendocument\.presentation/, Presentation],
  [/^text\//, FileType],
  [/json/, FileType],
]

function iconForMime(mime: string): ComponentType<{ size?: number }> {
  for (const [rx, icon] of MIME_ICON) {
    if (rx.test(mime)) return icon
  }
  return FileIcon
}

function sourceKindIcon(
  kind: DocumentSourceKind,
): ComponentType<{ size?: number }> {
  switch (kind) {
    case 'local':
      return Archive
    case 'feishu':
      return Cloud
    case 'oss_aliyun':
      return Database
    case 's3_aws':
      return Database
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

export function DocumentRow({ doc }: { doc: DocumentSummary }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const { download, remove } = useDocuments()
  const Icon = iconForMime(doc.mime_type)
  const SourceIcon = sourceKindIcon(doc.source_kind)

  const doDownload = async () => {
    try {
      await download(doc.id)
    } catch (err) {
      window.alert(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    }
  }

  const doDelete = async () => {
    if (!window.confirm(t('documents.confirmDelete', { name: doc.name }))) return
    try {
      await remove(doc.id)
    } catch (err) {
      window.alert(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    }
  }

  return (
    <li className="document-row">
      <span className="document-row-icon">
        <Icon size={16} />
      </span>
      <span className="document-row-body">
        <strong>{doc.name}</strong>
        <small>
          <span className={`chip chip-inline chip-source-${doc.source_kind}`}>
            <SourceIcon size={10} />
            {t(`documents.source.${doc.source_kind}`)}
          </span>
          <span className="dot-sep">·</span>
          {formatBytes(doc.size_bytes)}
          <span className="dot-sep">·</span>
          {doc.uploader_display_name || t('documents.unknownUploader')}
          <span className="dot-sep">·</span>
          {formatDate(doc.created_at)}
          {doc.review_state === 'pending' && (
            <span className="badge badge-pending">{t('uploadReview.badge.pending')}</span>
          )}
          {doc.review_state === 'rejected' && (
            <span
              className="badge badge-pending"
              title={doc.review_note ?? undefined}
            >
              {t('uploadReview.badge.rejected')}
              {doc.review_note ? `: ${doc.review_note}` : ''}
            </span>
          )}
        </small>
      </span>
      <span className="document-row-actions">
        <button
          className="secondary-button icon-button"
          onClick={() => void doDownload()}
          title={t('documents.download')}
          type="button"
        >
          <Download size={13} />
        </button>
        <button
          className="icon-button danger"
          onClick={() => void doDelete()}
          title={t('documents.delete')}
          type="button"
        >
          <Trash2 size={13} />
        </button>
      </span>
    </li>
  )
}
