import { Cloud, Loader2, Plus, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import {
  registerSource as apiRegisterSource,
  syncSource as apiSyncSource,
} from '../documents/api'
import { useSpaces } from '../spaces/SpacesContext'
import { isWholeSpace } from '../spaces/types'

import { feishuAuthorizeUrl, feishuOAuthStatus } from './api'
import { FeishuFolderPicker } from './FeishuFolderPicker'

function toMessage(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

/**
 * Connect + register a Feishu folder as a document source.
 *
 * Reused in two places:
 * - the Settings › Integrations panel (org-level, pick target space), and
 * - a space's "add documents" surface (``fixedSpaceId`` set → the target
 *   space is implicit and the picker is hidden; ``autoSync`` runs the first
 *   sync inline so files land immediately).
 */
export function AddFeishuDialog({
  onClose,
  onCreated,
  fixedSpaceId,
  autoSync = false,
}: {
  onClose: () => void
  onCreated: () => Promise<void>
  fixedSpaceId?: string
  autoSync?: boolean
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { state: spacesState } = useSpaces()
  const spaces = spacesState.kind === 'ready' ? spacesState.items.filter(isWholeSpace) : []

  const [folderToken, setFolderToken] = useState('')
  const [folderLabel, setFolderLabel] = useState('')
  const [spaceId, setSpaceId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [connState, setConnState] = useState<
    'checking' | 'connected' | 'disconnected'
  >('checking')
  const [connecting, setConnecting] = useState(false)

  const checkStatus = useCallback(async () => {
    if (!credentials) return
    try {
      const s = await feishuOAuthStatus(credentials)
      setConnState(s.connected ? 'connected' : 'disconnected')
    } catch {
      setConnState('disconnected')
    }
  }, [credentials])

  useEffect(() => {
    void checkStatus()
  }, [checkStatus])

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (
        e.data &&
        typeof e.data === 'object' &&
        e.data.source === 'quberi-feishu-oauth'
      ) {
        setConnecting(false)
        void checkStatus()
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [checkStatus])

  const startConnect = async () => {
    if (!credentials) return
    setError(null)
    try {
      const { authorize_url } = await feishuAuthorizeUrl(credentials)
      setConnecting(true)
      window.open(authorize_url, 'quberi-feishu-oauth', 'width=520,height=680')
    } catch (err) {
      setError(toMessage(err))
    }
  }

  const effectiveSpaceId = fixedSpaceId || spaceId || spaces[0]?.id || ''
  // The folder must come from the picker (which lists only what this person can
  // open in Feishu), so connecting needs the person's own Feishu authorisation.
  const canSubmit =
    connState === 'connected' &&
    !!folderToken.trim() &&
    !!effectiveSpaceId &&
    !submitting

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!credentials || !canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      const source = await apiRegisterSource(credentials, {
        kind: 'feishu',
        // No name field: the connection is named after the folder. The server
        // adds a number if that name is already taken.
        name: t('integrations.feishu.autoName', {
          folder: (folderLabel || folderToken).slice(0, 150),
        }),
        caching_policy: 'reference_only',
        config: {
          folder_token: folderToken.trim(),
          target_space_id: effectiveSpaceId,
        },
      })
      if (autoSync) {
        setSyncing(true)
        try {
          await apiSyncSource(credentials, source.id)
        } catch (err) {
          // Source is created; surface sync failure but still finish.
          setError(t('integrations.feishu.syncFail', { message: toMessage(err) }))
        }
      }
      await onCreated()
    } catch (err) {
      // 428 = this person has not authorised Feishu (or the grant expired).
      if (err instanceof ApiError && err.status === 428) setConnState('disconnected')
      setError(toMessage(err))
      setSubmitting(false)
      setSyncing(false)
    }
  }


  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card dialog-card-wide">
        <header className="dialog-header">
          <h2>
            <Cloud size={16} /> {t('integrations.feishu.addTitle')}
          </h2>
          <button
            aria-label={t('integrations.feishu.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form className="dialog-form" onSubmit={submit}>
          <p className="dialog-hint">{t('integrations.feishu.addHint')}</p>

          {connState === 'checking' && (
            <div className="feishu-conn-banner">
              {t('integrations.feishu.oauth.checking')}
            </div>
          )}
          {connState === 'disconnected' && (
            <div className="feishu-conn-banner disconnected">
              <div>
                <strong>{t('integrations.feishu.oauth.notConnected')}</strong>
                <small>{t('integrations.feishu.oauth.notConnectedHint')}</small>
              </div>
              <button
                className="primary-button"
                disabled={connecting}
                onClick={() => void startConnect()}
                type="button"
              >
                {connecting
                  ? t('integrations.feishu.oauth.connecting')
                  : t('integrations.feishu.oauth.connect')}
              </button>
            </div>
          )}
          {connState === 'connected' && (
            <div className="feishu-conn-banner connected">
              {t('integrations.feishu.oauth.connected')}
            </div>
          )}


          {connState === 'connected' && (
            <div className="feishu-picker-block">
              <span className="feishu-picker-label">
                {t('integrations.feishu.folderLabel')}
              </span>
              {folderToken && (
                <div className="feishu-picker-chosen">
                  {t('integrations.feishu.picker.chosen', {
                    name: folderLabel || folderToken.slice(0, 14),
                  })}
                </div>
              )}
              <FeishuFolderPicker
                onNeedsAuth={() => setConnState('disconnected')}
                onSelect={(token, label) => {
                  setFolderToken(token)
                  setFolderLabel(label)
                }}
                selectedToken={folderToken || null}
              />
            </div>
          )}
          {!fixedSpaceId && (
            <>
              <label>
                <span>{t('integrations.feishu.spaceLabel')}</span>
                <select
                  onChange={(e) => setSpaceId(e.target.value)}
                  value={effectiveSpaceId}
                >
                  {spaces.length === 0 && (
                    <option value="">{t('integrations.feishu.noSpaces')}</option>
                  )}
                  {spaces.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </label>
              <p className="dialog-subhint">{t('integrations.feishu.spaceHelp')}</p>
            </>
          )}

          {error && <div className="dialog-error">{error}</div>}

          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('integrations.feishu.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? (
                <Loader2 size={14} className="spin" />
              ) : (
                <Plus size={14} />
              )}
              <span>
                {syncing
                  ? t('integrations.feishu.syncingNow')
                  : t('integrations.feishu.create')}
              </span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
