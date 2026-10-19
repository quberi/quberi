import { AlertTriangle, Loader2, UserX, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../auth/AuthContext'
import './offboarding.css'
import { getOffboardingChecklist } from './api'
import { offboardingErrorText } from './memberErrors'
import { useMembers } from './MembersContext'
import type { MemberSummary, OffboardingChecklist } from './types'
import { useDialogFocus } from './useDialogFocus'

interface Props {
  member: MemberSummary
  onClose: () => void
  /** Called after the member was deactivated, so the page can offer the handover next. */
  onDone: (member: MemberSummary) => void
}

/**
 * Confirm deactivating a member. It first reads the offboarding checklist so the owner
 * sees what will happen before agreeing: sessions end, outside-tool and Feishu access go,
 * private model keys are deleted, the personal space is frozen.
 */
export function DeactivateMemberDialog({ member, onClose, onDone }: Props) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { deactivate } = useMembers()
  const [checklist, setChecklist] = useState<OffboardingChecklist | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  useDialogFocus(cardRef, onClose, !submitting)
  const tRef = useRef(t)
  tRef.current = t

  useEffect(() => {
    if (!credentials) return
    let cancelled = false
    getOffboardingChecklist(credentials, member.id)
      .then((result) => {
        if (!cancelled) setChecklist(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(offboardingErrorText(tRef.current, err))
      })
    return () => {
      cancelled = true
    }
  }, [credentials, member.id])

  const impact = checklist?.impact
  const embeddingBlocked = impact?.org_selections_referencing_private_keys.includes('embedding') ?? false
  const chatFallback = impact?.org_selections_referencing_private_keys.includes('chat') ?? false
  const canConfirm = checklist !== null && !embeddingBlocked && !submitting

  const confirm = async () => {
    if (!canConfirm) return
    setSubmitting(true)
    setError(null)
    try {
      const result = await deactivate(member.id)
      onDone(result.member)
    } catch (err) {
      setError(offboardingErrorText(t, err))
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        aria-labelledby="deactivate-title"
        aria-modal="true"
        className="dialog-card dialog-card-wide offboarding-card"
        ref={cardRef}
        role="dialog"
        tabIndex={-1}
      >
        <header className="dialog-header">
          <h2 id="deactivate-title">{t('deactivateDialog.title', { name: member.display_name })}</h2>
          <button
            aria-label={t('deactivateDialog.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <p className="dialog-hint">{t('deactivateDialog.intro', { name: member.display_name })}</p>

        {checklist === null && !loadError && (
          <div className="state-panel" role="status">
            <Loader2 className="spin" size={14} /> {t('deactivateDialog.loading')}
          </div>
        )}
        {loadError && (
          <div className="dialog-error" role="alert">
            {t('deactivateDialog.loadError')}: {loadError}
          </div>
        )}

        {impact && (
          <ul className="offboarding-effects">
            <li>{t('deactivateDialog.sessions')}</li>
            <li>{t('deactivateDialog.oauth', { count: impact.oauth_grants })}</li>
            <li>
              {t('deactivateDialog.feishu', {
                tokens: impact.feishu_tokens,
                sources: impact.feishu_sources,
              })}
            </li>
            <li>{t('deactivateDialog.byok', { count: impact.private_model_bindings })}</li>
            <li>{t('deactivateDialog.personal')}</li>
            <li>{t('deactivateDialog.keeps')}</li>
            {checklist.needs_transfer_total > 0 && (
              <li>
                {t('deactivateDialog.needsTransfer', { count: checklist.needs_transfer_total })}
              </li>
            )}
          </ul>
        )}

        {impact && impact.private_model_bindings > 0 && (
          <p className="offboarding-warning" role="note">
            <AlertTriangle aria-hidden="true" size={15} />
            <span>{t('deactivateDialog.byokGlobal')}</span>
          </p>
        )}
        {chatFallback && (
          <p className="offboarding-warning" role="note">
            <AlertTriangle aria-hidden="true" size={15} />
            <span>{t('deactivateDialog.orgChatFallback')}</span>
          </p>
        )}
        {embeddingBlocked && (
          <p className="offboarding-blocked" role="alert">
            <AlertTriangle aria-hidden="true" size={15} />
            <span>{t('deactivateDialog.embeddingBlocked')}</span>
          </p>
        )}
        {impact && <p className="dialog-hint">{t('deactivateDialog.restoreNote')}</p>}

        {error && (
          <div className="dialog-error" role="alert">
            {error}
          </div>
        )}
        <div className="dialog-actions">
          <button className="secondary-button" disabled={submitting} onClick={onClose} type="button">
            {t('deactivateDialog.cancel')}
          </button>
          <button
            className="primary-button offboarding-danger"
            disabled={!canConfirm}
            onClick={() => void confirm()}
            type="button"
          >
            {submitting ? <Loader2 className="spin" size={16} /> : <UserX size={16} />}
            <span>{t('deactivateDialog.confirm')}</span>
          </button>
        </div>
      </div>
    </div>
  )
}
