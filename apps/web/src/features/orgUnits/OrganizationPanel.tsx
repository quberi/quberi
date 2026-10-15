import {
  Archive,
  Check,
  ChevronDown,
  ChevronRight,
  FolderPlus,
  MoveRight,
  Pencil,
  RefreshCw,
  Trash2,
  UserPlus,
  Users,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import { AddMemberDialog, CreateOrgUnitDialog, MoveOrgUnitDialog } from './dialogs'
import { useOrgUnits } from './OrgUnitsContext'
import type { OrgUnitNode, OrgUnitTreeItem } from './types'

/**
 * Self-managed organisation panel.
 *
 * Layout:
 *   ┌───────────────┬─────────────────────────────────────┐
 *   │ actions bar   │ header (name/type/path)             │
 *   │ ─────────────│ ────────────────────────────────────│
 *   │ tree          │ detail form + member list          │
 *   └───────────────┴─────────────────────────────────────┘
 */
/**
 * OrgUnits tab body — designed to be embedded inside the tabbed
 * ``OrganizationPanel``. The outer chrome (section + tab bar) lives in
 * ``features/organization/OrganizationPanel.tsx``.
 */
export function OrgUnitsPanel() {
  const { t } = useTranslation()
  const { units, tree, reload, includeArchived, setIncludeArchived, selectedId } =
    useOrgUnits()

  const [createOpen, setCreateOpen] = useState(false)
  const [createParentId, setCreateParentId] = useState<string | null>(null)

  const openCreate = useCallback((parentId: string | null) => {
    setCreateParentId(parentId)
    setCreateOpen(true)
  }, [])

  return (
    <section className="org-tab-body">
      <header className="org-tab-header">
        <div>
          <p>{t('orgUnits.description')}</p>
        </div>
        <div className="panel-actions">
          <label className="toggle-row">
            <input
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
              type="checkbox"
            />
            <span>{t('orgUnits.showArchived')}</span>
          </label>
          <button
            className="secondary-button icon-button"
            disabled={units.kind === 'loading'}
            onClick={() => void reload()}
            type="button"
          >
            <RefreshCw
              size={14}
              className={units.kind === 'loading' ? 'spin' : undefined}
            />
            <span>{t('orgUnits.refresh')}</span>
          </button>
          <button
            className="primary-button"
            onClick={() => openCreate(null)}
            type="button"
          >
            <FolderPlus size={15} />
            <span>{t('orgUnits.newRoot')}</span>
          </button>
        </div>
      </header>

      <div className="org-workspace">
        <TreeColumn tree={tree} onCreateChild={openCreate} />
        <DetailColumn onCreateChild={openCreate} />
      </div>

      <CreateOrgUnitDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        parentId={createParentId}
      />
      {/* Move + AddMember dialogs live inside DetailColumn so they can bind
          to the currently-selected unit id without another prop drill. */}
      {units.kind === 'error' && (
        <div className="state-panel error-state" style={{ margin: '0 24px 24px' }}>
          {t('orgUnits.loadError')}: {units.message}
        </div>
      )}
      {units.kind === 'ready' && tree.length === 0 && (
        <div className="state-panel" style={{ margin: '0 24px 24px' }}>
          {t('orgUnits.empty')}
          {selectedId ? null : ''}
        </div>
      )}
    </section>
  )
}

/* ------------------------------------------------------------------------ */
/* Left column — the tree                                                    */
/* ------------------------------------------------------------------------ */

interface TreeProps {
  tree: OrgUnitNode[]
  onCreateChild: (parentId: string | null) => void
}

function TreeColumn({ tree, onCreateChild }: TreeProps) {
  const { units, selectedId, selectUnit } = useOrgUnits()

  return (
    <aside className="org-tree">
      {units.kind === 'loading' && (
        <div className="chat-sidebar-hint">Loading…</div>
      )}
      {units.kind === 'ready' && tree.length === 0 && (
        <div className="chat-sidebar-hint">—</div>
      )}
      {tree.map((node) => (
        <TreeNode
          key={node.id}
          node={node}
          selectedId={selectedId}
          onSelect={selectUnit}
          onCreateChild={onCreateChild}
        />
      ))}
    </aside>
  )
}

interface TreeNodeProps {
  node: OrgUnitNode
  selectedId: string | null
  onSelect: (id: string) => void
  onCreateChild: (parentId: string) => void
}

function TreeNode({ node, selectedId, onSelect, onCreateChild }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(true)
  const hasChildren = node.children.length > 0
  const isActive = selectedId === node.id

  return (
    <div className="org-tree-node">
      <div
        className={isActive ? 'org-tree-row active' : 'org-tree-row'}
        style={{ paddingLeft: 8 + node.depth * 18 }}
      >
        <button
          className="org-tree-caret"
          onClick={() => setExpanded((v) => !v)}
          type="button"
          aria-label={expanded ? 'collapse' : 'expand'}
        >
          {hasChildren ? (
            expanded ? (
              <ChevronDown size={13} />
            ) : (
              <ChevronRight size={13} />
            )
          ) : (
            <span className="org-tree-caret-placeholder" />
          )}
        </button>
        <button
          className="org-tree-label"
          onClick={() => onSelect(node.id)}
          type="button"
        >
          <strong>{node.name}</strong>
          <small>
            {node.status === 'archived' ? '· archived · ' : ''}
            {node.type}
          </small>
        </button>
        <button
          className="org-tree-add"
          onClick={() => onCreateChild(node.id)}
          type="button"
          title="add child"
        >
          <FolderPlus size={13} />
        </button>
      </div>
      {expanded &&
        hasChildren &&
        node.children.map((child) => (
          <TreeNode
            key={child.id}
            node={child}
            selectedId={selectedId}
            onSelect={onSelect}
            onCreateChild={onCreateChild}
          />
        ))}
    </div>
  )
}

/* ------------------------------------------------------------------------ */
/* Right column — selection detail                                           */
/* ------------------------------------------------------------------------ */

interface DetailProps {
  onCreateChild: (parentId: string | null) => void
}

function DetailColumn({ onCreateChild }: DetailProps) {
  const { t } = useTranslation()
  const {
    units,
    tree,
    selectedId,
    selectUnit,
    updateUnit,
    archiveUnit,
    members,
    reloadMembers,
    removeMember,
    setMemberPrimary,
  } = useOrgUnits()

  const [renaming, setRenaming] = useState(false)
  const [renameDraft, setRenameDraft] = useState('')
  const [renameError, setRenameError] = useState<string | null>(null)
  const [renameSubmitting, setRenameSubmitting] = useState(false)
  const [moveOpen, setMoveOpen] = useState(false)
  const [addMemberOpen, setAddMemberOpen] = useState(false)

  const selectedItem: OrgUnitTreeItem | undefined = useMemo(() => {
    if (units.kind !== 'ready') return undefined
    return units.items.find((u) => u.id === selectedId)
  }, [units, selectedId])

  // Also grab the node from the tree so we can pass it to MoveOrgUnitDialog
  // (which needs .children to build the exclude set).
  const selectedNode: OrgUnitNode | null = useMemo(() => {
    const find = (nodes: OrgUnitNode[]): OrgUnitNode | null => {
      for (const n of nodes) {
        if (n.id === selectedId) return n
        const nested = find(n.children)
        if (nested) return nested
      }
      return null
    }
    return find(tree)
  }, [tree, selectedId])

  useEffect(() => {
    // Reset any inline edit state when the selection changes.
    setRenaming(false)
    setRenameError(null)
    setRenameDraft(selectedItem?.name ?? '')
  }, [selectedId, selectedItem?.name])

  if (!selectedId || !selectedItem) {
    return (
      <section className="org-detail">
        <div className="state-panel">{t('orgUnits.pickOne')}</div>
      </section>
    )
  }

  const parentName = selectedItem.parent_id
    ? units.kind === 'ready'
      ? units.items.find((u) => u.id === selectedItem.parent_id)?.name ?? '—'
      : '—'
    : t('orgUnits.rootParent')

  const submitRename = async () => {
    const value = renameDraft.trim()
    if (!value || value === selectedItem.name) {
      setRenaming(false)
      return
    }
    setRenameSubmitting(true)
    setRenameError(null)
    try {
      await updateUnit(selectedItem.id, { name: value })
      setRenaming(false)
    } catch (err) {
      setRenameError(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    } finally {
      setRenameSubmitting(false)
    }
  }

  const doArchive = async () => {
    if (!window.confirm(t('orgUnits.confirmArchive', { name: selectedItem.name }))) return
    try {
      await archiveUnit(selectedItem.id)
      selectUnit(null)
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
          {renaming ? (
            <div className="org-detail-rename">
              <input
                autoFocus
                onChange={(e) => setRenameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void submitRename()
                  if (e.key === 'Escape') setRenaming(false)
                }}
                type="text"
                value={renameDraft}
              />
              <button
                className="primary-button"
                disabled={renameSubmitting}
                onClick={() => void submitRename()}
                type="button"
              >
                <Check size={14} />
              </button>
            </div>
          ) : (
            <>
              <h2>{selectedItem.name}</h2>
              <button
                className="link-button"
                onClick={() => {
                  setRenameDraft(selectedItem.name)
                  setRenaming(true)
                }}
                type="button"
              >
                <Pencil size={12} />
                <span>{t('orgUnits.actions.rename')}</span>
              </button>
            </>
          )}
        </div>
        <div className="org-detail-actions">
          <button
            className="secondary-button icon-button"
            onClick={() => onCreateChild(selectedItem.id)}
            type="button"
          >
            <FolderPlus size={13} />
            <span>{t('orgUnits.actions.addChild')}</span>
          </button>
          <button
            className="secondary-button icon-button"
            onClick={() => setMoveOpen(true)}
            type="button"
          >
            <MoveRight size={13} />
            <span>{t('orgUnits.actions.move')}</span>
          </button>
          <button
            className="secondary-button icon-button danger"
            onClick={() => void doArchive()}
            type="button"
            disabled={selectedItem.status === 'archived'}
          >
            <Archive size={13} />
            <span>{t('orgUnits.actions.archive')}</span>
          </button>
        </div>
      </header>

      {renameError && <div className="dialog-error" style={{ margin: '0 24px' }}>{renameError}</div>}

      <div className="org-detail-facets">
        <div className="facet">
          <div>
            <small>{t('orgUnits.facet.type')}</small>
            <strong>{t(`orgUnits.type.${selectedItem.type}`)}</strong>
          </div>
        </div>
        <div className="facet">
          <div>
            <small>{t('orgUnits.facet.parent')}</small>
            <strong>{parentName}</strong>
          </div>
        </div>
        <div className="facet">
          <div>
            <small>{t('orgUnits.facet.status')}</small>
            <strong>{selectedItem.status}</strong>
          </div>
        </div>
        <div className="facet">
          <div>
            <small>{t('orgUnits.facet.sync')}</small>
            <strong>{selectedItem.sync_state}</strong>
          </div>
        </div>
        {selectedItem.code && (
          <div className="facet">
            <div>
              <small>{t('orgUnits.facet.code')}</small>
              <strong>{selectedItem.code}</strong>
            </div>
          </div>
        )}
      </div>

      <div className="org-detail-members">
        <div className="panel-heading">
          <div>
            <span className="section-label">
              <Users size={14} /> {t('orgUnits.membersTitle')}
            </span>
            <p>{t('orgUnits.membersDescription')}</p>
          </div>
          <div className="panel-actions">
            <button
              className="secondary-button icon-button"
              onClick={() => void reloadMembers()}
              disabled={members.kind === 'loading'}
              type="button"
            >
              <RefreshCw
                size={13}
                className={members.kind === 'loading' ? 'spin' : undefined}
              />
              <span>{t('orgUnits.actions.refresh')}</span>
            </button>
            <button
              className="primary-button"
              onClick={() => setAddMemberOpen(true)}
              type="button"
            >
              <UserPlus size={14} />
              <span>{t('orgUnits.actions.addMember')}</span>
            </button>
          </div>
        </div>

        {members.kind === 'loading' && (
          <div className="state-panel">{t('orgUnits.membersLoading')}</div>
        )}
        {members.kind === 'error' && (
          <div className="state-panel error-state">
            {t('orgUnits.membersError')}: {members.message}
          </div>
        )}
        {members.kind === 'ready' && members.items.length === 0 && (
          <div className="state-panel">{t('orgUnits.membersEmpty')}</div>
        )}
        {members.kind === 'ready' && members.items.length > 0 && (
          <ul className="member-list">
            {members.items.map((m) => (
              <li className="member-row" key={m.membership_id}>
                <span className="member-avatar" aria-hidden="true">
                  {m.display_name.charAt(0).toUpperCase() || '?'}
                </span>
                <span className="member-body">
                  <strong>{m.display_name}</strong>
                  <small>{m.email}</small>
                </span>
                <span className="member-tags">
                  {m.is_primary ? (
                    <span className="badge badge-primary">
                      <Check size={12} /> {t('orgUnits.primary')}
                    </span>
                  ) : (
                    <button
                      className="link-button"
                      onClick={() => void setMemberPrimary(selectedItem.id, m.user_id)}
                      type="button"
                    >
                      {t('orgUnits.actions.setPrimary')}
                    </button>
                  )}
                  <button
                    className="icon-button danger"
                    onClick={() => void removeMember(selectedItem.id, m.user_id)}
                    type="button"
                    title={t('orgUnits.actions.removeMember')}
                  >
                    <Trash2 size={13} />
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <MoveOrgUnitDialog
        open={moveOpen}
        onClose={() => setMoveOpen(false)}
        node={selectedNode}
      />
      <AddMemberDialog
        open={addMemberOpen}
        onClose={() => setAddMemberOpen(false)}
        unitId={selectedItem.id}
      />
    </section>
  )
}
