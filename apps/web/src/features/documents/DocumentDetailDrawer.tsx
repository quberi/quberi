import {
  Bot,
  Clock3,
  Cpu,
  Database,
  Download,
  FileText,
  History,
  Info,
  ShieldCheck,
  Sparkles,
  Upload,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { downloadDocumentVersion, listDocumentVersions, uploadNewVersion } from './api'
import { DocumentAccessTab } from './DocumentAccessTab'

import { formatBytes } from './DocumentRow'
import { useDocuments } from './DocumentsContext'
import type { DocumentSummary, DocumentVersion, IndexingState } from './types'
import { useFormatDate } from '../../i18n/formatDate'

/**
 * Right-side drawer that shows one document's metadata + RAG state.
 *
 * Two tabs:
 *   * Properties — file name, size, mime, uploader, checksum, storage key,
 *                  source, timestamps. Static reads from the page cache.
 *   * RAG status — placeholder until the indexer ships. Renders whatever
 *                  the ``indexing_state`` column carries; today every fresh
 *                  upload lands as ``not_indexed`` with ``chunk_count=0``.
 *                  When the RAG slice lands, this view auto-populates.
 *
 * The drawer reads directly from the current ``page`` items so opening
 * and closing has no extra fetches. If the doc is not on the currently
 * loaded page (rare — user paged away before opening) we show a
 * "not found on this page" hint rather than a 404.
 */
export function DocumentDetailDrawer({
  documentId,
  onClose,
  canAddVersion = false,
}: {
  documentId: string
  onClose: () => void
  /** Whether to offer "upload a new version" (the server still decides). */
  canAddVersion?: boolean
}) {
  const { t } = useTranslation()
  const { page, download } = useDocuments()
  const [tab, setTab] = useState<'properties' | 'rag' | 'versions' | 'access'>('properties')

  const doc: DocumentSummary | null = useMemo(() => {
    if (page.kind !== 'ready') return null
    return page.items.find((d) => d.id === documentId) ?? null
  }, [page, documentId])

  const doDownload = async () => {
    if (!doc) return
    try {
      await download(doc.id)
    } catch (err) {
      window.alert(
        err instanceof ApiError
          ? err.detail || `HTTP ${err.status}`
          : String(err),
      )
    }
  }

  return (
    <div className="doc-drawer-backdrop" onClick={onClose} role="presentation">
      <aside
        aria-modal="true"
        className="doc-drawer"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
      >
        <header className="doc-drawer-head">
          <div className="doc-drawer-title">
            <FileText size={16} />
            <div>
              <strong>{doc?.name ?? t('knowledge.detail.notFoundTitle')}</strong>
              {doc && (
                <small>
                  {formatBytes(doc.size_bytes)}
                  <span className="dot-sep">·</span>
                  {doc.mime_type}
                </small>
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

        {doc && (
          <>
            <div className="doc-drawer-tabs" role="tablist">
              <button
                aria-selected={tab === 'properties'}
                className={tab === 'properties' ? 'active' : ''}
                onClick={() => setTab('properties')}
                role="tab"
                type="button"
              >
                <Info size={13} /> {t('knowledge.detail.tabProperties')}
              </button>
              <button
                aria-selected={tab === 'rag'}
                className={tab === 'rag' ? 'active' : ''}
                onClick={() => setTab('rag')}
                role="tab"
                type="button"
              >
                <Sparkles size={13} /> {t('knowledge.detail.tabRag')}
              </button>
              {doc.storage_key && (
                <button
                  aria-selected={tab === 'versions'}
                  className={tab === 'versions' ? 'active' : ''}
                  onClick={() => setTab('versions')}
                  role="tab"
                  type="button"
                >
                  <History size={13} /> {t('knowledge.versions.tab')}
                </button>
              )}
              <button
                aria-selected={tab === 'access'}
                className={tab === 'access' ? 'active' : ''}
                onClick={() => setTab('access')}
                role="tab"
                type="button"
              >
                <ShieldCheck size={13} /> {t('knowledge.detail.tabAccess')}
              </button>
            </div>

            {tab === 'properties' && <PropertiesTab doc={doc} />}
            {tab === 'rag' && <RagStatusTab doc={doc} />}
            {tab === 'versions' && <VersionsTab canAdd={canAddVersion} doc={doc} />}
            {tab === 'access' && (
              <DocumentAccessTab documentId={doc.id} spaceId={doc.space_id} />
            )}

            <footer className="doc-drawer-foot">
              <button
                className="secondary-button"
                onClick={() => void doDownload()}
                type="button"
              >
                <Download size={13} />
                <span>{t('documents.download')}</span>
              </button>
            </footer>
          </>
        )}

        {!doc && (
          <div className="state-panel doc-drawer-missing">
            {t('knowledge.detail.notFoundHint')}
          </div>
        )}
      </aside>
    </div>
  )
}

function PropertiesTab({ doc }: { doc: DocumentSummary }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  return (
    <div className="doc-drawer-body">
      <DetailRow
        label={t('knowledge.detail.propName')}
        value={doc.name}
      />
      <DetailRow
        label={t('knowledge.detail.propUploader')}
        value={doc.uploader_display_name || t('documents.unknownUploader')}
      />
      <DetailRow
        label={t('knowledge.detail.propSource')}
        value={
          <span className={`chip chip-inline chip-source-${doc.source_kind}`}>
            <Database size={10} />
            {t(`documents.source.${doc.source_kind}`)} · {doc.source_name}
          </span>
        }
      />
      {(doc.version_count ?? 0) > 1 && (
        <DetailRow
          label={t('knowledge.versions.propVersion')}
          value={t('knowledge.versions.currentOf', {
            number: doc.version_number,
            count: doc.version_count,
          })}
        />
      )}
      <DetailRow
        label={t('knowledge.detail.propSize')}
        value={formatBytes(doc.size_bytes)}
      />
      <DetailRow
        label={t('knowledge.detail.propMime')}
        value={doc.mime_type}
      />
      <DetailRow
        label={t('knowledge.detail.propCreatedAt')}
        value={formatDate(doc.created_at)}
      />
      <DetailRow
        label={t('knowledge.detail.propUpdatedAt')}
        value={formatDate(doc.updated_at)}
      />
      <DetailRow
        label={t('knowledge.detail.propChecksum')}
        value={
          doc.checksum_sha256 ? (
            <code className="mono">{doc.checksum_sha256}</code>
          ) : (
            '—'
          )
        }
      />
      <DetailRow
        label={t('knowledge.detail.propStorageKey')}
        value={
          doc.storage_key ? (
            <code className="mono">{doc.storage_key}</code>
          ) : (
            '—'
          )
        }
      />
      <DetailRow
        label={t('knowledge.detail.propSyncState')}
        value={
          <span className={`chip chip-inline chip-sync-${doc.sync_state}`}>
            {t(`knowledge.detail.sync.${doc.sync_state}`)}
          </span>
        }
      />
    </div>
  )
}

/**
 * The RAG status tab is a placeholder — the actual indexer lands in a
 * later slice. What we can show today is what the row already knows
 * about its own indexing lifecycle (all ``not_indexed`` right now) and
 * an explanation of what will happen when the RAG worker runs.
 */
function RagStatusTab({ doc }: { doc: DocumentSummary }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const state = doc.indexing_state
  const stateIcon: Record<IndexingState, React.ReactNode> = {
    not_indexed: <Clock3 size={13} />,
    indexing: <Cpu size={13} className="spin" />,
    indexed: <Sparkles size={13} />,
    failed: <Bot size={13} />,
  }
  return (
    <div className="doc-drawer-body">
      <div className={`rag-state-hero rag-state-${state}`}>
        <div className="rag-state-icon">{stateIcon[state]}</div>
        <div className="rag-state-copy">
          <strong>{t(`knowledge.indexing.${state}`)}</strong>
          <small>{t(`knowledge.detail.rag.${state}`)}</small>
        </div>
      </div>

      <DetailRow
        label={t('knowledge.detail.propChunks')}
        value={String(doc.chunk_count)}
      />
      <DetailRow
        label={t('knowledge.detail.propLastIndexed')}
        value={
          doc.last_indexed_at
            ? formatDate(doc.last_indexed_at)
            : '—'
        }
      />
      {doc.indexing_error && (
        <DetailRow
          label={t('knowledge.detail.propIndexingError')}
          value={<code className="mono">{doc.indexing_error}</code>}
        />
      )}
      <p className="doc-drawer-note">{t('knowledge.detail.rag.placeholder')}</p>
    </div>
  )
}

/**
 * Every file the document has held, newest first, and the one action that adds to it:
 * "upload a new version". Nothing is replaced: an earlier version stays downloadable and its
 * text stays searchable, labelled with its version number.
 */
export function VersionsTab({ doc, canAdd }: { doc: DocumentSummary; canAdd: boolean }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const { credentials } = useAuth()
  const { reloadPage, reloadStats } = useDocuments()
  const [versions, setVersions] = useState<DocumentVersion[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState('')
  // After submitting, whether the new version is the document's file now or is waiting.
  const [submitted, setSubmitted] = useState<'current' | 'waiting' | null>(null)
  const [progress, setProgress] = useState<number | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    if (!credentials) return
    try {
      const res = await listDocumentVersions(credentials, doc.space_id, doc.id)
      setVersions(res.versions)
      setError(null)
    } catch (err) {
      setError(messageOf(err))
    }
  }, [credentials, doc.id, doc.space_id])

  useEffect(() => {
    void load()
  }, [load, doc.version_number])

  const onPick = async (file: File | undefined) => {
    if (!file || !credentials) return
    setError(null)
    setSubmitted(null)
    setProgress(0)
    try {
      const result = await uploadNewVersion(credentials, doc.space_id, doc.id, file, {
        note,
        onProgress: (p) => setProgress(p.total ? p.loaded / p.total : 0),
      })
      setSubmitted(result.pending_version_number != null ? 'waiting' : 'current')
      setNote('')
      await Promise.all([load(), reloadPage(), reloadStats()])
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setProgress(null)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  const saveVersion = async (v: DocumentVersion) => {
    if (!credentials) return
    try {
      const { blob, filename } = await downloadDocumentVersion(
        credentials,
        doc.space_id,
        doc.id,
        v.version_number,
      )
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename || v.filename
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (err) {
      setError(messageOf(err))
    }
  }

  return (
    <div className="doc-drawer-body">
      {canAdd && (
        <div className="doc-version-upload">
          <p className="doc-drawer-note">{t('knowledge.versions.explain')}</p>
          <input
            aria-label={t('knowledge.versions.noteLabel')}
            maxLength={500}
            onChange={(e) => setNote(e.target.value)}
            placeholder={t('knowledge.versions.notePlaceholder')}
            type="text"
            value={note}
          />
          <input
            hidden
            onChange={(e) => void onPick(e.target.files?.[0])}
            ref={fileInput}
            type="file"
          />
          {doc.pending_version_number != null && (
            <small className="settings-hint">
              {t('knowledge.versions.oneWaiting', { number: doc.pending_version_number })}
            </small>
          )}
          <button
            className="secondary-button"
            disabled={progress !== null || doc.pending_version_number != null}
            onClick={() => fileInput.current?.click()}
            type="button"
          >
            <Upload size={13} />
            <span>
              {progress === null
                ? t('knowledge.versions.upload')
                : t('knowledge.versions.uploading', { percent: Math.round(progress * 100) })}
            </span>
          </button>
        </div>
      )}
      {error && (
        <p className="doc-version-error" role="alert">
          {error}
        </p>
      )}
      {submitted === 'waiting' && (
        <p className="doc-drawer-note" role="status">
          {t('knowledge.versions.submittedWaiting')}
        </p>
      )}
      {versions === null && !error && <p className="doc-drawer-note">{t('knowledge.versions.loading')}</p>}
      {versions !== null && (
        <ul className="doc-version-list">
          {versions.map((v) => (
            <li
              className={v.is_current ? 'doc-version-item current' : 'doc-version-item'}
              key={v.version_id}
            >
              <span className="doc-version-head">
                <strong>{t('knowledge.versions.number', { number: v.version_number })}</strong>
                {v.is_current && (
                  <span className="chip chip-inline">{t('knowledge.versions.current')}</span>
                )}
                {v.review_state === 'pending' && (
                  <span className="badge badge-pending">{t('knowledge.versions.pending')}</span>
                )}
                {v.review_state === 'rejected' && (
                  <span className="badge badge-pending">{t('knowledge.versions.rejected')}</span>
                )}
                <span title={v.filename}>{v.filename}</span>
              </span>
              <button
                aria-label={t('knowledge.versions.download', { number: v.version_number })}
                className="secondary-button icon-button"
                onClick={() => void saveVersion(v)}
                title={t('knowledge.versions.download', { number: v.version_number })}
                type="button"
              >
                <Download size={13} />
              </button>
              <span className="doc-version-meta">
                {formatDate(v.created_at)}
                <span className="dot-sep">·</span>
                {v.created_by_name || t('documents.unknownUploader')}
                <span className="dot-sep">·</span>
                {formatBytes(v.size_bytes)}
                <span className="dot-sep">·</span>
                <code className="mono" title={v.checksum_sha256}>
                  {v.checksum_sha256.slice(0, 10)}
                </code>
              </span>
              {v.note && <span className="doc-version-note">{v.note}</span>}
              {v.review_state === 'rejected' && v.review_note && (
                <span className="doc-version-note">
                  {t('knowledge.versions.rejectedBecause', { reason: v.review_note })}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function messageOf(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

function DetailRow({
  label,
  value,
}: {
  label: string
  value: React.ReactNode
}) {
  return (
    <div className="doc-drawer-row">
      <small>{label}</small>
      <span>{value}</span>
    </div>
  )
}
