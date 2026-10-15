import { Plug } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { getOrganizationSettings, updateOrganizationSettings } from '../settings/api'

/**
 * Organisation switch: may members connect outside tools (e.g. DeepSeek
 * Harness) that read this organisation's knowledge? Off by default, because
 * those reads leave Quberi without the "check before sending" step.
 */
export function ExternalToolsCard() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  useEffect(() => {
    if (!credentials) return
    let cancelled = false
    getOrganizationSettings(credentials)
      .then((s) => {
        if (!cancelled) setEnabled(Boolean(s.allow_external_tools))
      })
      .catch((err) => {
        if (!cancelled) setError(readErr(err))
      })
    return () => {
      cancelled = true
    }
  }, [credentials])

  const toggle = async (next: boolean) => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      const s = await updateOrganizationSettings(credentials, { allow_external_tools: next })
      setEnabled(Boolean(s.allow_external_tools))
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <Plug size={15} /> {t('externalTools.title')}
          </span>
          <p>{t('externalTools.description')}</p>
        </div>
      </div>
      {enabled === null && !error && <div className="state-panel">{t('externalTools.loading')}</div>}
      {error && <div className="state-panel error-state">{error}</div>}
      {enabled !== null && (
        <>
          <label className="settings-model-vendoronly">
            <input
              checked={enabled}
              disabled={busy}
              onChange={(e) => void toggle(e.target.checked)}
              type="checkbox"
            />
            <span>{t('externalTools.toggleLabel')}</span>
          </label>
          <small>{t('externalTools.offEffect')}</small>
        </>
      )}
    </section>
  )
}
