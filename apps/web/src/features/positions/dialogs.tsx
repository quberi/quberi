import { Loader2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useMembers } from '../members/MembersContext'
import { isAssignable } from '../spaceAccess/useSubjectCandidates'
import { useOrgUnits } from '../orgUnits/OrgUnitsContext'

import { usePositions } from './PositionsContext'

function toMessage(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

/* ========================================================================
 * CreatePositionDialog
 * ==================================================================== */

export function CreatePositionDialog({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const { t } = useTranslation()
  const { positions, createPosition } = usePositions()
  const { units } = useOrgUnits()

  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  const [grade, setGrade] = useState('')
  const [jobFamily, setJobFamily] = useState('')
  const [orgUnitId, setOrgUnitId] = useState<string | ''>('')
  const [reportsTo, setReportsTo] = useState<string | ''>('')
  const [headcount, setHeadcount] = useState<number>(1)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setName('')
      setCode('')
      setGrade('')
      setJobFamily('')
      setOrgUnitId('')
      setReportsTo('')
      setHeadcount(1)
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

  const unitOptions = units.kind === 'ready' ? units.items : []
  const positionOptions =
    positions.kind === 'ready' ? positions.items.filter((p) => p.status !== 'archived') : []

  if (!open) return null

  const canSubmit = name.trim().length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await createPosition({
        name: name.trim(),
        code: code.trim() || null,
        organization_unit_id: orgUnitId || null,
        reports_to_position_id: reportsTo || null,
        job_family: jobFamily.trim() || null,
        grade: grade.trim() || null,
        headcount_budget: headcount,
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
          <h2>{t('positions.create.title')}</h2>
          <button
            aria-label={t('positions.create.cancel')}
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
            <span>{t('positions.create.name')}</span>
            <input
              autoFocus
              onChange={(e) => setName(e.target.value)}
              placeholder={t('positions.create.namePlaceholder')}
              type="text"
              value={name}
            />
          </label>
          <label>
            <span>{t('positions.create.code')}</span>
            <input
              onChange={(e) => setCode(e.target.value)}
              placeholder="AI-LEAD"
              type="text"
              value={code}
            />
          </label>
          <div className="dialog-form-row">
            <label>
              <span>{t('positions.create.grade')}</span>
              <input
                onChange={(e) => setGrade(e.target.value)}
                placeholder="P6"
                type="text"
                value={grade}
              />
            </label>
            <label>
              <span>{t('positions.create.family')}</span>
              <input
                onChange={(e) => setJobFamily(e.target.value)}
                placeholder="engineering"
                type="text"
                value={jobFamily}
              />
            </label>
          </div>
          <label>
            <span>{t('positions.create.orgUnit')}</span>
            <select
              onChange={(e) => setOrgUnitId(e.target.value)}
              value={orgUnitId}
            >
              <option value="">{t('positions.create.orgUnitNone')}</option>
              {unitOptions.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('positions.create.reportsTo')}</span>
            <select
              onChange={(e) => setReportsTo(e.target.value)}
              value={reportsTo}
            >
              <option value="">{t('positions.create.reportsToNone')}</option>
              {positionOptions.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('positions.create.headcount')}</span>
            <input
              min={0}
              onChange={(e) => setHeadcount(Number(e.target.value))}
              type="number"
              value={headcount}
            />
          </label>
          <p className="dialog-hint">{t('positions.create.hint')}</p>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('positions.create.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('positions.create.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ========================================================================
 * AssignOccupantDialog
 * ==================================================================== */

export function AssignOccupantDialog({
  open,
  onClose,
  positionId,
}: {
  open: boolean
  onClose: () => void
  positionId: string
}) {
  const { t } = useTranslation()
  const { assignOccupant, occupants } = usePositions()
  const { state: allMembers } = useMembers()
  const [userId, setUserId] = useState('')
  const [isPrimary, setIsPrimary] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const currentUserIds = useMemo(() => {
    if (occupants.kind !== 'ready') return new Set<string>()
    return new Set(occupants.items.map((o) => o.user_id))
  }, [occupants])

  const candidates = useMemo(() => {
    if (allMembers.kind !== 'ready') return []
    return allMembers.items.filter((m) => isAssignable(m) && !currentUserIds.has(m.id))
  }, [allMembers, currentUserIds])

  useEffect(() => {
    if (open) {
      setUserId(candidates[0]?.id ?? '')
      setIsPrimary(false)
      setError(null)
      setSubmitting(false)
    }
  }, [open, candidates])

  if (!open) return null

  const canSubmit = userId.length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await assignOccupant(positionId, { user_id: userId, is_primary: isPrimary })
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
          <h2>{t('positions.assignDialog.title')}</h2>
          <button
            aria-label={t('positions.assignDialog.cancel')}
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
            <span>{t('positions.assignDialog.user')}</span>
            <select onChange={(e) => setUserId(e.target.value)} value={userId}>
              {candidates.length === 0 && (
                <option value="">{t('positions.assignDialog.noCandidates')}</option>
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
              checked={isPrimary}
              onChange={(e) => setIsPrimary(e.target.checked)}
              type="checkbox"
            />
            <span>{t('positions.assignDialog.makePrimary')}</span>
          </label>
          <p className="dialog-hint">{t('positions.assignDialog.hint')}</p>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('positions.assignDialog.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('positions.assignDialog.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
