import {
  Archive,
  Briefcase,
  Building2,
  Check,
  ChevronDown,
  ChevronRight,
  Pencil,
  RefreshCw,
  Search,
  Shield,
  ShieldCheck,
  SortAsc,
  Trash2,
  UserCog,
  UserPlus,
  Users,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import {
  AddGroupMemberDialog,
  CreateGroupDialog,
  EditGroupDialog,
} from './dialogs'
import { useGroups } from './GroupsContext'
import type {
  EffectiveGroupHitPath,
  EffectiveGroupMember,
  GroupHitPathCode,
  GroupListItem,
  GroupMemberEntry,
  GroupMemberType,
  GroupType,
} from './types'

/**
 * Groups tab — matrix cohorts across the org tree.
 *
 * Layout:
 *   ┌──────────────────────┬────────────────────────────────┐
 *   │ filters + list       │ header (name/type/facets/acts) │
 *   │                      │──────────────────────────────  │
 *   │                      │ direct members grouped by type │
 *   │                      │──────────────────────────────  │
 *   │                      │ effective members (collapsed)  │
 *   └──────────────────────┴────────────────────────────────┘
 */

type SortKey = 'name' | 'members' | 'updated'

const ALL_TYPES: GroupType[] = ['static', 'dynamic', 'security', 'project']
const MEMBER_TYPE_ORDER: GroupMemberType[] = [
  'user',
  'position',
  'organization_unit',
  'service_account',
]

function toMessage(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

export function GroupsPanel() {
  const { t } = useTranslation()
  const {
    groups,
    reload,
    includeArchived,
    setIncludeArchived,
    selectedId,
  } = useGroups()
  const [createOpen, setCreateOpen] = useState(false)

  return (
    <section className="org-tab-body">
      <header className="org-tab-header">
        <div>
          <p>{t('groups.description')}</p>
        </div>
        <div className="panel-actions">
          <label className="toggle-row">
            <input
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
              type="checkbox"
            />
            <span>{t('groups.showArchived')}</span>
          </label>
          <button
            className="secondary-button icon-button"
            disabled={groups.kind === 'loading'}
            onClick={() => void reload()}
            type="button"
          >
            <RefreshCw
              size={14}
              className={groups.kind === 'loading' ? 'spin' : undefined}
            />
            <span>{t('groups.refresh')}</span>
          </button>
          <button
            className="primary-button"
            onClick={() => setCreateOpen(true)}
            type="button"
          >
            <span>{t('groups.new')}</span>
          </button>
        </div>
      </header>

      <div className="org-workspace">
        <GroupListColumn />
        <GroupDetailColumn />
      </div>

      <CreateGroupDialog open={createOpen} onClose={() => setCreateOpen(false)} />

      {groups.kind === 'error' && (
        <div className="state-panel error-state" style={{ margin: '0 24px 24px' }}>
          {t('groups.loadError')}: {groups.message}
        </div>
      )}
      {groups.kind === 'ready' && groups.items.length === 0 && !selectedId && (
        <div className="state-panel" style={{ margin: '0 24px 24px' }}>
          {t('groups.empty')}
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Left column — search + filter chips + sort + list
// ---------------------------------------------------------------------------

function GroupListColumn() {
  const { t } = useTranslation()
  const { groups, selectedId, selectGroup } = useGroups()

  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState<Set<GroupType>>(new Set())
  const [sortKey, setSortKey] = useState<SortKey>('name')

  const items = useMemo(() => {
    if (groups.kind !== 'ready') return [] as GroupListItem[]
    let list: GroupListItem[] = groups.items
    if (query.trim()) {
      const q = query.trim().toLowerCase()
      list = list.filter(
        (g) =>
          g.name.toLowerCase().includes(q) ||
          (g.description ?? '').toLowerCase().includes(q),
      )
    }
    if (typeFilter.size > 0) {
      list = list.filter((g) => typeFilter.has(g.type))
    }
    // Sorting: descending for numeric / timestamp, alpha for names.
    const sorted = [...list]
    sorted.sort((a, b) => {
      if (sortKey === 'name') return a.name.localeCompare(b.name, 'zh')
      if (sortKey === 'members') return b.member_count - a.member_count
      // updated_at
      return b.updated_at.localeCompare(a.updated_at)
    })
    return sorted
  }, [groups, query, typeFilter, sortKey])

  const toggleType = (tp: GroupType) => {
    setTypeFilter((prev) => {
      const next = new Set(prev)
      if (next.has(tp)) next.delete(tp)
      else next.add(tp)
      return next
    })
  }

  return (
    <aside className="org-tree groups-list-col">
      <div className="groups-list-toolbar">
        <div className="groups-search">
          <Search size={12} />
          <input
            className="groups-search-input"
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('groups.searchPlaceholder')}
            type="search"
            value={query}
          />
          {query && (
            <button
              aria-label={t('groups.clearSearch')}
              className="groups-search-clear"
              onClick={() => setQuery('')}
              type="button"
            >
              <X size={11} />
            </button>
          )}
        </div>
        <div className="groups-filter-chips">
          {ALL_TYPES.map((tp) => (
            <button
              className={`chip chip-group-${tp} ${
                typeFilter.has(tp) ? 'chip-active' : ''
              }`}
              key={tp}
              onClick={() => toggleType(tp)}
              type="button"
            >
              {t(`groups.type.${tp}`)}
            </button>
          ))}
        </div>
        <label className="groups-sort">
          <SortAsc size={11} />
          <select
            onChange={(e) => setSortKey(e.target.value as SortKey)}
            value={sortKey}
          >
            <option value="name">{t('groups.sort.name')}</option>
            <option value="members">{t('groups.sort.members')}</option>
            <option value="updated">{t('groups.sort.updated')}</option>
          </select>
        </label>
      </div>

      <div className="groups-list-scroll">
        {groups.kind === 'loading' && (
          <div className="chat-sidebar-hint">{t('groups.loading')}</div>
        )}
        {items.length === 0 && groups.kind === 'ready' && (
          <div className="chat-sidebar-hint">{t('groups.filterEmpty')}</div>
        )}
        {items.map((g) => (
          <GroupRow
            key={g.id}
            group={g}
            isActive={g.id === selectedId}
            onSelect={() => selectGroup(g.id)}
          />
        ))}
      </div>
    </aside>
  )
}

function GroupRow({
  group,
  isActive,
  onSelect,
}: {
  group: GroupListItem
  isActive: boolean
  onSelect: () => void
}) {
  const { t } = useTranslation()
  return (
    <div className={isActive ? 'org-tree-row active' : 'org-tree-row'}>
      <span className="org-tree-caret-placeholder" />
      <button className="org-tree-label" onClick={onSelect} type="button">
        <strong>
          {group.name}
          {group.status === 'archived' && (
            <span className="badge badge-pending" style={{ marginLeft: 6 }}>
              archived
            </span>
          )}
        </strong>
        <small>
          <span className={`chip chip-inline chip-group-${group.type}`}>
            {t(`groups.type.${group.type}`)}
          </span>
          <span className="dot-sep">·</span>
          {group.member_count} {t('groups.membersShort')}
        </small>
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Right column — group detail with grouped members + effective view
// ---------------------------------------------------------------------------

function GroupDetailColumn() {
  const { t } = useTranslation()
  const {
    groups,
    selectedId,
    members,
    reloadMembers,
    removeMember,
    archiveGroup,
    selectGroup,
    effectiveMembers,
    reloadEffectiveMembers,
  } = useGroups()

  const [addOpen, setAddOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  // Multi-select bulk mode: a Set of ``${type}:${id}`` composite keys.
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // Track the last-clicked row for shift-click range selection.
  const lastClickIdxRef = useRef<number | null>(null)

  const selectedGroup = useMemo(() => {
    if (groups.kind !== 'ready') return undefined
    return groups.items.find((g) => g.id === selectedId)
  }, [groups, selectedId])

  // Reset multi-select whenever the group or member list changes.
  useEffect(() => {
    setSelected(new Set())
    lastClickIdxRef.current = null
  }, [selectedId, members.kind])

  if (!selectedGroup) {
    return (
      <section className="org-detail">
        <div className="state-panel">{t('groups.pickOne')}</div>
      </section>
    )
  }

  const memberList: GroupMemberEntry[] =
    members.kind === 'ready' ? members.items : []

  const groupedByType: Record<GroupMemberType, GroupMemberEntry[]> = {
    user: [],
    position: [],
    organization_unit: [],
    service_account: [],
  }
  for (const m of memberList) {
    groupedByType[m.member_type].push(m)
  }
  const orderedMembers = MEMBER_TYPE_ORDER.flatMap(
    (tp) => groupedByType[tp],
  )
  const memberBreakdown = MEMBER_TYPE_ORDER.map((tp) => ({
    type: tp,
    count: groupedByType[tp].length,
  })).filter((b) => b.count > 0)

  const effectiveCount =
    effectiveMembers.kind === 'ready' ? effectiveMembers.items.length : null

  const keyOf = (m: GroupMemberEntry) => `${m.member_type}:${m.member_id}`

  const toggleSelect = (m: GroupMemberEntry, idx: number, shift: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev)
      const key = keyOf(m)
      if (shift && lastClickIdxRef.current !== null) {
        const [start, end] = [
          Math.min(lastClickIdxRef.current, idx),
          Math.max(lastClickIdxRef.current, idx),
        ]
        for (let i = start; i <= end; i++) {
          next.add(keyOf(orderedMembers[i]))
        }
      } else if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })
    lastClickIdxRef.current = idx
  }

  const doArchive = async () => {
    if (!window.confirm(t('groups.confirmArchive', { name: selectedGroup.name }))) return
    try {
      await archiveGroup(selectedGroup.id)
      selectGroup(null)
    } catch (err) {
      window.alert(toMessage(err))
    }
  }

  const doRemove = async (m: GroupMemberEntry) => {
    const subject = m.display_name || t(`groups.memberType.${m.member_type}`)
    if (!window.confirm(t('groups.confirmRemoveOne', { subject }))) return
    try {
      await removeMember(selectedGroup.id, m.member_type, m.member_id)
    } catch (err) {
      window.alert(toMessage(err))
    }
  }

  const doBulkRemove = async () => {
    if (selected.size === 0) return
    const chosen = orderedMembers.filter((m) => selected.has(keyOf(m)))
    if (!window.confirm(t('groups.confirmRemoveBulk', { count: chosen.length }))) return
    try {
      // Serial removal; parallelisation would risk partial-failure races
      // and small member counts don't need the speedup.
      for (const m of chosen) {
        await removeMember(selectedGroup.id, m.member_type, m.member_id)
      }
      setSelected(new Set())
    } catch (err) {
      window.alert(toMessage(err))
    }
  }

  return (
    <section className="org-detail">
      <header className="org-detail-header">
        <div className="org-detail-title">
          <h2>{selectedGroup.name}</h2>
          <span className={`badge chip-group-${selectedGroup.type} chip-inline`}>
            {t(`groups.type.${selectedGroup.type}`)}
          </span>
        </div>
        <div className="org-detail-actions">
          <button
            className="secondary-button icon-button"
            onClick={() => setEditOpen(true)}
            type="button"
          >
            <Pencil size={13} />
            <span>{t('groups.edit.button')}</span>
          </button>
          <button
            className="secondary-button icon-button danger"
            disabled={selectedGroup.status === 'archived'}
            onClick={() => void doArchive()}
            type="button"
          >
            <Archive size={13} />
            <span>{t('groups.archive')}</span>
          </button>
        </div>
      </header>

      {selectedGroup.description && (
        <p className="space-overview-description" style={{ padding: '4px 24px' }}>
          {selectedGroup.description}
        </p>
      )}

      <div className="org-detail-facets">
        <div className="facet">
          <ShieldCheck size={14} />
          <div>
            <small>{t('groups.facet.type')}</small>
            <strong>{t(`groups.type.${selectedGroup.type}`)}</strong>
          </div>
        </div>
        <div className="facet">
          <Users size={14} />
          <div>
            <small>{t('groups.facet.directBindings')}</small>
            <strong>{selectedGroup.member_count}</strong>
          </div>
        </div>
        <div className="facet">
          <UserCog size={14} />
          <div>
            <small>{t('groups.facet.effectiveUsers')}</small>
            <strong>
              {effectiveCount === null
                ? t('groups.facet.effectivePending')
                : effectiveCount}
            </strong>
          </div>
        </div>
        {memberBreakdown.length > 0 && (
          <div className="facet facet-wide">
            <div>
              <small>{t('groups.facet.breakdown')}</small>
              <strong>
                {memberBreakdown
                  .map(
                    (b) =>
                      `${b.count} ${t(`groups.memberType.${b.type}`)}`,
                  )
                  .join(' · ')}
              </strong>
            </div>
          </div>
        )}
      </div>

      <div className="org-detail-members">
        <div className="panel-heading">
          <div>
            <span className="section-label">
              <Users size={14} /> {t('groups.membersTitle')}
            </span>
            <p>{t('groups.membersDescription')}</p>
          </div>
          <div className="panel-actions">
            {selected.size > 0 && (
              <button
                className="secondary-button icon-button danger"
                onClick={() => void doBulkRemove()}
                type="button"
              >
                <Trash2 size={13} />
                <span>
                  {t('groups.removeSelected', { count: selected.size })}
                </span>
              </button>
            )}
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
              <span>{t('groups.refresh')}</span>
            </button>
            <button
              className="primary-button"
              onClick={() => setAddOpen(true)}
              type="button"
            >
              <UserPlus size={14} />
              <span>{t('groups.addMember')}</span>
            </button>
          </div>
        </div>

        {members.kind === 'loading' && (
          <div className="state-panel">{t('groups.membersLoading')}</div>
        )}
        {members.kind === 'error' && (
          <div className="state-panel error-state">
            {t('groups.membersError')}: {members.message}
          </div>
        )}
        {members.kind === 'ready' && memberList.length === 0 && (
          <div className="state-panel">{t('groups.membersEmpty')}</div>
        )}
        {members.kind === 'ready' && memberList.length > 0 && (
          <GroupedMemberList
            groupedByType={groupedByType}
            orderedMembers={orderedMembers}
            selected={selected}
            onToggle={toggleSelect}
            onRemove={doRemove}
          />
        )}
      </div>

      <EffectiveMembersBlock
        state={effectiveMembers}
        reload={() => void reloadEffectiveMembers()}
      />

      <AddGroupMemberDialog
        open={addOpen}
        onClose={() => setAddOpen(false)}
        groupId={selectedGroup.id}
      />
      <EditGroupDialog
        open={editOpen}
        onClose={() => setEditOpen(false)}
        group={{
          id: selectedGroup.id,
          name: selectedGroup.name,
          type: selectedGroup.type,
          description: selectedGroup.description,
        }}
      />
    </section>
  )
}

// ---------------------------------------------------------------------------
// Grouped members list (users first, then positions, then org units)
// ---------------------------------------------------------------------------

function memberTypeIconFor(type: GroupMemberType) {
  switch (type) {
    case 'user':
      return <Users size={14} />
    case 'position':
      return <Briefcase size={14} />
    case 'organization_unit':
      return <Building2 size={14} />
    case 'service_account':
      return <Shield size={14} />
  }
}

function GroupedMemberList({
  groupedByType,
  orderedMembers,
  selected,
  onToggle,
  onRemove,
}: {
  groupedByType: Record<GroupMemberType, GroupMemberEntry[]>
  orderedMembers: GroupMemberEntry[]
  selected: Set<string>
  onToggle: (m: GroupMemberEntry, idx: number, shift: boolean) => void
  onRemove: (m: GroupMemberEntry) => void
}) {
  const { t } = useTranslation()
  const keyOf = (m: GroupMemberEntry) => `${m.member_type}:${m.member_id}`
  return (
    <div className="group-member-groups">
      {MEMBER_TYPE_ORDER.map((tp) => {
        const rows = groupedByType[tp]
        if (rows.length === 0) return null
        return (
          <div className="group-member-section" key={tp}>
            <div className="group-member-section-heading">
              {memberTypeIconFor(tp)}
              <span>{t(`groups.memberType.${tp}`)}</span>
              <span className="group-member-section-count">{rows.length}</span>
            </div>
            <ul className="group-member-list">
              {rows.map((m) => {
                const idx = orderedMembers.indexOf(m)
                const isSelected = selected.has(keyOf(m))
                return (
                  <li
                    className={
                      isSelected
                        ? 'group-member-row selected'
                        : 'group-member-row'
                    }
                    key={m.id}
                  >
                    <label className="group-member-check">
                      <input
                        checked={isSelected}
                        onChange={(e) => {
                          onToggle(
                            m,
                            idx,
                            (e.nativeEvent as MouseEvent).shiftKey,
                          )
                        }}
                        type="checkbox"
                      />
                    </label>
                    <span
                      className={`group-member-avatar avatar-${m.member_type}`}
                    >
                      {memberTypeIconFor(m.member_type)}
                    </span>
                    <span className="group-member-body">
                      <strong>{m.display_name}</strong>
                      {m.subtitle && <small>{m.subtitle}</small>}
                    </span>
                    <button
                      className="icon-button danger"
                      onClick={() => onRemove(m)}
                      title={t('groups.removeMember')}
                      type="button"
                    >
                      <Trash2 size={13} />
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Effective members block (collapsible)
// ---------------------------------------------------------------------------

function EffectiveMembersBlock({
  state,
  reload,
}: {
  state: ReturnType<typeof useGroups>['effectiveMembers']
  reload: () => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)

  const items = state.kind === 'ready' ? state.items : []

  return (
    <div className="group-effective-block">
      <button
        aria-expanded={open}
        className="group-effective-header"
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>
          {t('groups.effective.title')}
          {state.kind === 'ready' && (
            <span className="group-effective-count">{items.length}</span>
          )}
        </span>
        <span className="group-effective-desc">
          {t('groups.effective.description')}
        </span>
        <button
          className="icon-button"
          onClick={(e) => {
            e.stopPropagation()
            reload()
          }}
          type="button"
        >
          <RefreshCw
            size={12}
            className={state.kind === 'loading' ? 'spin' : undefined}
          />
        </button>
      </button>

      {open && (
        <>
          {state.kind === 'loading' && (
            <div className="state-panel">{t('groups.effective.loading')}</div>
          )}
          {state.kind === 'error' && (
            <div className="state-panel error-state">
              {t('groups.effective.loadError')}: {state.message}
            </div>
          )}
          {state.kind === 'ready' && items.length === 0 && (
            <div className="state-panel">{t('groups.effective.empty')}</div>
          )}
          {state.kind === 'ready' && items.length > 0 && (
            <ul className="group-effective-list">
              {items.map((m) => (
                <EffectiveMemberRow key={m.user_id} member={m} />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}

function EffectiveMemberRow({ member }: { member: EffectiveGroupMember }) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  return (
    <li className="group-effective-row">
      <button
        aria-expanded={expanded}
        className="group-effective-row-summary"
        onClick={() => setExpanded((v) => !v)}
        type="button"
      >
        <span className="group-effective-caret">
          {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </span>
        <span className="member-avatar" aria-hidden="true">
          {member.display_name.charAt(0).toUpperCase() || '?'}
        </span>
        <span className="group-effective-body">
          <strong>{member.display_name}</strong>
          <small>{member.email}</small>
        </span>
        <span className="group-effective-path-count">
          {t('groups.effective.pathsCount', { count: member.hit_paths.length })}
        </span>
      </button>
      {expanded && (
        <ul className="group-effective-paths">
          {member.hit_paths.map((p, i) => (
            <li key={i} className="group-effective-path">
              <HitPathLabel path={p} />
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function HitPathLabel({ path }: { path: EffectiveGroupHitPath }) {
  const { t } = useTranslation()
  const key: GroupHitPathCode = path.hit_path
  return (
    <span>
      {t(`groups.effective.hitPath.${key}`, {
        subject: path.subject_display_name,
      })}
    </span>
  )
}
