import {
  ChevronDown,
  ChevronRight,
  Eye,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserCog,
  X,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import { useEffectiveSpaceMembers, useSpaceAccess } from './useSpaceAccess'
import { subjectIdForApi, useSubjectCandidates } from './useSubjectCandidates'
import type {
  AccessBinding,
  AccessSubjectType,
  EffectiveHitPath,
  EffectiveMember,
  HitPathCode,
  SpaceRoleCode,
} from './types'

/**
 * Direct role bindings on a single space. Inherited bindings (via groups,
 * positions, org units) are intentionally not shown here — that lives in
 * a future "effective access" viewer. Owners / admins can grant + revoke
 * from this panel; other callers see the list but writes 403 out.
 */
export function SpaceAccessPanel({ spaceId }: { spaceId: string | null }) {
  const { t } = useTranslation()
  const { state, reload, revoke, grant } = useSpaceAccess(spaceId)
  const effective = useEffectiveSpaceMembers(spaceId)
  const [addOpen, setAddOpen] = useState(false)

  // Any successful grant/revoke also invalidates the effective view — the
  // hooks are independent by design, so we chain the reload here.
  const reloadBoth = async () => {
    await Promise.all([reload(), effective.reload()])
  }

  const doRevoke = async (b: AccessBinding) => {
    if (!window.confirm(t('spaceAccess.confirmRevoke'))) return
    try {
      await revoke(b.id)
      await effective.reload()
    } catch (err) {
      window.alert(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    }
  }

  if (!spaceId) return null

  return (
    <section className="panel space-access-panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <KeyRound size={15} /> {t('spaceAccess.title')}
          </span>
          <p>{t('spaceAccess.description')}</p>
        </div>
        <div className="panel-actions">
          <button
            className="secondary-button icon-button"
            disabled={state.kind === 'loading' || effective.state.kind === 'loading'}
            onClick={() => void reloadBoth()}
            type="button"
          >
            <RefreshCw
              size={13}
              className={
                state.kind === 'loading' || effective.state.kind === 'loading'
                  ? 'spin'
                  : undefined
              }
            />
          </button>
          <button
            className="primary-button"
            onClick={() => setAddOpen(true)}
            type="button"
          >
            <ShieldCheck size={14} />
            <span>{t('spaceAccess.add')}</span>
          </button>
        </div>
      </div>

      {state.kind === 'loading' && (
        <div className="state-panel">{t('spaceAccess.loading')}</div>
      )}
      {state.kind === 'error' && (
        <div className="state-panel error-state">
          {t('spaceAccess.loadError')}: {state.message}
        </div>
      )}
      {state.kind === 'ready' && state.items.length === 0 && (
        <div className="state-panel">{t('spaceAccess.empty')}</div>
      )}
      {state.kind === 'ready' && state.items.length > 0 && (
        <ul className="space-access-list">
          {state.items.map((b) => (
            <li className="space-access-row" key={b.id}>
              <span className="space-access-subject">
                <SubjectTypeIcon type={b.subject_type} />
                <span>
                  <strong>{b.subject_display_name}</strong>
                  <small>
                    {t(`spaceAccess.subjectTypeShort.${b.subject_type}`)}
                    {b.subject_subtitle ? ` · ${b.subject_subtitle}` : ''}
                  </small>
                </span>
              </span>
              <span className={`badge role-${b.role_code}`}>
                <ShieldCheck size={12} /> {t(`members.role.${b.role_code}`)}
              </span>
              <button
                className="icon-button danger"
                onClick={() => void doRevoke(b)}
                title={t('spaceAccess.remove')}
                type="button"
              >
                <Trash2 size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}

      <EffectiveMembersSection state={effective.state} />

      <AddAccessDialog
        open={addOpen}
        onClose={() => {
          setAddOpen(false)
          // Any newly-granted binding must be reflected in the effective
          // view; the hook's ``grant`` callback already refreshes the
          // direct list.
          void effective.reload()
        }}
        spaceId={spaceId}
        grant={grant}
      />
    </section>
  )
}

/* ========================================================================
 * EffectiveMembersSection
 * ==================================================================== */

function EffectiveMembersSection({
  state,
}: {
  state: ReturnType<typeof useEffectiveSpaceMembers>['state']
}) {
  const { t } = useTranslation()

  return (
    <div className="effective-access-section">
      <div className="effective-access-heading">
        <span className="section-label">
          <Eye size={14} /> {t('spaceAccess.effective.title')}
        </span>
        <p>{t('spaceAccess.effective.description')}</p>
      </div>

      {state.kind === 'idle' && (
        <div className="state-panel">{t('spaceAccess.effective.idle')}</div>
      )}
      {state.kind === 'loading' && (
        <div className="state-panel">{t('spaceAccess.effective.loading')}</div>
      )}
      {state.kind === 'error' && (
        <div className="state-panel error-state">
          {t('spaceAccess.effective.loadError')}: {state.message}
        </div>
      )}
      {state.kind === 'ready' && state.items.length === 0 && (
        <div className="state-panel">{t('spaceAccess.effective.empty')}</div>
      )}
      {state.kind === 'ready' && state.items.length > 0 && (
        <ul className="effective-member-list">
          {state.items.map((m) => (
            <EffectiveMemberRow key={m.user_id} member={m} />
          ))}
        </ul>
      )}
    </div>
  )
}

function EffectiveMemberRow({ member }: { member: EffectiveMember }) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  const hasHitPaths = member.hit_paths.length > 0

  return (
    <li className="effective-member-row">
      <button
        aria-expanded={expanded}
        className="effective-member-summary"
        onClick={() => setExpanded((v) => !v)}
        type="button"
      >
        <span className="effective-member-caret" aria-hidden="true">
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
        <span className="member-avatar" aria-hidden="true">
          {member.display_name.charAt(0).toUpperCase() || '?'}
        </span>
        <span className="effective-member-body">
          <strong>{member.display_name}</strong>
          <small>{member.email}</small>
        </span>
        <span className={`badge role-${member.effective_role}`}>
          <ShieldCheck size={12} /> {t(`members.role.${member.effective_role}`)}
        </span>
        <span className="effective-member-path-count">
          {t('spaceAccess.effective.pathsCount', {
            count: member.hit_paths.length,
          })}
        </span>
      </button>
      {expanded && hasHitPaths && (
        <ul className="effective-hit-path-list">
          {member.hit_paths.map((p, idx) => (
            <li key={idx} className="effective-hit-path">
              <HitPathBadge path={p} />
              <span className={`badge role-${p.role_code} badge-inline`}>
                {t(`members.role.${p.role_code}`)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function HitPathBadge({ path }: { path: EffectiveHitPath }) {
  const { t } = useTranslation()
  // Map the enum to a translation key + subject display; the subject name
  // is interpolated so the sentence reads naturally in both languages.
  const key: HitPathCode = path.hit_path
  return (
    <span className="hit-path-label">
      {t(`spaceAccess.effective.hitPath.${key}`, {
        subject: path.subject_display_name,
      })}
    </span>
  )
}

function SubjectTypeIcon({ type }: { type: AccessSubjectType }) {
  // Keep icons small + monochrome; the visual weight is on the name.
  switch (type) {
    case 'user':
      return <UserCog size={14} />
    case 'group':
      return <UserCog size={14} />
    case 'organization_unit':
      return <UserCog size={14} />
    case 'position':
      return <UserCog size={14} />
    default:
      return <UserCog size={14} />
  }
}

/* ========================================================================
 * AddAccessDialog
 * ==================================================================== */

export const SUBJECT_TYPES: AccessSubjectType[] = [
  'organization',
  'organization_unit',
  'position',
  'group',
  'user',
]
export const ROLE_CODES: SpaceRoleCode[] = ['owner', 'admin', 'editor', 'viewer']

function AddAccessDialog({
  open,
  onClose,
  spaceId,
  grant,
}: {
  open: boolean
  onClose: () => void
  spaceId: string
  grant: ReturnType<typeof useSpaceAccess>['grant']
}) {
  const { t } = useTranslation()
  // Note: ``spaceId`` is intentionally unused here — the parent already
  // scoped the ``grant`` closure to this space, and we don't need to
  // subscribe to another copy of the list state.
  void spaceId

  const [subjectType, setSubjectType] = useState<AccessSubjectType>('user')
  const [subjectId, setSubjectId] = useState('')
  const [role, setRole] = useState<SpaceRoleCode>('viewer')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const candidates = useSubjectCandidates(subjectType)

  useEffect(() => {
    if (open) {
      setSubjectType('user')
      setRole('viewer')
      setError(null)
      setSubmitting(false)
    }
  }, [open])

  useEffect(() => {
    // When the subject type changes, reset the picked subject to the first
    // available candidate.
    setSubjectId(candidates[0]?.id ?? '')
  }, [subjectType, candidates])

  useEffect(() => {
    if (!open) return
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [open, submitting, onClose])

  if (!open) return null

  const canSubmit = subjectId.length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await grant({
        subject_type: subjectType,
        subject_id: subjectIdForApi(subjectType, subjectId),
        role,
      })
      onClose()
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card">
        <header className="dialog-header">
          <h2>{t('spaceAccess.addDialog.title')}</h2>
          <button
            aria-label={t('spaceAccess.addDialog.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form className="dialog-form" onSubmit={handleSubmit}>
          <label>
            <span>{t('spaceAccess.subjectType')}</span>
            <select
              onChange={(e) =>
                setSubjectType(e.target.value as AccessSubjectType)
              }
              value={subjectType}
            >
              {SUBJECT_TYPES.map((s) => (
                <option key={s} value={s}>
                  {t(`spaceAccess.subjectTypeOption.${s}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('spaceAccess.subject')}</span>
            <select
              onChange={(e) => setSubjectId(e.target.value)}
              value={subjectId}
            >
              {candidates.length === 0 && (
                <option value="">{t('spaceAccess.addDialog.noCandidates')}</option>
              )}
              {candidates.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                  {c.subtitle ? ` · ${c.subtitle}` : ''}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('spaceAccess.role')}</span>
            <select
              onChange={(e) => setRole(e.target.value as SpaceRoleCode)}
              value={role}
            >
              {ROLE_CODES.map((r) => (
                <option key={r} value={r}>
                  {t(`members.role.${r}`)}
                </option>
              ))}
            </select>
          </label>
          <p className="dialog-hint">{t('spaceAccess.addDialog.hint')}</p>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('spaceAccess.addDialog.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('spaceAccess.addDialog.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
