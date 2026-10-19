import { Loader2, Send, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../auth/AuthContext'
import './offboarding.css'
import { getOffboardingChecklist, transferSpaces } from './api'
import { offboardingErrorText } from './memberErrors'
import { useMembers } from './MembersContext'
import type {
  MemberSummary,
  OffboardingChecklist,
  TransferItemResult,
  TransferSpacesResponse,
} from './types'
import { useDialogFocus } from './useDialogFocus'

interface Props {
  member: MemberSummary
  onClose: () => void
}

/**
 * What a departing member still holds, and a batch handover of their collaboration spaces.
 * Spaces are handed over only once the member is deactivated. Each space is reported on
 * its own: a failure does not undo the others, and sending the same request again is safe.
 */
export function OffboardingDialog({ member, onClose }: Props) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { state: members } = useMembers()
  const [checklist, setChecklist] = useState<OffboardingChecklist | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [newOwner, setNewOwner] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<TransferSpacesResponse | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  useDialogFocus(cardRef, onClose, !submitting)
  // Read at call time, so a changing ``t`` does not re-run the load.
  const tRef = useRef(t)
  tRef.current = t

  const load = useCallback(async () => {
    if (!credentials) return
    try {
      const result = await getOffboardingChecklist(credentials, member.id)
      setChecklist(result)
      setLoadError(null)
      // Keep the choice only for spaces that are still listed.
      setSelected((prev) => {
        const still = new Set(result.owned_spaces.map((s) => s.space_id))
        return new Set([...prev].filter((id) => still.has(id)))
      })
    } catch (err) {
      setLoadError(offboardingErrorText(tRef.current, err))
    }
  }, [credentials, member.id])

  useEffect(() => {
    void load()
  }, [load])

  // Anyone active, other than the departing member. The organisation owner is excluded
  // too: the server refuses to make them a space owner.
  const candidates = useMemo(
    () =>
      members.kind === 'ready'
        ? members.items.filter(
            (m) => m.status === 'active' && m.role_code !== 'owner' && m.id !== member.id,
          )
        : [],
    [members, member.id],
  )

  const suspended = checklist?.member.status === 'suspended'
  // Names of every space seen so far: after a handover the reloaded list no longer has them,
  // but the result list still has to say which space each line is about.
  const seenNames = useRef(new Map<string, string>())
  for (const s of checklist?.owned_spaces ?? []) seenNames.current.set(s.space_id, s.name)
  const names = seenNames.current
  const canSubmit = suspended && newOwner !== '' && selected.size > 0 && !submitting

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const submit = async () => {
    if (!credentials || !canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      const result = await transferSpaces(credentials, member.id, newOwner, [...selected])
      setReport(result)
      await load()
    } catch (err) {
      setError(offboardingErrorText(t, err))
    } finally {
      setSubmitting(false)
    }
  }

  const resultLabel = (r: TransferItemResult) =>
    r.code
      ? `${t(`offboarding.result.${r.status}`)}: ${t(`offboarding.code.${r.code}`, { defaultValue: r.code })}`
      : t(`offboarding.result.${r.status}`)

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        aria-labelledby="offboarding-title"
        aria-modal="true"
        className="dialog-card dialog-card-wide offboarding-card"
        ref={cardRef}
        role="dialog"
        tabIndex={-1}
      >
        <header className="dialog-header">
          <h2 id="offboarding-title">{t('offboarding.title', { name: member.display_name })}</h2>
          <button
            aria-label={t('offboarding.close')}
            className="dialog-close"
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>

        {checklist === null && !loadError && (
          <div className="state-panel" role="status">
            <Loader2 className="spin" size={14} /> {t('offboarding.loading')}
          </div>
        )}
        {loadError && (
          <div className="dialog-error" role="alert">
            {t('offboarding.loadError')}: {loadError}
          </div>
        )}

        {checklist && (
          <div className="offboarding-body">
            {!suspended && (
              <p className="offboarding-warning" role="note">
                {t('offboarding.notSuspended')}
              </p>
            )}

            <section aria-labelledby="off-owned">
              <h3 id="off-owned">
                {t('offboarding.ownedTitle', { count: checklist.owned_spaces_total })}
              </h3>
              {checklist.owned_spaces.length === 0 ? (
                <p className="dialog-hint">{t('offboarding.ownedEmpty')}</p>
              ) : (
                <ul className="offboarding-list">
                  {checklist.owned_spaces.map((s) => (
                    <li key={s.space_id}>
                      <label>
                        <input
                          checked={selected.has(s.space_id)}
                          disabled={!suspended || submitting}
                          onChange={() => toggle(s.space_id)}
                          type="checkbox"
                        />
                        <span className="offboarding-item-name">{s.name}</span>
                      </label>
                      <small>
                        {s.needs_transfer
                          ? t('offboarding.needsOwner')
                          : t('offboarding.hasOtherOwner', {
                              names: s.other_owners.map((o) => o.display_name).join('、'),
                            })}
                        {s.pending_review_count > 0 &&
                          ` · ${t('offboarding.pendingGoesAlong', { count: s.pending_review_count })}`}
                      </small>
                    </li>
                  ))}
                </ul>
              )}
              {checklist.owned_spaces_total > checklist.owned_spaces.length && (
                <p className="dialog-hint">
                  {t('offboarding.moreHidden', {
                    count: checklist.owned_spaces_total - checklist.owned_spaces.length,
                  })}
                </p>
              )}
              {checklist.owned_spaces.length > 0 && (
                <div className="offboarding-transfer">
                  <label>
                    <span>{t('offboarding.newOwner')}</span>
                    <select
                      disabled={!suspended || submitting}
                      onChange={(e) => setNewOwner(e.target.value)}
                      value={newOwner}
                    >
                      <option value="">{t('offboarding.chooseOwner')}</option>
                      {candidates.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.display_name} · {m.email}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="primary-button"
                    disabled={!canSubmit}
                    onClick={() => void submit()}
                    type="button"
                  >
                    {submitting ? <Loader2 className="spin" size={16} /> : <Send size={16} />}
                    <span>{t('offboarding.transfer', { count: selected.size })}</span>
                  </button>
                </div>
              )}
              {error && (
                <div className="dialog-error" role="alert">
                  {error}
                </div>
              )}
            </section>

            {report && (
              <section aria-labelledby="off-results" className="offboarding-results">
                <h3 id="off-results">{t('offboarding.resultsTitle')}</h3>
                <p role="status">
                  {t('offboarding.resultsSummary', {
                    transferred: report.transferred,
                    already: report.already_done,
                    skipped: report.skipped,
                    failed: report.failed,
                  })}
                </p>
                <ul className="offboarding-list">
                  {report.results.map((r) => (
                    <li className={`offboarding-result result-${r.status}`} key={r.space_id}>
                      <span className="offboarding-item-name">
                        {names.get(r.space_id) ?? r.space_id}
                      </span>
                      <small>{resultLabel(r)}</small>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section aria-labelledby="off-reviewer">
              <h3 id="off-reviewer">
                {t('offboarding.reviewerTitle', { count: checklist.reviewer_spaces_total })}
              </h3>
              {checklist.reviewer_spaces.length === 0 ? (
                <p className="dialog-hint">{t('offboarding.reviewerEmpty')}</p>
              ) : (
                <ul className="offboarding-list">
                  {checklist.reviewer_spaces.map((s) => (
                    <li key={s.space_id}>
                      <span className="offboarding-item-name">{s.name}</span>
                      <small>
                        {s.other_reviewers === 0 ? (
                          <strong className="offboarding-noreviewer">
                            {t('offboarding.noReviewer')}
                          </strong>
                        ) : (
                          t('offboarding.otherReviewers', { count: s.other_reviewers })
                        )}
                        {s.pending_review_count > 0 &&
                          ` · ${t('offboarding.pendingCount', { count: s.pending_review_count })}`}
                      </small>
                    </li>
                  ))}
                </ul>
              )}
              {checklist.reviewer_spaces_total > checklist.reviewer_spaces.length && (
                <p className="dialog-hint">
                  {t('offboarding.moreHidden', {
                    count: checklist.reviewer_spaces_total - checklist.reviewer_spaces.length,
                  })}
                </p>
              )}
              <p className="dialog-hint">{t('offboarding.reviewerHint')}</p>
            </section>

            <section aria-labelledby="off-uploads">
              <h3 id="off-uploads">
                {t('offboarding.uploadsTitle', { count: checklist.uploads_as_uploader_total })}
              </h3>
              {checklist.uploads_as_uploader.length === 0 ? (
                <p className="dialog-hint">{t('offboarding.uploadsEmpty')}</p>
              ) : (
                <ul className="offboarding-list">
                  {checklist.uploads_as_uploader.map((u) => (
                    <li key={u.document_id}>
                      <span className="offboarding-item-name">{u.name}</span>
                      <small>
                        {u.space_name} ·{' '}
                        {u.reviewer_count === 0 ? (
                          <strong className="offboarding-noreviewer">
                            {t('offboarding.noReviewer')}
                          </strong>
                        ) : (
                          t('offboarding.reviewers', { count: u.reviewer_count })
                        )}
                      </small>
                    </li>
                  ))}
                </ul>
              )}
              {checklist.uploads_as_uploader_total > checklist.uploads_as_uploader.length && (
                <p className="dialog-hint">
                  {t('offboarding.moreHidden', {
                    count:
                      checklist.uploads_as_uploader_total - checklist.uploads_as_uploader.length,
                  })}
                </p>
              )}
              <p className="dialog-hint">{t('offboarding.uploadsHint')}</p>
            </section>
          </div>
        )}

        <div className="dialog-actions">
          <button className="secondary-button" onClick={onClose} type="button">
            {t('offboarding.close')}
          </button>
        </div>
      </div>
    </div>
  )
}
