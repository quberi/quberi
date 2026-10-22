import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { googleRedirectError } from '../auth/googleRedirect'
import { SignIn } from '../auth/SignIn'

import {
  authorize,
  enableExternalTools,
  getConsent,
  type AuthorizeParams,
  type ConsentView,
} from './api'
import { ConsentCard } from './ConsentCard'
import { LanguageSwitch } from './LanguageSwitch'
import { deniedAddress, readAuthorizeParams } from './params'

type Load =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; view: ConsentView }
  | { kind: 'done' }

const readErr = (err: unknown) =>
  err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

/**
 * Where a tool (e.g. DeepSeek Harness) sends the browser to ask for access.
 *
 * Sign in if needed, pick the organisation whose knowledge the tool may read,
 * then allow or cancel. The server has already checked the tool and its return
 * address, and the only address this page ever navigates to is the one the
 * server returned - never one read from this page's own URL.
 */
export function OAuthConsent({
  search = window.location.search,
  navigate = (address: string) => window.location.assign(address),
}: {
  /** The query string of the address the tool opened (injectable for tests). */
  search?: string
  navigate?: (address: string) => void
} = {}) {
  const { credentials, isGuest } = useAuth()
  const params = readAuthorizeParams(search)

  if (params === null) return <Shell><InvalidRequest /></Shell>
  if (!credentials || isGuest) return <SignIn initialError={googleRedirectError()} />
  return (
    <Shell>
      <Consent params={params} navigate={navigate} />
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="oauth-page">
      <LanguageSwitch />
      {children}
    </main>
  )
}

function InvalidRequest({ message }: { message?: string }) {
  const { t } = useTranslation()
  return (
    <div className="oauth-card oauth-card-narrow">
      <h1>{t('oauth.invalid.title')}</h1>
      <div className="state-panel error-state">{message ?? t('oauth.invalid.body')}</div>
    </div>
  )
}

function Consent({
  params,
  navigate,
}: {
  params: AuthorizeParams
  navigate: (address: string) => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [load, setLoad] = useState<Load>({ kind: 'loading' })
  const [orgId, setOrgId] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // The page can ask twice (the credentials object is replaced once the profile loads). Only the
  // newest question may change what is shown: an older one that finishes later, or times out,
  // must not overwrite a good answer or a clear error with its own.
  const latest = useRef(0)
  const reload = async () => {
    if (!credentials) return
    const mine = ++latest.current
    try {
      const view = await getConsent(credentials, params)
      if (mine !== latest.current) return
      setLoad({ kind: 'ready', view })
      setOrgId((current) => {
        if (current && view.organizations.some((o) => o.organization_id === current)) {
          return current
        }
        const usable = view.organizations.filter((o) => o.external_tools_enabled)
        const preferred = usable.find((o) => o.is_current) ?? usable[0]
        return preferred?.organization_id ?? ''
      })
    } catch (err) {
      if (mine !== latest.current) return
      setLoad({ kind: 'error', message: readErr(err) })
    }
  }

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credentials])

  if (load.kind === 'loading') {
    return <div className="oauth-card oauth-card-narrow"><div className="state-panel">{t('oauth.loading')}</div></div>
  }
  if (load.kind === 'error') return <InvalidRequest message={load.message} />
  if (load.kind === 'done') {
    return (
      <div className="oauth-card oauth-card-narrow">
        <h1>{t('oauth.done.title')}</h1>
        <p>{t('oauth.done.body')}</p>
      </div>
    )
  }

  const { view } = load
  const chosen = view.organizations.find((o) => o.organization_id === orgId)

  const allow = async () => {
    if (!credentials || !chosen || busy) return
    setBusy(true)
    setError(null)
    try {
      const { redirect_to } = await authorize(credentials, params, chosen.organization_id)
      setLoad({ kind: 'done' })
      navigate(redirect_to)
    } catch (err) {
      setError(readErr(err))
      setBusy(false)
    }
  }

  const deny = () => {
    setLoad({ kind: 'done' })
    navigate(deniedAddress(view.redirect_uri, params.state))
  }

  const turnOn = async (organizationId: string) => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      await enableExternalTools(credentials, organizationId)
      await reload()
      setOrgId(organizationId)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <ConsentCard
      view={view}
      selectedOrgId={orgId}
      busy={busy}
      error={error}
      onSelect={setOrgId}
      onTurnOn={(id) => void turnOn(id)}
      onAllow={() => void allow()}
      onCancel={deny}
    />
  )
}
