import { Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useFormatDate } from '../../i18n/formatDate'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import { listExternalAccess, revokeExternalAccess, type ExternalGrant } from './api'

/**
 * Outside tools the signed-in person has connected (DeepSeek Harness, ...),
 * with a button to disconnect each. Disconnecting works at once.
 */
export function ConnectedTools() {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const { credentials } = useAuth()
  const [items, setItems] = useState<ExternalGrant[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  const load = useCallback(async () => {
    if (!credentials) return
    try {
      setItems((await listExternalAccess(credentials)).items)
    } catch (err) {
      setError(readErr(err))
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  const disconnect = async (grant: ExternalGrant) => {
    if (!credentials || busyId) return
    const who = grant.caller_label
      ? `${grant.client_name} (${grant.caller_label})`
      : grant.client_name
    if (!window.confirm(t('connectedTools.confirm', { client: who }))) return
    setBusyId(grant.id)
    setError(null)
    try {
      await revokeExternalAccess(credentials, grant.id)
      await load()
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusyId(null)
    }
  }

  // Nothing connected and nothing wrong: stay out of the way.
  if (items !== null && items.length === 0 && !error) return null

  return (
    <>
    <div className="account-methods-head">
      <h2>{t('connectedTools.title')}</h2>
      <p>{t('connectedTools.hint')}</p>
      {error && <div className="state-panel error-state">{error}</div>}
      <ul className="integration-source-list">
        {(items ?? []).map((g) => (
          <li key={g.id} className="integration-source-row integration-source-row-no-mark">
            <div className="integration-source-body">
              <div className="connected-tool-title">
                <strong>{g.client_name}</strong>
                {g.caller_label && (
                  <span className="connected-tool-caller" title={t('connectedTools.callerHint')}>
                    {g.caller_label}
                  </span>
                )}
                {g.scope.split(/\s+/).includes('knowledge:write') && (
                  <span className="oauth-write-badge">{t('oauth.writeBadge')}</span>
                )}
                {g.scope.split(/\s+/).includes('knowledge:upload') && (
                  <span className="oauth-write-badge">{t('oauth.uploadBadge')}</span>
                )}
              </div>
              <small className="integration-source-meta">
                <span>{g.organization_name}</span>
                <span className="dot-sep">·</span>
                <span>{t('connectedTools.authorizedAt', { when: formatDate(g.created_at) })}</span>
                <span className="dot-sep">·</span>
                <span>
                  {g.last_used_at
                    ? t('connectedTools.lastUsed', { when: formatDate(g.last_used_at) })
                    : t('connectedTools.neverUsed')}
                </span>
              </small>
            </div>
            <div className="integration-source-actions">
              <button
                className="secondary-button icon-button"
                disabled={busyId !== null}
                onClick={() => void disconnect(g)}
                type="button"
              >
                <Trash2 size={13} />
                <span>{t('connectedTools.disconnect')}</span>
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
    <div className="account-divider" />
    </>
  )
}
