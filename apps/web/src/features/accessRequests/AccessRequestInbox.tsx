import { Check, Loader2, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useFormatDate } from '../../i18n/formatDate'
import { decideAccessRequest, listRequestsToDecide, type AccessRequest } from './api'

function messageOf(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

/**
 * Requests for access that wait for this person, as an owner of the space.
 * Shows nothing when there are none, so it can sit at the top of any page.
 */
export function AccessRequestInbox({ onDecided }: { onDecided?: () => void }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const formatDate = useFormatDate()
  const [items, setItems] = useState<AccessRequest[]>([])
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!credentials || credentials.isGuest) return
    try {
      const list = await listRequestsToDecide(credentials)
      setItems(list.items)
    } catch {
      setItems([]) // a person with nothing to decide, or an older server: show nothing
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  const decide = async (request: AccessRequest, approve: boolean) => {
    if (!credentials || busyId) return
    setBusyId(request.id)
    setError(null)
    try {
      await decideAccessRequest(credentials, request.id, approve, notes[request.id] ?? '')
      await load()
      onDecided?.()
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusyId(null)
    }
  }

  if (items.length === 0) return null
  return (
    <section className="panel access-request-inbox">
      <div className="panel-heading">
        <div>
          <span className="section-label">{t('accessRequests.inboxTitle', { count: items.length })}</span>
          <p>{t('accessRequests.inboxHint')}</p>
        </div>
      </div>
      {error && <div className="dialog-error">{error}</div>}
      <ul className="access-request-list">
        {items.map((r) => (
          <li key={r.id}>
            <div className="access-request-main">
              <strong>
                {t('accessRequests.line', {
                  name: r.requester_name,
                  space: r.space_name,
                  role: t(`accessRequests.role.${r.role}`),
                })}
              </strong>
              <small>
                {r.requester_email} · {formatDate(r.created_at)}
              </small>
              <p>{r.reason}</p>
            </div>
            <div className="access-request-actions">
              <input
                aria-label={t('accessRequests.note')}
                maxLength={500}
                onChange={(e) => setNotes((n) => ({ ...n, [r.id]: e.target.value }))}
                placeholder={t('accessRequests.notePlaceholder')}
                value={notes[r.id] ?? ''}
              />
              <button
                className="secondary-button"
                disabled={busyId !== null}
                onClick={() => void decide(r, false)}
                type="button"
              >
                <X size={13} /> {t('accessRequests.reject')}
              </button>
              <button
                className="primary-button"
                disabled={busyId !== null}
                onClick={() => void decide(r, true)}
                type="button"
              >
                {busyId === r.id ? <Loader2 className="spin" size={13} /> : <Check size={13} />}
                {t('accessRequests.approve')}
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
