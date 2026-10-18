import {
  Briefcase,
  Clock3,
  Loader2,
  Mail,
  Network,
  RefreshCw,
  ShieldCheck,
  ShieldOff,
  UserCog,
  UserPlus,
  Users,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { listGroupsForUser } from '../groups/api'
import type { GroupListItem } from '../groups/types'

import { InviteMemberDialog } from './InviteMemberDialog'
import { useMembers } from './MembersContext'
import type { MemberRole, MemberSummary } from './types'

/**
 * Panel that lists everyone in the caller's organisation and — for owners /
 * admins — exposes an ``Invite`` button and per-row role controls (promote
 * to admin / demote to member). Non-privileged callers see the list but
 * every mutating call rejects them with 403; buttons stay visible for
 * consistency, and the error surfaces inline.
 *
 * The ``owner`` row never gets a role control — ownership can't be granted
 * or revoked through this panel (see ``members.set_member_role`` on the
 * backend), and a caller can't change their own role either.
 */

function roleBadgeClass(role: MemberRole): string {
  switch (role) {
    case 'owner':
      return 'badge role-owner'
    case 'admin':
      return 'badge role-admin'
    case 'member':
      return 'badge role-member'
    case 'editor':
      return 'badge role-editor'
    case 'viewer':
      return 'badge role-viewer'
    default:
      return 'badge role-none'
  }
}

function MemberRow({
  member,
  isSelf,
}: {
  member: MemberSummary
  isSelf: boolean
}) {
  const { t } = useTranslation()
  const { setRole } = useMembers()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Role control only makes sense for admin/member rows the caller isn't
  // themselves — owner is untouchable (backend rejects it anyway, but we
  // don't even show the affordance), and nobody edits their own role.
  const canToggleRole =
    !isSelf && (member.role_code === 'admin' || member.role_code === 'member')

  const toggleRole = async () => {
    if (!canToggleRole || busy) return
    const next = member.role_code === 'admin' ? 'member' : 'admin'
    setBusy(true)
    setError(null)
    try {
      await setRole(member.id, next)
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="member-row" data-user-id={member.id}>
      <span className="member-avatar" aria-hidden="true">
        {member.display_name.charAt(0).toUpperCase() || '?'}
      </span>
      <span className="member-body">
        <strong>{member.display_name}</strong>
        <small>{member.email}</small>
        {error && <small className="member-row-error">{error}</small>}
      </span>
      <span className="member-orgunit">
        {member.primary_org_unit_name ? (
          <>
            <small>{t('members.primaryOrgUnitLabel')}</small>
            <strong>{member.primary_org_unit_name}</strong>
          </>
        ) : (
          <small className="member-orgunit-empty">
            {t('members.primaryOrgUnitEmpty')}
          </small>
        )}
      </span>
      <span className="member-position">
        {member.primary_position_name ? (
          <>
            <small>
              <Briefcase size={11} /> {t('members.primaryPositionLabel')}
            </small>
            <strong>{member.primary_position_name}</strong>
          </>
        ) : (
          <small className="member-position-empty">
            {t('members.primaryPositionEmpty')}
          </small>
        )}
      </span>
      <span className="member-managers">
        {member.direct_managers && member.direct_managers.length > 0 ? (
          <>
            <small>
              <UserCog size={11} /> {t('members.directManagerLabel')}
            </small>
            <strong>
              {member.direct_managers.map((m) => m.display_name).join('、')}
            </strong>
          </>
        ) : (
          <small className="member-managers-empty">
            {t('members.directManagerEmpty')}
          </small>
        )}
      </span>
      <span className="member-tags">
        <span className={roleBadgeClass(member.role_code)}>
          <ShieldCheck size={13} />
          {member.role_code
            ? t(`members.role.${member.role_code}`)
            : t('members.role.none')}
        </span>
        {member.is_pending && (
          <span className="badge badge-pending">
            <Clock3 size={13} /> {t('members.pending')}
          </span>
        )}
        <UserGroupsChip userId={member.id} />
        {canToggleRole && (
          <button
            className="ghost-button member-role-toggle"
            disabled={busy}
            onClick={() => void toggleRole()}
            type="button"
          >
            {busy ? (
              <Loader2 size={13} className="spin" />
            ) : member.role_code === 'admin' ? (
              <ShieldOff size={13} />
            ) : (
              <ShieldCheck size={13} />
            )}
            <span>
              {member.role_code === 'admin'
                ? t('members.demoteToMember')
                : t('members.promoteToAdmin')}
            </span>
          </button>
        )}
      </span>
    </li>
  )
}

/**
 * Reverse-lookup chip: shows "N groups" once loaded, expands to a small
 * inline popover listing each group the user belongs to via any literal
 * membership path (direct / via position / via org unit).
 *
 * Load is lazy — we don't ask the API until the user actually clicks.
 * A per-instance cache keeps the second click free.
 */
function UserGroupsChip({ userId }: { userId: string }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [open, setOpen] = useState(false)
  const [groups, setGroups] = useState<GroupListItem[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const popoverRef = useRef<HTMLDivElement>(null)

  // Load lazily on first open; keep the result cached for subsequent opens.
  useEffect(() => {
    if (!open || groups !== null || loading || !credentials) return
    setLoading(true)
    setError(null)
    listGroupsForUser(credentials, userId)
      .then(({ items }) => setGroups(items))
      .catch((err: unknown) =>
        setError(
          err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
        ),
      )
      .finally(() => setLoading(false))
  }, [open, groups, loading, credentials, userId])

  // Close on outside click.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (
        popoverRef.current &&
        !popoverRef.current.contains(e.target as Node)
      ) {
        setOpen(false)
      }
    }
    window.addEventListener('mousedown', onDown)
    return () => window.removeEventListener('mousedown', onDown)
  }, [open])

  const count = groups?.length ?? null

  return (
    <span className="member-groups-chip-wrapper" ref={popoverRef}>
      <button
        className={`badge badge-groups-count ${open ? 'active' : ''}`}
        onClick={() => setOpen((v) => !v)}
        type="button"
        title={t('members.groupsChip')}
      >
        <Network size={12} />
        <span>
          {count === null
            ? t('members.groupsChip')
            : t('members.groupsChipCount', { count })}
        </span>
      </button>
      {open && (
        <div className="member-groups-popover" role="dialog">
          <header>
            <strong>{t('members.groupsPopoverTitle')}</strong>
            <button
              className="icon-button"
              onClick={() => setOpen(false)}
              type="button"
            >
              <RefreshCw size={11} />
            </button>
          </header>
          {loading && <div className="state-panel">{t('members.groupsLoading')}</div>}
          {error && <div className="state-panel error-state">{error}</div>}
          {!loading && !error && groups && groups.length === 0 && (
            <div className="state-panel">{t('members.groupsEmpty')}</div>
          )}
          {!loading && !error && groups && groups.length > 0 && (
            <ul className="member-groups-list">
              {groups.map((g) => (
                <li key={g.id}>
                  <strong>{g.name}</strong>
                  <small>
                    <span className={`chip chip-inline chip-group-${g.type}`}>
                      {t(`groups.type.${g.type}`)}
                    </span>
                    <span className="dot-sep">·</span>
                    {g.member_count} {t('groups.membersShort')}
                  </small>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </span>
  )
}

export function MembersPanel() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { state, reload } = useMembers()
  const [inviteOpen, setInviteOpen] = useState(false)

  // The list is loaded once, when the app starts. People are added elsewhere too (the
  // organisation page, another admin), so a list that was already loaded is read again each
  // time this page is opened. A first load still under way is left alone.
  const openedWith = useRef(state.kind)
  useEffect(() => {
    if (openedWith.current === 'ready') void reload()
    // Once per opening: ``reload`` changes with the sign-in, not with the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Simple role tally for the top-of-page stats card. We surface a few
  // buckets the operator actually cares about (owner / admin / member)
  // and roll the rest into ``other``.
  const tally = (() => {
    if (state.kind !== 'ready') return null
    const t0 = { total: state.items.length, owner: 0, admin: 0, member: 0, other: 0, pending: 0 }
    for (const m of state.items) {
      if (m.is_pending) t0.pending += 1
      switch (m.role_code) {
        case 'owner':
          t0.owner += 1
          break
        case 'admin':
          t0.admin += 1
          break
        case 'member':
          t0.member += 1
          break
        default:
          t0.other += 1
      }
    }
    return t0
  })()

  return (
    <>
      {tally && (
        <section className="panel members-role-tally">
          <div className="members-role-tally-item">
            <small>{t('members.tally.total')}</small>
            <strong>{tally.total}</strong>
          </div>
          <div className="members-role-tally-item">
            <small>{t('members.role.owner')}</small>
            <strong>{tally.owner}</strong>
          </div>
          <div className="members-role-tally-item">
            <small>{t('members.role.admin')}</small>
            <strong>{tally.admin}</strong>
          </div>
          <div className="members-role-tally-item">
            <small>{t('members.role.member')}</small>
            <strong>{tally.member}</strong>
          </div>
          <div className="members-role-tally-item">
            <small>{t('members.tally.other')}</small>
            <strong>{tally.other}</strong>
          </div>
          <div className="members-role-tally-item">
            <small>{t('members.tally.pending')}</small>
            <strong>{tally.pending}</strong>
          </div>
        </section>
      )}
      <section className="panel members-panel">
        <div className="panel-heading">
          <div>
            <span className="section-label">
              <Users size={15} /> {t('members.title')}
            </span>
            <p>{t('members.description')}</p>
          </div>
          <div className="panel-actions">
            <button
              className="secondary-button icon-button"
              onClick={() => void reload()}
              disabled={state.kind === 'loading'}
              type="button"
            >
              <RefreshCw
                size={14}
                className={state.kind === 'loading' ? 'spin' : undefined}
              />
              <span>{t('members.refresh')}</span>
            </button>
            <button
              className="primary-button"
              onClick={() => setInviteOpen(true)}
              type="button"
            >
              <UserPlus size={15} />
              <span>{t('members.invite')}</span>
            </button>
          </div>
        </div>

        {state.kind === 'loading' && (
          <div className="state-panel">{t('members.loading')}</div>
        )}
        {state.kind === 'error' && (
          <div className="state-panel error-state">
            <Mail size={16} />
            <span>{t('members.loadError')}: {state.message}</span>
          </div>
        )}
        {state.kind === 'ready' && state.items.length === 0 && (
          <div className="state-panel">{t('members.empty')}</div>
        )}
        {state.kind === 'ready' && state.items.length > 0 && (
          <ul className="member-list">
            {state.items.map((m) => (
              <MemberRow
                key={m.id}
                member={m}
                isSelf={
                  Boolean(credentials?.userEmail) &&
                  m.email.toLowerCase() === credentials?.userEmail.toLowerCase()
                }
              />
            ))}
          </ul>
        )}
      </section>
      <InviteMemberDialog open={inviteOpen} onClose={() => setInviteOpen(false)} />
    </>
  )
}
