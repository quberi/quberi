import { Loader2, X } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { createAccessRequest } from '../accessRequests/api'

/** An administrator asks the owner of an enterprise space for access. */
export function RequestAccessDialog({
  spaceId,
  spaceName,
  onClose,
  onDone,
}: {
  spaceId: string
  spaceName: string
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [role, setRole] = useState<'viewer' | 'editor'>('viewer')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!credentials || !reason.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await createAccessRequest(credentials, { space_id: spaceId, role, reason: reason.trim() })
      await onDone()
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card">
        <header className="dialog-header">
          <h2>{t('accessRequests.dialog.title', { space: spaceName })}</h2>
          <button
            aria-label={t('accessRequests.dialog.cancel')}
            className="dialog-close"
            disabled={busy}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form className="dialog-form" onSubmit={(e) => void submit(e)}>
          <p className="dialog-hint">{t('accessRequests.dialog.hint')}</p>
          <label>
            <span>{t('accessRequests.dialog.role')}</span>
            <select onChange={(e) => setRole(e.target.value as 'viewer' | 'editor')} value={role}>
              <option value="viewer">{t('accessRequests.role.viewer')}</option>
              <option value="editor">{t('accessRequests.role.editor')}</option>
            </select>
          </label>
          <label>
            <span>{t('accessRequests.dialog.reason')}</span>
            <textarea
              maxLength={500}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t('accessRequests.dialog.reasonPlaceholder')}
              rows={3}
              value={reason}
            />
          </label>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button className="secondary-button" disabled={busy} onClick={onClose} type="button">
              {t('accessRequests.dialog.cancel')}
            </button>
            <button className="primary-button" disabled={!reason.trim() || busy} type="submit">
              {busy ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('accessRequests.dialog.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
