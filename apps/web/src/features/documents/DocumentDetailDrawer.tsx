import {
  Bot,
  Clock3,
  Cpu,
  Database,
  Download,
  FileText,
  Info,
  Sparkles,
  X,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import { formatBytes } from './DocumentRow'
import { useDocuments } from './DocumentsContext'
import type { DocumentSummary, IndexingState } from './types'
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
}: {
  documentId: string
  onClose: () => void
}) {
  const { t } = useTranslation()
  const { page, download } = useDocuments()
  const [tab, setTab] = useState<'properties' | 'rag'>('properties')

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
            </div>

            {tab === 'properties' && <PropertiesTab doc={doc} />}
            {tab === 'rag' && <RagStatusTab doc={doc} />}

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
