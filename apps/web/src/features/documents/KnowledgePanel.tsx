import {
  ChevronRight,
  Database,
  EyeOff,
  Layers,
  Plus,
  ShieldCheck,
} from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/AuthContext'
import { useAuthModal } from '../auth/AuthModalContext'
import { AccessRequestInbox } from '../accessRequests/AccessRequestInbox'
import { CreateSpaceDialog } from '../spaces/CreateSpaceDialog'
import { useSpaces } from '../spaces/SpacesContext'
import type { SpaceSummary } from '../spaces/types'

import { formatBytes } from './DocumentRow'
import { DocumentsProvider, useDocuments } from './DocumentsContext'
import { useFormatDate } from '../../i18n/formatDate'

// Keep in sync with backend PERSONAL_MAX_SPACES (services/spaces.py).
const PERSONAL_MAX_SPACES = 20

/**
 * Full-page Knowledge view.
 *
 * The page lists **every space the caller can access** as a card
 * grid; each card is one knowledge base. Cards themselves stay
 * summary-only — total documents, total size, last upload, source
 * distribution. Deep browsing (per-file search, filter, batch delete)
 * lives on the ``SpaceKnowledgeView`` detail page reached via the
 * "查看全部" button.
 *
 * Each card mounts its own ``DocumentsProvider`` — the provider only
 * pulls the cheap ``/stats`` endpoint on mount, so N cards on screen
 * cost N stats calls, not N full-list scans.
 */
export function KnowledgePanel({
  onOpenSpace,
}: {
  onOpenSpace: (spaceId: string) => void
}) {
  const { t } = useTranslation()
  const { state } = useSpaces()
  const { isGuest, credentials } = useAuth()
  const { openAuth } = useAuthModal()
  const [createOpen, setCreateOpen] = useState(false)

  const spaceCount = state.kind === 'ready' ? state.items.length : 0
  const isPersonal = credentials?.tier === 'personal'
  const atLimit = isPersonal && spaceCount >= PERSONAL_MAX_SPACES

  const onNewSpace = () => {
    if (isGuest) {
      openAuth()
      return
    }
    if (atLimit) return
    setCreateOpen(true)
  }

  return (
    <>
      <AccessRequestInbox />
      <section className="panel knowledge-header">
        <div className="knowledge-header-lockup">
          <span className="knowledge-header-mark">
            <Database size={18} />
          </span>
          <div>
            <span className="section-label">
              <Layers size={14} /> {t('knowledge.pageEyebrow')}
            </span>
            <h1>{t('knowledge.pageTitle')}</h1>
            <p>{t('knowledge.pageDescription')}</p>
          </div>
        </div>
        <div className="knowledge-header-actions">
          <button
            className="primary-button"
            onClick={onNewSpace}
            disabled={atLimit}
            title={atLimit ? t('knowledge.spaceLimitReached', { max: PERSONAL_MAX_SPACES }) : undefined}
            type="button"
          >
            <Plus size={15} />
            <span>{t('knowledge.newSpace')}</span>
          </button>
          {isPersonal && (
            <small className="knowledge-space-count">
              {t('knowledge.spaceCount', { count: spaceCount, max: PERSONAL_MAX_SPACES })}
            </small>
          )}
        </div>
      </section>

      <CreateSpaceDialog open={createOpen} onClose={() => setCreateOpen(false)} />

      {state.kind === 'loading' && (
        <section className="panel">
          <div className="state-panel">{t('spaces.loading')}</div>
        </section>
      )}

      {state.kind === 'error' && (
        <section className="panel">
          <div className="state-panel error-state">
            {t('spaces.loadError')}: {state.message}
          </div>
        </section>
      )}

      {state.kind === 'ready' && state.items.length === 0 && (
        <section className="panel knowledge-empty">
          <EyeOff size={24} />
          <div>
            <strong>{t('knowledge.emptyTitle')}</strong>
            <p>{t('knowledge.emptyDescription')}</p>
          </div>
        </section>
      )}

      {state.kind === 'ready' && state.items.length > 0 && (
        <div className="knowledge-grid">
          {state.items.map((s) => (
            <DocumentsProvider spaceId={s.id} key={s.id}>
              <SpaceKnowledgeCard space={s} onOpen={() => onOpenSpace(s.id)} />
            </DocumentsProvider>
          ))}
        </div>
      )}
    </>
  )
}

