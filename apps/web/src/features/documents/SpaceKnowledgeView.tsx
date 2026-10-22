import {
  ArrowLeft,
  Building2,
  ChevronLeft,
  ChevronRight,
  Cloud,
  Database,
  Filter,
  HardDrive,
  MessageSquare,
  RefreshCw,
  Search,
  ShieldCheck,
  SortAsc,
  ShieldAlert,
  SortDesc,
  Trash2,
  Upload,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useAuthModal } from '../auth/AuthModalContext'
import { useSpaces } from '../spaces/SpacesContext'
import type { SpaceSummary } from '../spaces/types'

import { AddFeishuDialog } from '../integrations/AddFeishuDialog'

import { DocumentDetailDrawer } from './DocumentDetailDrawer'
import { formatBytes } from './DocumentRow'
import { ExceptionBadges } from './ExceptionBadges'
import { DocumentsProvider, useDocuments } from './DocumentsContext'
import { isReadOnlySpace } from './permissions'
import { UploadDropZone } from './UploadDropZone'
import { UploadReviewSection } from './UploadReview'
import type { DocumentFileType, DocumentSourceKind, DocumentSummary } from './types'
import { useFormatDate } from '../../i18n/formatDate'
import { localizedName } from '../../i18n/localizedName'

const GUEST_SANDBOX_SLUG = 'guest-sandbox'

/**
 * Full-page browser for one space's document library.
 *
 * Layout:
 *
 *   ┌ breadcrumb + back button                              ─┐
 *   │ header (space name + facets)                            │
 *   │ stats bar (total / size / by_source / by_indexing)      │
 *   │ upload dropzone (collapsible)                           │
 *   │ toolbar (search / source filter / sort / batch delete)  │
 *   │ table (paginated rows)                                  │
 *   │ pager (X of Y · prev / next)                            │
 *   └─────────────────────────────────────────────────────────┘
 *
 * A row-click opens ``DocumentDetailDrawer`` in place with a "properties"
 * tab and a "RAG status" tab (placeholder until the indexer ships).
 */
export function SpaceKnowledgeView({
  spaceId,
  onBack,
}: {
  spaceId: string
  onBack: () => void
}) {
  const { state } = useSpaces()
  const space = useMemo<SpaceSummary | null>(() => {
    if (state.kind !== 'ready') return null
    return state.items.find((s) => s.id === spaceId) ?? null
  }, [state, spaceId])

  return (
    <DocumentsProvider spaceId={spaceId}>
      <SpaceKnowledgeViewInner space={space} onBack={onBack} />
    </DocumentsProvider>
  )
}

