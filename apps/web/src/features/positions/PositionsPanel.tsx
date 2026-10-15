import {
  Archive,
  Check,
  ChevronRight,
  RefreshCw,
  Trash2,
  UserPlus,
  Users,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import { AssignOccupantDialog, CreatePositionDialog } from './dialogs'
import { usePositions } from './PositionsContext'
import type { PositionListItem } from './types'

/**
 * Positions tab: left = list of positions, right = detail with occupants
 * + assign action. Kept intentionally flat (no tree) — reports_to is shown
 * as an inline "→ 上级：..." row so the reporting line is legible without
 * a full org chart yet.
 */
export function PositionsPanel() {
  const { t } = useTranslation()
  const {
    positions,
    reload,
    includeArchived,
    setIncludeArchived,
    selectedId,
    selectPosition,
  } = usePositions()
  const [createOpen, setCreateOpen] = useState(false)

  return (
    <section className="org-tab-body">
      <header className="org-tab-header">
        <div>
          <p>{t('positions.description')}</p>
        </div>
        <div className="panel-actions">
          <label className="toggle-row">
            <input
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
              type="checkbox"
            />
            <span>{t('positions.showArchived')}</span>
          </label>
          <button
            className="secondary-button icon-button"
            disabled={positions.kind === 'loading'}
            onClick={() => void reload()}
            type="button"
          >
            <RefreshCw
              size={14}
              className={positions.kind === 'loading' ? 'spin' : undefined}
            />
            <span>{t('positions.refresh')}</span>
          </button>
          <button
            className="primary-button"
            onClick={() => setCreateOpen(true)}
            type="button"
          >
            <span>{t('positions.new')}</span>
          </button>
        </div>
      </header>

      <div className="org-workspace">
        <PositionListColumn />
        <PositionDetailColumn />
      </div>

      <CreatePositionDialog open={createOpen} onClose={() => setCreateOpen(false)} />

      {positions.kind === 'error' && (
        <div className="state-panel error-state" style={{ margin: '0 24px 24px' }}>
          {t('positions.loadError')}: {positions.message}
        </div>
      )}
      {positions.kind === 'ready' && positions.items.length === 0 && !selectedId && (
        <div className="state-panel" style={{ margin: '0 24px 24px' }}>
          {t('positions.empty')}
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Left column — position list
// ---------------------------------------------------------------------------

function PositionListColumn() {
  const { t } = useTranslation()
  const { positions, selectedId, selectPosition } = usePositions()

  const items = positions.kind === 'ready' ? positions.items : []

  return (
    <aside className="org-tree">
      {positions.kind === 'loading' && (
        <div className="chat-sidebar-hint">{t('positions.loading')}</div>
      )}
      {items.map((p) => (
        <PositionRow
          key={p.id}
          position={p}
          isActive={p.id === selectedId}
          onSelect={() => selectPosition(p.id)}
        />
      ))}
    </aside>
  )
}

function PositionRow({
  position,
  isActive,
  onSelect,
}: {
  position: PositionListItem
  isActive: boolean
  onSelect: () => void
}) {
  return (
    <div className={isActive ? 'org-tree-row active' : 'org-tree-row'}>
      <span className="org-tree-caret-placeholder" />
      <button className="org-tree-label" onClick={onSelect} type="button">
        <strong>
          {position.name}
          {position.status === 'archived' && (
            <span className="badge badge-pending" style={{ marginLeft: 6 }}>
              archived
            </span>
          )}
        </strong>
        <small>
          {position.headcount_budget === 0
            ? `${position.current_occupants} · ∞`
            : `${position.current_occupants} / ${position.headcount_budget}`}
          {position.grade ? ` · ${position.grade}` : ''}
          {position.code ? ` · ${position.code}` : ''}
        </small>
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Right column — position detail
// ---------------------------------------------------------------------------

function PositionDetailColumn() {
  const { t } = useTranslation()
  const {
    positions,
    selectedId,
    occupants,
    reloadOccupants,
    endTenure,
    setPrimary,
    archivePosition,
    selectPosition,
  } = usePositions()

  const [assignOpen, setAssignOpen] = useState(false)

  const selected = useMemo(() => {
    if (positions.kind !== 'ready') return undefined
    return positions.items.find((p) => p.id === selectedId)
  }, [positions, selectedId])

  const reportsToName = useMemo(() => {
    if (positions.kind !== 'ready' || !selected?.reports_to_position_id) return null
    return (
      positions.items.find((p) => p.id === selected.reports_to_position_id)?.name ??
      null
    )
  }, [positions, selected])

  if (!selected) {
    return (
      <section className="org-detail">
        <div className="state-panel">{t('positions.pickOne')}</div>
      </section>
    )
  }

  const doArchive = async () => {
    if (!window.confirm(t('positions.confirmArchive', { name: selected.name }))) return
    try {
      await archivePosition(selected.id)
      selectPosition(null)
    } catch (err) {
      window.alert(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    }
  }

  return (
    <section className="org-detail">
      <header className="org-detail-header">
        <div className="org-detail-title">
          <h2>{selected.name}</h2>
          {selected.code && <small className="space-overview-slug">{selected.code}</small>}
        </div>
        <div className="org-detail-actions">
          <button
            className="secondary-button icon-button danger"
            disabled={selected.status === 'archived'}
            onClick={() => void doArchive()}
            type="button"
          >
            <Archive size={13} />
            <span>{t('positions.archive')}</span>
          </button>
        </div>
      </header>

      <div className="org-detail-facets">
        <div className="facet">
          <div>
            <small>{t('positions.facet.status')}</small>
            <strong>{selected.status}</strong>
          </div>
        </div>
        <div className="facet">
          <div>
            <small>{t('positions.facet.headcount')}</small>
            <strong>
              {selected.headcount_budget === 0
                ? t('positions.headcountUnlimited')
                : `${selected.current_occupants} / ${selected.headcount_budget}`}
            </strong>
          </div>
        </div>
        {selected.grade && (
          <div className="facet">
            <div>
              <small>{t('positions.facet.grade')}</small>
              <strong>{selected.grade}</strong>
            </div>
          </div>
        )}
        {selected.job_family && (
          <div className="facet">
            <div>
              <small>{t('positions.facet.family')}</small>
              <strong>{selected.job_family}</strong>
            </div>
          </div>
        )}
        {reportsToName && (
          <div className="facet">
            <ChevronRight size={14} />
            <div>
              <small>{t('positions.facet.reportsTo')}</small>
              <strong>{reportsToName}</strong>
            </div>
          </div>
        )}
      </div>

      <div className="org-detail-members">
        <div className="panel-heading">
          <div>
            <span className="section-label">
              <Users size={14} /> {t('positions.occupants')}
            </span>
            <p>{t('positions.occupantsDescription')}</p>
          </div>
          <div className="panel-actions">
            <button
              className="secondary-button icon-button"
              onClick={() => void reloadOccupants()}
              disabled={occupants.kind === 'loading'}
              type="button"
            >
              <RefreshCw
                size={13}
                className={occupants.kind === 'loading' ? 'spin' : undefined}
              />
              <span>{t('positions.refresh')}</span>
            </button>
            <button
              className="primary-button"
              onClick={() => setAssignOpen(true)}
              type="button"
            >
              <UserPlus size={14} />
              <span>{t('positions.assign')}</span>
            </button>
          </div>
        </div>

        {occupants.kind === 'loading' && (
          <div className="state-panel">{t('positions.occupantsLoading')}</div>
        )}
        {occupants.kind === 'error' && (
          <div className="state-panel error-state">
            {t('positions.occupantsError')}: {occupants.message}
          </div>
        )}
        {occupants.kind === 'ready' && occupants.items.length === 0 && (
          <div className="state-panel">{t('positions.occupantsEmpty')}</div>
        )}
        {occupants.kind === 'ready' && occupants.items.length > 0 && (
          <ul className="member-list">
            {occupants.items.map((o) => (
              <li className="member-row" key={o.tenure_id}>
                <span className="member-avatar" aria-hidden="true">
                  {o.display_name.charAt(0).toUpperCase() || '?'}
                </span>
                <span className="member-body">
                  <strong>{o.display_name}</strong>
                  <small>
                    {o.email} · since {o.start_date}
                  </small>
                </span>
                <span className="member-tags">
                  {o.is_primary ? (
                    <span className="badge badge-primary">
                      <Check size={12} /> {t('positions.primary')}
                    </span>
                  ) : (
                    <button
                      className="link-button"
                      onClick={() => void setPrimary(selected.id, o.user_id)}
                      type="button"
                    >
                      {t('positions.setPrimary')}
                    </button>
                  )}
                  <button
                    className="icon-button danger"
                    onClick={() => void endTenure(selected.id, o.user_id)}
                    type="button"
                    title={t('positions.endTenure')}
                  >
                    <Trash2 size={13} />
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <AssignOccupantDialog
        open={assignOpen}
        onClose={() => setAssignOpen(false)}
        positionId={selected.id}
      />
    </section>
  )
}
