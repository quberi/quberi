import { Loader2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useMembers } from '../members/MembersContext'
import { isAssignable } from '../spaceAccess/useSubjectCandidates'

import { useOrgUnits } from './OrgUnitsContext'
import type { OrgUnitNode, OrgUnitType } from './types'

/**
 * Dialogs shared by ``OrganizationPanel``:
 *  * ``CreateOrgUnitDialog`` — new unit (optionally under a parent)
 *  * ``MoveOrgUnitDialog``    — reparent an existing unit
 *  * ``AddMemberDialog``      — assign a member (with optional primary flag)
 *
 * All three lean on ``useOrgUnits`` for the mutation calls; ``AddMemberDialog``
 * additionally pulls the org-wide member list from ``useMembers`` so the
 * caller can pick from an existing dropdown rather than typing a UUID.
 */

const UNIT_TYPES: OrgUnitType[] = [
  'department',
  'team',
  'business_unit',
  'legal_entity',
  'cost_center',
  'custom',
]

function toMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.detail || `HTTP ${error.status}`
    : String(error)
}

/* ------------------------------------------------------------------------ */
/* Flatten a tree into "— ─ 名称" style options for the parent picker.       */
/* ------------------------------------------------------------------------ */
function flattenForPicker(
  tree: OrgUnitNode[],
  exclude: Set<string> = new Set(),
): Array<{ id: string; label: string }> {
  const rows: Array<{ id: string; label: string }> = []
  const visit = (nodes: OrgUnitNode[], depth: number) => {
    for (const node of nodes) {
      if (exclude.has(node.id)) continue
      const prefix = '— '.repeat(depth)
      rows.push({ id: node.id, label: `${prefix}${node.name}` })
      visit(node.children, depth + 1)
    }
  }
  visit(tree, 0)
  return rows
}

/** Collect ``root.id`` + every descendant id. */
function subtreeIds(node: OrgUnitNode): Set<string> {
  const ids = new Set<string>()
  const walk = (n: OrgUnitNode) => {
    ids.add(n.id)
    for (const c of n.children) walk(c)
  }
  walk(node)
  return ids
}

/* ========================================================================
 * CreateOrgUnitDialog
 * ==================================================================== */

interface CreateProps {
  open: boolean
  onClose: () => void
  parentId?: string | null
}