function SpaceKnowledgeViewInner({
  space,
  onBack,
}: {
  space: SpaceSummary | null
  onBack: () => void
}) {
  const { t } = useTranslation()
  const { page, reloadPage, reloadStats, stats } = useDocuments()
  const { reload: reloadSpaces } = useSpaces()
  const reloadAll = () => Promise.all([reloadPage(), reloadStats(), reloadSpaces()])

  // Kick off the first page fetch on mount — the provider only enters
  // "browser mode" once we call ``reloadPage`` at least once.
  useEffect(() => {
    void reloadPage()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const [localOpen, setLocalOpen] = useState(false)
  const [feishuOpen, setFeishuOpen] = useState(false)
  const [detailId, setDetailId] = useState<string | null>(null)
  const { isGuest } = useAuth()
  const { openAuth } = useAuthModal()
  // Guests may only upload into the shared sandbox space.
  const guestCanUpload = isGuest && space?.slug === GUEST_SANDBOX_SLUG
  const needsLogin = isGuest && !guestCanUpload
  // A reader of the space is not offered adding or deleting: the server would refuse them.
  const readOnly = isReadOnlySpace(space, isGuest)
  // A new version is added by an editor or better. In a space that approves uploads an
  // editor's version waits for an owner or admin; the server decides which.
  const canAddVersion = !readOnly && !isGuest

  return (
    <>
      <div className="space-view-breadcrumb">
        <button className="link-button" onClick={onBack} type="button">
          <ArrowLeft size={13} />
          <span>{t('knowledge.view.back')}</span>
        </button>
        <span className="dot-sep">·</span>
        <span>{t('knowledge.view.crumbRoot')}</span>
        {space && <span className="dot-sep">›</span>}
        {space && <strong>{space.name}</strong>}
      </div>

      <section className="panel space-view-header">
        <div className="space-view-header-lockup">
          <span className={`space-view-mark preset-${space?.preset ?? 'none'}`}>
            {space?.name.charAt(0).toUpperCase() ?? '?'}
          </span>
          <div>
            <span className="section-label">
              <Database size={14} /> {t('knowledge.view.eyebrow')}
            </span>
            <h1>{space?.name ?? t('knowledge.view.unknownSpace')}</h1>
            <small className="space-view-slug">{space?.slug}</small>
          </div>
          {space?.is_owner && (
            <span className="badge badge-owner">
              <ShieldCheck size={12} /> {t('spaces.owner')}
            </span>
          )}
        </div>

        {space && (
          <div className="space-view-facets">
            {(space.shared_only_documents ?? 0) > 0 && (
              <span className="chip chip-inline">
                {t('spaces.sharedOnly', { count: space.shared_only_documents })}
              </span>
            )}
            <span className={`chip chip-inline chip-preset-${space.preset}`}>
              {t(`spaces.preset.${space.preset}`)}
            </span>
            <span className={`chip chip-inline chip-visibility-${space.visibility}`}>
              {t(`spaces.visibility.${space.visibility}`)}
            </span>
            <span className={`chip chip-inline chip-trust-${space.trust_level}`}>
              {t(`spaces.trust.${space.trust_level}`)}
            </span>
          </div>
        )}
      </section>

      <StatsBar />
      {space && (
        <UploadReviewSection
          onChanged={() => void reloadAll()}
          required={space.require_upload_approval === true}
          role={space.caller_role}
          spaceId={space.id}
        />
      )}
      {!readOnly && (
        <section className="panel space-view-sources">
          <div className="panel-heading">
            <div>
              <span className="section-label">
                <Upload size={14} /> {t('knowledge.view.sources.label')}
              </span>
              <p>{t('knowledge.view.sources.hint')}</p>
            </div>
          </div>
          <div className="source-option-row">
            <button
              className={localOpen ? 'source-option active' : 'source-option'}
              onClick={() => (needsLogin ? openAuth() : setLocalOpen((v) => !v))}
              aria-pressed={localOpen}
              type="button"
            >
              <span className="source-option-icon">
                <Upload size={17} />
              </span>
              <span className="source-option-body">
                <strong>{t('knowledge.view.sources.local')}</strong>
                <small>{t('knowledge.view.sources.localHint')}</small>
              </span>
            </button>
            <button
              className="source-option"
              onClick={() => (isGuest ? openAuth() : setFeishuOpen(true))}
              disabled={!space && !isGuest}
              type="button"
            >
              <span className="source-option-icon feishu">
                <Cloud size={17} />
              </span>
              <span className="source-option-body">
                <strong>{t('knowledge.view.sources.feishu')}</strong>
                <small>{t('knowledge.view.sources.feishuHint')}</small>
              </span>
            </button>
            <SoonSource icon={<MessageSquare size={17} />} label={t('knowledge.view.sources.wecom')} />
            <SoonSource icon={<Building2 size={17} />} label={t('knowledge.view.sources.dingtalk')} />
            <SoonSource icon={<HardDrive size={17} />} label={t('knowledge.view.sources.drive')} />
          </div>
          {guestCanUpload && <GuestSandboxNotice onSignIn={openAuth} />}
          {localOpen && <UploadDropZone />}
        </section>
      )}

      <section className="panel space-view-browser">
        <Toolbar />
        <FileTypeTags />
        <DocumentsTable
          canDelete={!readOnly}
          onSelectDetail={(id) => setDetailId(id)}
          selectedId={detailId}
        />
        <Pager />
        {stats.kind === 'ready' && page.kind === 'ready' && (
          <PageFooter total={page.total} statsTotal={stats.stats.total} />
        )}
      </section>

      {detailId && (
        <DocumentDetailDrawer
          canAddVersion={canAddVersion}
          documentId={detailId}
          onClose={() => setDetailId(null)}
        />
      )}

      {feishuOpen && space && (
        <AddFeishuDialog
          fixedSpaceId={space.id}
          autoSync
          onClose={() => setFeishuOpen(false)}
          onCreated={async () => {
            setFeishuOpen(false)
            await Promise.all([reloadPage(), reloadStats()])
          }}
        />
      )}
    </>
  )
}

/** Privacy warning shown to guests in the shared sandbox space. */
function GuestSandboxNotice({ onSignIn }: { onSignIn: () => void }) {
  const { t } = useTranslation()
  return (
    <div className="guest-sandbox-notice" role="alert">
      <ShieldAlert size={18} aria-hidden="true" />
      <div>
        <strong>{t('guestSandbox.title')}</strong>
        <p>{t('guestSandbox.body')}</p>
      </div>
      <button className="link-button" onClick={onSignIn} type="button">
        {t('guestSandbox.signIn')}
      </button>
    </div>
  )
}

/** A greyed-out "coming soon" source tile. */
function SoonSource({ icon, label }: { icon: React.ReactNode; label: string }) {
  const { t } = useTranslation()
  return (
    <button className="source-option soon" disabled type="button">
      <span className="source-option-icon">{icon}</span>
      <span className="source-option-body">
        <strong>{label}</strong>
        <small>{t('knowledge.view.sources.soon')}</small>
      </span>
    </button>
  )
}

// ---------------------------------------------------------------------------
// Stats bar
// ---------------------------------------------------------------------------

function StatsBar() {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const { stats, reloadStats } = useDocuments()

  const total = stats.kind === 'ready' ? stats.stats.total : null
  const size = stats.kind === 'ready' ? stats.stats.total_size_bytes : null
  const lastAt = stats.kind === 'ready' ? stats.stats.last_uploaded_at : null
  const bySource = stats.kind === 'ready' ? stats.stats.by_source : {}
  const byState = stats.kind === 'ready' ? stats.stats.by_indexing_state : {}

  return (
    <section className="panel space-view-stats">
      <div className="space-view-stats-cells">
        <StatBigCell
          label={t('knowledge.view.stats.total')}
          value={total === null ? '…' : String(total)}
        />
        <StatBigCell
          label={t('knowledge.view.stats.size')}
          value={size === null ? '…' : size > 0 ? formatBytes(size) : '—'}
        />
        <StatBigCell
          label={t('knowledge.view.stats.lastUpload')}
          value={
            lastAt ? formatDate(lastAt) : '—'
          }
        />
        <StatBigCell
          label={t('knowledge.view.stats.indexed')}
          value={
            stats.kind === 'ready'
              ? `${byState['indexed'] ?? 0} / ${total ?? 0}`
              : '…'
          }
        />
      </div>
      <div className="space-view-stats-breakdown">
        <div className="space-view-stats-group">
          <small>{t('knowledge.view.stats.bySource')}</small>
          <div className="space-view-stats-chips">
            {Object.keys(bySource).length === 0 && (
              <span className="chip chip-inline">—</span>
            )}
            {Object.entries(bySource).map(([kind, count]) => (
              <span
                key={kind}
                className={`chip chip-inline chip-source-${kind}`}
              >
                {t(`documents.source.${kind}`)} · {count}
              </span>
            ))}
          </div>
        </div>
        <div className="space-view-stats-group">
          <small>{t('knowledge.view.stats.byIndexing')}</small>
          <div className="space-view-stats-chips">
            {Object.keys(byState).length === 0 && (
              <span className="chip chip-inline">—</span>
            )}
            {Object.entries(byState).map(([state, count]) => (
              <span
                key={state}
                className={`chip chip-inline chip-indexing-${state}`}
              >
                {t(`knowledge.indexing.${state}`)} · {count}
              </span>
            ))}
          </div>
        </div>
        <button
          className="icon-button space-view-stats-refresh"
          disabled={stats.kind === 'loading'}
          onClick={() => void reloadStats()}
          title={t('documents.refresh')}
          type="button"
        >
          <RefreshCw
            size={13}
            className={stats.kind === 'loading' ? 'spin' : undefined}
          />
        </button>
      </div>
    </section>
  )
}

function StatBigCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-view-stat-cell">
      <small>{label}</small>
      <strong>{value}</strong>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Toolbar (search / filter / sort)
// ---------------------------------------------------------------------------

const SOURCE_OPTIONS: Array<{ value: '' | DocumentSourceKind; key: string }> = [
  { value: '', key: 'all' },
  { value: 'local', key: 'local' },
  { value: 'feishu', key: 'feishu' },
  { value: 'oss_aliyun', key: 'oss_aliyun' },
  { value: 's3_aws', key: 's3_aws' },
]

const FILE_TYPE_ORDER: DocumentFileType[] = [
  'image',
  'pdf',
  'word',
  'spreadsheet',
  'presentation',
  'text',
  'other',
]

/**
 * File-type tags, like the kind filter of a file manager. One tag is on at a
 * time; "All" clears it. Only kinds the space actually holds are offered (plus
 * the selected one, so it can always be switched off).
 */
export function FileTypeTags() {
  const { t } = useTranslation()
  const { params, setParams, stats } = useDocuments()
  const counts = stats.kind === 'ready' ? stats.stats.by_mime_group : {}
  const selected = params.file_type
  const kinds = FILE_TYPE_ORDER.filter((k) => (counts[k] ?? 0) > 0 || k === selected)
  if (kinds.length === 0 && !selected) return null

  const choose = (next: DocumentFileType | undefined) =>
    setParams((prev) => ({ ...prev, file_type: next, offset: 0 }))

  return (
    <div
      aria-label={t('knowledge.view.fileType.label')}
      className="file-type-tags"
      role="group"
    >
      <button
        aria-pressed={!selected}
        className={selected ? 'file-type-tag' : 'file-type-tag is-active'}
        onClick={() => choose(undefined)}
        type="button"
      >
        {t('knowledge.view.fileType.all')}
      </button>
      {kinds.map((kind) => (
        <button
          aria-pressed={selected === kind}
          className={selected === kind ? 'file-type-tag is-active' : 'file-type-tag'}
          key={kind}
          onClick={() => choose(selected === kind ? undefined : kind)}
          type="button"
        >
          {t(`knowledge.view.fileType.${kind}`)}
          {counts[kind] !== undefined && <small>{counts[kind]}</small>}
        </button>
      ))}
    </div>
  )
}

function Toolbar() {
  const { t } = useTranslation()
  const { params, setParams, reloadPage, page } = useDocuments()

  // Debounce ``q`` so we don't hammer the API on every keystroke.
  const [draftQ, setDraftQ] = useState<string>(params.q ?? '')
  const debounceRef = useRef<number | null>(null)
  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    debounceRef.current = window.setTimeout(() => {
      setParams((prev) => ({ ...prev, q: draftQ || undefined, offset: 0 }))
    }, 300)
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftQ])

  const flipOrder = () =>
    setParams((prev) => ({
      ...prev,
      order: prev.order === 'asc' ? 'desc' : 'asc',
      offset: 0,
    }))

  return (
    <div className="space-view-toolbar">
      <label className="space-view-search">
        <Search size={13} />
        <input
          onChange={(e) => setDraftQ(e.target.value)}
          placeholder={t('knowledge.view.search')}
          type="search"
          value={draftQ}
        />
      </label>
      <label className="space-view-filter">
        <Filter size={13} />
        <select
          onChange={(e) => {
            const val = e.target.value as '' | DocumentSourceKind
            setParams((prev) => ({
              ...prev,
              source_kind: val === '' ? undefined : val,
              offset: 0,
            }))
          }}
          value={params.source_kind ?? ''}
        >
          {SOURCE_OPTIONS.map((opt) => (
            <option key={opt.key} value={opt.value}>
              {t(`knowledge.view.filter.${opt.key}`)}
            </option>
          ))}
        </select>
      </label>
      <label className="space-view-sort">
        <select
          onChange={(e) =>
            setParams((prev) => ({
              ...prev,
              order_by: e.target.value as typeof params.order_by,
              offset: 0,
            }))
          }
          value={params.order_by ?? 'created_at'}
        >
          <option value="created_at">{t('knowledge.view.sort.createdAt')}</option>
          <option value="updated_at">{t('knowledge.view.sort.updatedAt')}</option>
          <option value="name">{t('knowledge.view.sort.name')}</option>
          <option value="size_bytes">{t('knowledge.view.sort.size')}</option>
        </select>
        <button
          className="icon-button"
          onClick={flipOrder}
          title={
            params.order === 'asc'
              ? t('knowledge.view.sort.asc')
              : t('knowledge.view.sort.desc')
          }
          type="button"
        >
          {params.order === 'asc' ? (
            <SortAsc size={13} />
          ) : (
            <SortDesc size={13} />
          )}
        </button>
      </label>
      <div className="space-view-toolbar-spacer" />
      <button
        className="icon-button"
        disabled={page.kind === 'loading'}
        onClick={() => void reloadPage()}
        title={t('documents.refresh')}
        type="button"
      >
        <RefreshCw
          size={13}
          className={page.kind === 'loading' ? 'spin' : undefined}
        />
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

function DocumentsTable({
  canDelete = true,
  onSelectDetail,
  selectedId,
}: {
  /** False for a reader of the space: the delete buttons are not shown. */
  canDelete?: boolean
  onSelectDetail: (id: string) => void
  selectedId: string | null
}) {
  const { t, i18n } = useTranslation()
  const formatDate = useFormatDate()
  const { page, remove } = useDocuments()
  const { isGuest } = useAuth()

  if (page.kind === 'idle' || page.kind === 'loading') {
    return (
      <div className="state-panel space-view-loading">
        {t('documents.loading')}
      </div>
    )
  }
  if (page.kind === 'error') {
    return (
      <div className="state-panel error-state">
        {t('documents.loadError')}: {page.message}
      </div>
    )
  }
  if (page.items.length === 0) {
    return <div className="state-panel">{t('knowledge.view.empty')}</div>
  }

  const doRemove = async (doc: DocumentSummary) => {
    if (!window.confirm(t('documents.confirmDelete', { name: doc.name }))) return
    try {
      await remove(doc.id)
    } catch (err) {
      window.alert(
        err instanceof ApiError
          ? err.detail || `HTTP ${err.status}`
          : String(err),
      )
    }
  }

  return (
    <table className="space-view-table">
      <thead>
        <tr>
          <th>{t('knowledge.view.table.name')}</th>
          <th>{t('knowledge.view.table.source')}</th>
          <th>{t('knowledge.view.table.size')}</th>
          <th>{t('knowledge.view.table.indexing')}</th>
          <th>{t('knowledge.view.table.uploader')}</th>
          <th>{t('knowledge.view.table.created')}</th>
          <th aria-label="actions" />
        </tr>
      </thead>
      <tbody>
        {page.items.map((doc) => (
          <tr
            className={selectedId === doc.id ? 'active' : ''}
            key={doc.id}
            onClick={() => onSelectDetail(doc.id)}
          >
            <td>
              <strong>{doc.name}</strong>
              {doc.pending_version_number != null && (
                <span className="badge badge-pending">
                  {t('knowledge.versions.waitingBadge', { number: doc.pending_version_number })}
                </span>
              )}
              {(doc.version_count ?? 0) > 1 && (
                <span
                  className="chip chip-inline"
                  title={t('knowledge.versions.versionsTitle', { count: doc.version_count })}
                >
                  {t('knowledge.versions.badge', { number: doc.version_number })}
                </span>
              )}
              <ExceptionBadges allow={doc.allow_rules} deny={doc.deny_rules} />
              {isGuest && doc.is_mine && (
                <span className="chip chip-inline">{t('guestSandbox.mine')}</span>
              )}
            </td>
            <td>
              <span className={`chip chip-inline chip-source-${doc.source_kind}`}>
                {t(`documents.source.${doc.source_kind}`)}
              </span>
            </td>
            <td>{formatBytes(doc.size_bytes)}</td>
            <td>
              <span
                className={`chip chip-inline chip-indexing-${doc.indexing_state}`}
              >
                {t(`knowledge.indexing.${doc.indexing_state}`)}
              </span>
            </td>
            <td>
              {doc.uploader_display_name
                ? isGuest
                  ? localizedName(doc.uploader_display_name, i18n.language)
                  : doc.uploader_display_name
                : '—'}
            </td>
            <td>{formatDate(doc.created_at)}</td>
            <td className="space-view-row-actions">
              {canDelete && (!isGuest || doc.is_mine) && (
                <button
                  className="icon-button danger"
                  onClick={(e) => {
                    e.stopPropagation()
                    void doRemove(doc)
                  }}
                  title={t('documents.delete')}
                  type="button"
                >
                  <Trash2 size={13} />
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ---------------------------------------------------------------------------
// Pager
// ---------------------------------------------------------------------------

function Pager() {
  const { t } = useTranslation()
  const { page, setParams } = useDocuments()

  if (page.kind !== 'ready') return null

  const { limit, offset, total } = page
  const start = total === 0 ? 0 : offset + 1
  const end = Math.min(total, offset + limit)
  const canPrev = offset > 0
  const canNext = offset + limit < total

  const go = (nextOffset: number) =>
    setParams((prev) => ({ ...prev, offset: Math.max(0, nextOffset) }))

  return (
    <div className="space-view-pager">
      <span className="space-view-pager-range">
        {t('knowledge.view.pager.range', { start, end, total })}
      </span>
      <label className="space-view-pager-size">
        <span>{t('knowledge.view.pager.perPage')}</span>
        <select
          onChange={(e) =>
            setParams((prev) => ({
              ...prev,
              limit: Number(e.target.value),
              offset: 0,
            }))
          }
          value={limit}
        >
          <option value={10}>10</option>
          <option value={25}>25</option>
          <option value={50}>50</option>
          <option value={100}>100</option>
        </select>
      </label>
      <div className="space-view-pager-actions">
        <button
          className="icon-button"
          disabled={!canPrev}
          onClick={() => go(offset - limit)}
          type="button"
        >
          <ChevronLeft size={13} />
        </button>
        <button
          className="icon-button"
          disabled={!canNext}
          onClick={() => go(offset + limit)}
          type="button"
        >
          <ChevronRight size={13} />
        </button>
      </div>
    </div>
  )
}

function PageFooter({
  total,
  statsTotal,
}: {
  total: number
  statsTotal: number
}) {
  const { t } = useTranslation()
  if (total === statsTotal) return null
  return (
    <div className="space-view-footer-hint">
      {t('knowledge.view.filteredHint', { filtered: total, total: statsTotal })}
    </div>
  )
}
