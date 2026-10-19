import { Loader2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useMembers } from '../members/MembersContext'
import { isAssignable } from '../spaceAccess/useSubjectCandidates'
import { useOrgUnits } from '../orgUnits/OrgUnitsContext'
import { usePositions } from '../positions/PositionsContext'

import { useGroups } from './GroupsContext'
import type { GroupMemberType, GroupType } from './types'

function toMessage(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

const GROUP_TYPES: GroupType[] = ['static', 'dynamic', 'security', 'project']

/* ========================================================================
 * CreateGroupDialog
 * ==================================================================== */

export function CreateGroupDialog({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const { t } = useTranslation()
  const { createGroup } = useGroups()

  const [name, setName] = useState('')
  const [type, setType] = useState<GroupType>('static')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setName('')
      setType('static')
      setDescription('')
      setError(null)
      setSubmitting(false)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [open, submitting, onClose])

  if (!open) return null

  const canSubmit = name.trim().length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await createGroup({
        name: name.trim(),
        type,
        description: description.trim() || null,
      })
      onClose()
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card">
        <header className="dialog-header">
          <h2>{t('groups.create.title')}</h2>
          <button
            aria-label={t('groups.create.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form onSubmit={handleSubmit} className="dialog-form">
          <label>
            <span>{t('groups.create.name')}</span>
            <input
              autoFocus
              onChange={(e) => setName(e.target.value)}
              placeholder="AI 作战小组"
              type="text"
              value={name}
            />
          </label>
          <label>
            <span>{t('groups.create.type')}</span>
            <select onChange={(e) => setType(e.target.value as GroupType)} value={type}>
              {GROUP_TYPES.map((tp) => (
                <option key={tp} value={tp}>
                  {t(`groups.type.${tp}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('groups.create.description')}</span>
            <textarea
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('groups.create.descriptionPlaceholder')}
              rows={3}
              value={description}
            />
          </label>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('groups.create.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('groups.create.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ========================================================================
 * EditGroupDialog — rename / retype / redescribe
 * ==================================================================== */

interface EditGroupProps {
  open: boolean
  onClose: () => void
  group: {
    id: string
    name: string
    type: GroupType
    description: string | null
  } | null
}

export function EditGroupDialog({ open, onClose, group }: EditGroupProps) {
  const { t } = useTranslation()
  const { updateGroup } = useGroups()

  const [name, setName] = useState('')
  const [type, setType] = useState<GroupType>('static')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Seed the fields when the dialog opens with a group in tow.
  useEffect(() => {
    if (open && group) {
      setName(group.name)
      setType(group.type)
      setDescription(group.description ?? '')
      setError(null)
      setSubmitting(false)
    }
  }, [open, group])

  useEffect(() => {
    if (!open) return
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [open, submitting, onClose])

  if (!open || !group) return null

  const trimmedName = name.trim()
  const trimmedDesc = description.trim()
  const dirty =
    trimmedName !== group.name ||
    type !== group.type ||
    trimmedDesc !== (group.description ?? '')
  const canSubmit = trimmedName.length > 0 && dirty && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      // Only send fields that actually changed — the PATCH endpoint treats
      // ``null`` as an explicit clear for description.
      const patch: Record<string, unknown> = {}
      if (trimmedName !== group.name) patch.name = trimmedName
      if (type !== group.type) patch.type = type
      if (trimmedDesc !== (group.description ?? '')) {
        patch.description = trimmedDesc || null
      }
      await updateGroup(group.id, patch as any)
      onClose()
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card">
        <header className="dialog-header">
          <h2>{t('groups.edit.title')}</h2>
          <button
            aria-label={t('groups.edit.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form onSubmit={handleSubmit} className="dialog-form">
          <label>
            <span>{t('groups.edit.name')}</span>
            <input
              autoFocus
              onChange={(e) => setName(e.target.value)}
              type="text"
              value={name}
            />
          </label>
          <label>
            <span>{t('groups.edit.type')}</span>
            <select onChange={(e) => setType(e.target.value as GroupType)} value={type}>
              {GROUP_TYPES.map((tp) => (
                <option key={tp} value={tp}>
                  {t(`groups.type.${tp}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('groups.edit.description')}</span>
            <textarea
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('groups.edit.descriptionPlaceholder')}
              rows={3}
              value={description}
            />
          </label>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('groups.edit.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('groups.edit.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ========================================================================
 * AddGroupMemberDialog — polymorphic subject picker
 * ==================================================================== */

const MEMBER_TYPES: GroupMemberType[] = ['user', 'organization_unit', 'position']

export function AddGroupMemberDialog({
  open,
  onClose,
  groupId,
}: {
  open: boolean
  onClose: () => void
  groupId: string
}) {
  const { t } = useTranslation()
  const { addMember, members } = useGroups()
  const { state: allMembers } = useMembers()
  const { units } = useOrgUnits()
  const { positions } = usePositions()

  const [memberType, setMemberType] = useState<GroupMemberType>('user')
  const [memberId, setMemberId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const currentKeys = useMemo(() => {
    if (members.kind !== 'ready') return new Set<string>()
    return new Set(members.items.map((m) => `${m.member_type}:${m.member_id}`))
  }, [members])

  const options = useMemo(() => {
    if (memberType === 'user' && allMembers.kind === 'ready') {
      return allMembers.items
        .filter((m) => isAssignable(m) && !currentKeys.has(`user:${m.id}`))
        .map((m) => ({ id: m.id, label: `${m.display_name} · ${m.email}` }))
    }
    if (memberType === 'organization_unit' && units.kind === 'ready') {
      return units.items
        .filter((u) => !currentKeys.has(`organization_unit:${u.id}`))
        .map((u) => ({ id: u.id, label: u.name }))
    }
    if (memberType === 'position' && positions.kind === 'ready') {
      return positions.items
        .filter((p) => !currentKeys.has(`position:${p.id}`))
        .map((p) => ({
          id: p.id,
          label: `${p.name}${p.code ? ` (${p.code})` : ''}`,
        }))
    }
    return []
  }, [memberType, allMembers, units, positions, currentKeys])

  useEffect(() => {
    if (open) {
      setMemberType('user')
      setMemberId('')
      setError(null)
      setSubmitting(false)
    }
  }, [open])

  useEffect(() => {
    // When the type changes, default to the first candidate.
    setMemberId(options[0]?.id ?? '')
  }, [memberType, options])

  if (!open) return null

  const canSubmit = memberId.length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await addMember(groupId, { member_type: memberType, member_id: memberId })
      onClose()
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card">
        <header className="dialog-header">
          <h2>{t('groups.addMemberDialog.title')}</h2>
          <button
            aria-label={t('groups.addMemberDialog.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form onSubmit={handleSubmit} className="dialog-form">
          <label>
            <span>{t('groups.addMemberDialog.subjectType')}</span>
            <select
              onChange={(e) => setMemberType(e.target.value as GroupMemberType)}
              value={memberType}
            >
              {MEMBER_TYPES.map((tp) => (
                <option key={tp} value={tp}>
                  {t(`groups.memberType.${tp}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('groups.addMemberDialog.subject')}</span>
            <select onChange={(e) => setMemberId(e.target.value)} value={memberId}>
              {options.length === 0 && (
                <option value="">{t('groups.addMemberDialog.noCandidates')}</option>
              )}
              {options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <p className="dialog-hint">{t('groups.addMemberDialog.hint')}</p>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('groups.addMemberDialog.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('groups.addMemberDialog.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
