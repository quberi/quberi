import { Check, CircleAlert, ShieldOff } from 'lucide-react'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'

import { clientIcon } from './clientIcons'
import { LanguageSwitch } from './LanguageSwitch'

/** The outcomes a tool may send the browser here with. Anything else is shown as a failure. */
export const OUTCOMES = ['ok', 'cancelled', 'org_off', 'failed'] as const
export type Outcome = (typeof OUTCOMES)[number]

export const CONNECTED_PATH = '/oauth/connected'

export function isConnectedRoute(pathname: string): boolean {
  return pathname === CONNECTED_PATH || pathname === `${CONNECTED_PATH}/`
}

/**
 * Read the outcome from the address. Only one of a fixed set of words is
 * accepted - nothing else in the address is ever displayed, so a crafted link
 * cannot put text of its own on this page.
 */
export function readOutcome(search: string): Outcome {
  const value = new URLSearchParams(search).get('status')
  return (OUTCOMES as readonly string[]).includes(value ?? '') ? (value as Outcome) : 'failed'
}

const ICON = {
  ok: Check,
  cancelled: ShieldOff,
  org_off: ShieldOff,
  failed: CircleAlert,
} as const

/**
 * Where the browser ends up once a tool's sign-in is over. The tool brings its
 * own window back to the front; this page only has to say what happened and
 * that this tab can be closed.
 */
export function OAuthConnected({
  search = window.location.search,
  close = () => window.close(),
}: {
  search?: string
  /** Try to close this tab. Browsers only allow it for tabs a script opened; otherwise nothing happens. */
  close?: () => void
} = {}) {
  const { t } = useTranslation()
  const outcome = readOutcome(search)
  const Icon = ICON[outcome]

  // Close the tab if the browser lets us; if not, the message says it is safe to close.
  useEffect(() => {
    if (outcome !== 'ok') return
    const timer = window.setTimeout(close, 2500)
    return () => window.clearTimeout(timer)
  }, [outcome, close])

  return (
    <main className="oauth-page">
      <LanguageSwitch />
      <div className={`oauth-card oauth-card-narrow oauth-result ${outcome === 'ok' ? 'ok' : 'bad'}`}>
        <span className="oauth-result-icon" aria-hidden="true">
          <Icon size={30} />
        </span>
        <h1>{t(`oauth.result.${outcome}.title`)}</h1>
        <p>{t(`oauth.result.${outcome}.body`)}</p>
        <div className="oauth-result-tool" aria-hidden="true">
          <img src={clientIcon('quberi-harness')} alt="" /> DeepSeek Harness
        </div>
        <small className="oauth-foot">{t('oauth.result.closeHint')}</small>
      </div>
    </main>
  )
}