/**
 * One card in the grid. Rendered inside its own ``DocumentsProvider`` so
 * ``useDocuments().stats`` is scoped to exactly this space. Cards do not
 * expose upload / expand any more — that friction sits in the detail
 * page where the user has a wider viewport and a full toolbar.
 */
function SpaceKnowledgeCard({
  space,
  onOpen,
}: {
  space: SpaceSummary
  onOpen: () => void
}) {
  const { t } = useTranslation()
  const { stats } = useDocuments()

  return (
    <article className="space-card">
      <header className="space-card-head">
        <span className={`space-card-mark preset-${space.preset}`}>
          {space.name.charAt(0).toUpperCase() || '?'}
        </span>
        <div className="space-card-title">
          <strong>{space.name}</strong>
          <small>{space.slug}</small>
        </div>
      </header>

      <div className="space-card-facets">
        <span className={`chip chip-inline chip-preset-${space.preset}`}>
          {t(`spaces.preset.${space.preset}`)}
        </span>
        <span className={`chip chip-inline chip-visibility-${space.visibility}`}>
          {t(`spaces.visibility.${space.visibility}`)}
        </span>
        <span className={`chip chip-inline chip-trust-${space.trust_level}`}>
          {t(`spaces.trust.${space.trust_level}`)}
        </span>
        {space.is_owner && (
          <span className="badge badge-owner" title={t('spaces.owner')}>
            <ShieldCheck size={12} /> {t('spaces.owner')}
          </span>
        )}
        {(space.shared_only_documents ?? 0) > 0 && (
          <span className="chip chip-inline">
            {t('spaces.sharedOnly', { count: space.shared_only_documents })}
          </span>
        )}
      </div>
      <StatsBlock />

      <button
        className="secondary-button space-card-open"
        onClick={onOpen}
        type="button"
      >
        <span>{t('knowledge.card.openAll')}</span>
        <ChevronRight size={14} />
      </button>
    </article>
  )
}

function StatsBlock() {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const { stats } = useDocuments()

  if (stats.kind === 'loading' || stats.kind === 'idle') {
    return (
      <div className="space-card-stats-grid">
        <StatCell label={t('knowledge.card.docCount')} value="…" />
        <StatCell label={t('knowledge.card.sizeTotal')} value="…" />
        <StatCell wide label={t('knowledge.card.lastUpload')} value="…" />
      </div>
    )
  }
  if (stats.kind === 'error') {
    return (
      <div className="space-card-error">
        {t('documents.loadError')}: {stats.message}
      </div>
    )
  }
  const s = stats.stats
  return (
    <div className="space-card-stats-grid">
      <StatCell
        label={t('knowledge.card.docCount')}
        value={String(s.total)}
      />
      <StatCell
        label={t('knowledge.card.sizeTotal')}
        value={s.total_size_bytes > 0 ? formatBytes(s.total_size_bytes) : '—'}
      />
      <StatCell
        wide
        label={t('knowledge.card.lastUpload')}
        value={
          s.last_uploaded_at
            ? // "Oct 9, 2026" / "2026年10月9日": short, unambiguous, localised.
              formatDate(s.last_uploaded_at, 'date')
            : '—'
        }
      />
    </div>
  )
}

function StatCell({
  label,
  value,
  wide = false,
}: {
  label: string
  value: string
  wide?: boolean
}) {
  return (
    <div className={wide ? 'space-card-stat-cell space-card-stat-cell-wide' : 'space-card-stat-cell'}>
      <small>{label}</small>
      <strong>{value}</strong>
    </div>
  )
}
