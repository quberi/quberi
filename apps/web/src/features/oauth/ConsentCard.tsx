import { Check, Plug, ShieldCheck, Terminal, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ConsentView } from './api'
import { clientIcon } from './clientIcons'

/**
 * The look of the consent page. It holds no state and calls nothing: the
 * page that owns it passes what to show and what the buttons do, so the same
 * markup can be rendered on its own (tests, screenshots).
 */
export interface ConsentCardProps {
  view: ConsentView
  selectedOrgId: string
  busy: boolean
  error: string | null
  onSelect: (organizationId: string) => void
  onTurnOn: (organizationId: string) => void
  onAllow: () => void
  onCancel: () => void
}

export function ConsentCard({
  view,
  selectedOrgId,
  busy,
  error,
  onSelect,
  onTurnOn,
  onAllow,
  onCancel,
}: ConsentCardProps) {
  const { t } = useTranslation()
  const chosen = view.organizations.find((o) => o.organization_id === selectedOrgId)
  const canAllow = Boolean(chosen?.external_tools_enabled) && !busy
  const toolIcon = clientIcon(view.client_id)
  // Each power gets its own sentence, so the page never says less than the tool asked for.
  const scopes = view.scope.split(/\s+/)
  const canRead = scopes.includes('knowledge:read')
  const canSave = scopes.includes('knowledge:write')
  const canUpload = scopes.includes('knowledge:upload')
  const canChange = canSave || canUpload
  const abilities = [
    ...(canRead ? ['oauth.canRead'] : []),
    ...(canSave ? ['oauth.canSave'] : []),
    ...(canUpload ? ['oauth.canUpload'] : []),
  ]

  return (
    <div className="oauth-card">
      <div className="oauth-link" aria-hidden="true">
        <span className="oauth-tile oauth-tile-quberi">
          <img src="/logo.svg" alt="" />
        </span>
        <span className="oauth-link-line" />
        <span className={`oauth-tile oauth-tile-tool${toolIcon ? ' has-image' : ''}`}>
          {toolIcon ? <img src={toolIcon} alt="" /> : <Terminal size={22} />}
        </span>
      </div>

      <header className="oauth-head">
        <h1>{t('oauth.title', { client: view.client_name })}</h1>
        <p>{t('oauth.intro', { client: view.client_name })}</p>
      </header>

      <ul className="oauth-perms">
        {abilities.map((key) => (
          <li key={key}>
            <span className="oauth-perm-icon yes"><Check size={14} /></span>
            <span>{t(key)}</span>
          </li>
        ))}
        <li>
          <span className="oauth-perm-icon no"><X size={14} /></span>
          <span>{t(canChange ? 'oauth.cannotWriteOrUpload' : 'oauth.cannot')}</span>
        </li>
      </ul>

      <fieldset className="oauth-orgs" disabled={busy}>
        <legend>{t('oauth.chooseOrg')}</legend>
        {view.organizations.map((o) => {
          const selected = o.organization_id === selectedOrgId
          const name = o.tier === 'personal' ? t('oauth.personalSpace') : o.name
          return (
            <div
              key={o.organization_id}
              className={`oauth-org${selected ? ' selected' : ''}${o.external_tools_enabled ? '' : ' off'}`}
            >
              <label className="oauth-org-main">
                <input
                  checked={selected}
                  disabled={!o.external_tools_enabled}
                  name="organization"
                  onChange={() => onSelect(o.organization_id)}
                  type="radio"
                />
                <span className="oauth-org-name">{name}</span>
              </label>
              <span className="oauth-org-kind">
                {o.tier === 'personal' ? t('oauth.kindPersonal') : t('oauth.kindTeam')}
              </span>
              {!o.external_tools_enabled && (
                <div className="oauth-org-off">
                  <span>{o.can_enable ? t('oauth.offCanEnable') : t('oauth.offAskAdmin')}</span>
                  {o.can_enable && (
                    <button
                      className="oauth-turn-on"
                      onClick={() => onTurnOn(o.organization_id)}
                      type="button"
                    >
                      <Plug size={12} /> {t('oauth.turnOn')}
                    </button>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </fieldset>

      <div className="oauth-notice" role="note">
        <ShieldCheck size={15} aria-hidden="true" />
        <span>{t('oauth.leaves', { client: view.client_name })}</span>
      </div>

      {error && <div className="state-panel error-state">{error}</div>}

      <div className="oauth-actions">
        <button className="oauth-cancel" disabled={busy} onClick={onCancel} type="button">
          {t('oauth.cancel')}
        </button>
        <button
          className="primary-button oauth-allow"
          disabled={!canAllow}
          onClick={onAllow}
          type="button"
        >
          {t('oauth.allow')}
        </button>
      </div>
      <small className="oauth-foot">{t('oauth.revokeHint')}</small>
    </div>
  )
}