export function CreateOrgUnitDialog({ open, onClose, parentId = null }: CreateProps) {
  const { t } = useTranslation()
  const { tree, createUnit } = useOrgUnits()
  const [name, setName] = useState('')
  const [type, setType] = useState<OrgUnitType>('department')
  const [selectedParent, setSelectedParent] = useState<string | null>(parentId)
  const [code, setCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setName('')
      setType('department')
      setSelectedParent(parentId)
      setCode('')
      setError(null)
      setSubmitting(false)
    }
  }, [open, parentId])

  useEffect(() => {
    if (!open) return
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [open, submitting, onClose])

  const parentOptions = useMemo(() => flattenForPicker(tree), [tree])

  if (!open) return null

  const canSubmit = name.trim().length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await createUnit({
        name: name.trim(),
        type,
        parent_id: selectedParent,
        code: code.trim() || null,
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
          <h2>{t('orgUnits.create.title')}</h2>
          <button
            aria-label={t('orgUnits.create.cancel')}
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
            <span>{t('orgUnits.create.name')}</span>
            <input
              autoFocus
              onChange={(e) => setName(e.target.value)}
              placeholder={t('orgUnits.create.namePlaceholder')}
              type="text"
              value={name}
            />
          </label>
          <label>
            <span>{t('orgUnits.create.type')}</span>
            <select onChange={(e) => setType(e.target.value as OrgUnitType)} value={type}>
              {UNIT_TYPES.map((tp) => (
                <option key={tp} value={tp}>
                  {t(`orgUnits.type.${tp}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('orgUnits.create.parent')}</span>
            <select
              onChange={(e) => setSelectedParent(e.target.value || null)}
              value={selectedParent ?? ''}
            >
              <option value="">{t('orgUnits.create.parentRoot')}</option>
              {parentOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('orgUnits.create.code')}</span>
            <input
              onChange={(e) => setCode(e.target.value)}
              placeholder="ENG"
              type="text"
              value={code}
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
              {t('orgUnits.create.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('orgUnits.create.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ========================================================================
 * MoveOrgUnitDialog
 * ==================================================================== */

interface MoveProps {
  open: boolean
  onClose: () => void
  node: OrgUnitNode | null
}

export function MoveOrgUnitDialog({ open, onClose, node }: MoveProps) {
  const { t } = useTranslation()
  const { tree, moveUnit } = useOrgUnits()
  const [target, setTarget] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open && node) {
      setTarget(node.parent_id)
      setError(null)
      setSubmitting(false)
    }
  }, [open, node])

  const exclude = useMemo(() => (node ? subtreeIds(node) : new Set<string>()), [node])
  const options = useMemo(() => flattenForPicker(tree, exclude), [tree, exclude])

  if (!open || !node) return null

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try {
      await moveUnit(node.id, target)
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
          <h2>{t('orgUnits.move.title', { name: node.name })}</h2>
          <button
            aria-label={t('orgUnits.move.cancel')}
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
            <span>{t('orgUnits.move.newParent')}</span>
            <select
              onChange={(e) => setTarget(e.target.value || null)}
              value={target ?? ''}
            >
              <option value="">{t('orgUnits.create.parentRoot')}</option>
              {options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <p className="dialog-hint">{t('orgUnits.move.hint')}</p>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('orgUnits.move.cancel')}
            </button>
            <button className="primary-button" disabled={submitting} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('orgUnits.move.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ========================================================================
 * AddMemberDialog
 * ==================================================================== */

interface AddMemberProps {
  open: boolean
  onClose: () => void
  unitId: string | null
}

export function AddMemberDialog({ open, onClose, unitId }: AddMemberProps) {
  const { t } = useTranslation()
  const { addMember, members: memberList } = useOrgUnits()
  const { state: allMembers } = useMembers()
  const [userId, setUserId] = useState('')
  const [makePrimary, setMakePrimary] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const alreadyMember = useMemo(() => {
    if (memberList.kind !== 'ready') return new Set<string>()
    return new Set(memberList.items.map((m) => m.user_id))
  }, [memberList])

  const candidates = useMemo(() => {
    if (allMembers.kind !== 'ready') return []
    return allMembers.items.filter((m) => isAssignable(m) && !alreadyMember.has(m.id))
  }, [allMembers, alreadyMember])

  useEffect(() => {
    if (open) {
      setUserId(candidates[0]?.id ?? '')
      setMakePrimary(false)
      setError(null)
      setSubmitting(false)
    }
  }, [open, candidates])

  if (!open || !unitId) return null

  const canSubmit = userId.length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await addMember(unitId, userId, makePrimary)
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
          <h2>{t('orgUnits.addMember.title')}</h2>
          <button
            aria-label={t('orgUnits.addMember.cancel')}
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
            <span>{t('orgUnits.addMember.user')}</span>
            <select
              onChange={(e) => setUserId(e.target.value)}
              value={userId}
            >
              {candidates.length === 0 && (
                <option value="">{t('orgUnits.addMember.noCandidates')}</option>
              )}
              {candidates.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name} · {m.email}
                </option>
              ))}
            </select>
          </label>
          <label className="checkbox-row">
            <input
              checked={makePrimary}
              onChange={(e) => setMakePrimary(e.target.checked)}
              type="checkbox"
            />
            <span>{t('orgUnits.addMember.makePrimary')}</span>
          </label>
          <p className="dialog-hint">{t('orgUnits.addMember.hint')}</p>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('orgUnits.addMember.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('orgUnits.addMember.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
