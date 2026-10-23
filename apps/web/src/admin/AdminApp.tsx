import {
  Activity,
  AlertTriangle,
  Cpu,
  LayoutDashboard,
  LogOut,
  Plug,
  RefreshCw,
  ShieldCheck,
  Users,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import {
  AdminApiError,
  adminLogin,
  changeAdminPassword,
  getModelCatalog,
  getOrganizations,
  getOverview,
  type ModelCatalogEntry,
  type OrganizationStatsRow,
  type PlatformOverview,
} from './api'
import { ClientsView } from './ClientsView'
import { type MessageKey, useLang } from './i18n'
import { errorMessage } from './i18n/errors'
import { LangSwitch } from './i18n/LangSwitch'
import { ModelsView } from './ModelsView'
import { OrganizationsView } from './OrganizationsView'
import { OverviewView } from './OverviewView'
import { TrafficView } from './TrafficView'

const TOKEN_KEY = 'quberi.admin.token'
// Set while the signed-in account still has to choose a new password.
const MUST_CHANGE_KEY = 'quberi.admin.mustChange'

// Password length rules the API enforces (services/platform_admin.py); shown in the hint.
const PASSWORD_MIN = 12
const PASSWORD_MAX = 128

type AdminView = 'overview' | 'organizations' | 'models' | 'clients' | 'traffic'

const NAV_ITEMS: { key: AdminView; labelKey: MessageKey; icon: typeof LayoutDashboard }[] = [
  { key: 'overview', labelKey: 'nav.overview', icon: LayoutDashboard },
  { key: 'organizations', labelKey: 'nav.organizations', icon: Users },
  { key: 'models', labelKey: 'nav.models', icon: Cpu },
  { key: 'clients', labelKey: 'nav.clients', icon: Plug },
  { key: 'traffic', labelKey: 'nav.traffic', icon: Activity },
]

export function AdminApp() {
  // Applies the stored language (document lang + title) on the very first render.
  useLang()
  const [token, setToken] = useState<string | null>(() =>
    window.sessionStorage.getItem(TOKEN_KEY),
  )
  const [mustChange, setMustChange] = useState<boolean>(
    () => window.sessionStorage.getItem(MUST_CHANGE_KEY) === '1',
  )

  const start = (t: string, change: boolean) => {
    window.sessionStorage.setItem(TOKEN_KEY, t)
    if (change) window.sessionStorage.setItem(MUST_CHANGE_KEY, '1')
    else window.sessionStorage.removeItem(MUST_CHANGE_KEY)
    setToken(t)
    setMustChange(change)
  }
  // Stable across renders: the dashboard reloads its data when this changes, and a
  // language switch re-renders this component.
  const signOut = useCallback(() => {
    window.sessionStorage.removeItem(TOKEN_KEY)
    window.sessionStorage.removeItem(MUST_CHANGE_KEY)
    setToken(null)
    setMustChange(false)
  }, [])

  if (!token) {
    return <SignInGate onSignedIn={start} />
  }
  if (mustChange) {
    return (
      <ChangePasswordGate
        token={token}
        onChanged={(t) => start(t, false)}
        onSignOut={signOut}
      />
    )
  }
  return <Dashboard token={token} onSignOut={signOut} />
}

// ---------------------------------------------------------------------------
// Sign-in gate — username + password. (The deployment's admin token is still
// accepted by the API for scripts, but the console no longer offers it.)
// ---------------------------------------------------------------------------

function SignInGate({
  onSignedIn,
}: {
  onSignedIn: (token: string, mustChange: boolean) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  // The failure itself, not its text, so the message follows a language switch.
  const [failure, setFailure] = useState<{ err: unknown } | null>(null)
  const { t, tCode } = useLang()

  const failureText = (err: unknown): string => {
    if (err instanceof AdminApiError) {
      const coded = err.code ? tCode(err.code, err.params) : null
      if (coded !== null) return coded
      if (err.status === 429) return t('gate.signIn.tooMany')
      if (err.status === 401) return t('gate.signIn.invalid')
      if (err.code === 'validation') return t('error.validation')
    }
    return t('gate.signIn.unreachable')
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || !username.trim() || !password) return
    setBusy(true)
    setFailure(null)
    try {
      const session = await adminLogin(username.trim(), password)
      onSignedIn(session.token, session.must_change_password)
    } catch (err) {
      setFailure({ err })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin-gate">
      <div className="admin-gate-stack">
        <div className="admin-gate-card">
          <span className="admin-gate-mark" aria-hidden="true">
            <ShieldCheck size={22} />
          </span>
          <h1>{t('app.title')}</h1>
          <p>{t('gate.signIn.hint')}</p>
          <form onSubmit={submit}>
            <input
              aria-label={t('gate.signIn.username')}
              autoComplete="username"
              autoFocus
              onChange={(e) => setUsername(e.target.value)}
              placeholder={t('gate.signIn.username')}
              type="text"
              value={username}
            />
            <input
              aria-label={t('gate.signIn.password')}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t('gate.signIn.password')}
              type="password"
              value={password}
            />
            {failure && (
              <div className="admin-gate-error" role="alert">
                {failureText(failure.err)}
              </div>
            )}
            <button disabled={busy || !password || !username.trim()} type="submit">
              {busy ? t('gate.signIn.busy') : t('gate.signIn.submit')}
            </button>
          </form>
        </div>
        <LangSwitch />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Forced password change — shown after signing in with a password the
// operator did not choose. Nothing else in the console is reachable first.
// ---------------------------------------------------------------------------

function ChangePasswordGate({
  token,
  onChanged,
  onSignOut,
}: {
  token: string
  onChanged: (token: string) => void
  onSignOut: () => void
}) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  // The failure itself, not its text, so the message follows a language switch.
  const [failure, setFailure] = useState<'mismatch' | { err: unknown } | null>(null)
  const { t, tCode } = useLang()

  const failureText = (f: 'mismatch' | { err: unknown }): string => {
    if (f === 'mismatch') return t('gate.password.mismatch')
    const { err } = f
    // A server error that said nothing useful keeps this gate's own wording.
    if (
      err instanceof AdminApiError &&
      !err.detail &&
      err.code !== 'validation' &&
      (!err.code || tCode(err.code, err.params) === null)
    ) {
      return t('gate.password.failed')
    }
    return errorMessage(err, tCode, t)
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (next !== confirm) {
      setFailure('mismatch')
      return
    }
    setBusy(true)
    setFailure(null)
    try {
      const session = await changeAdminPassword(token, current, next)
      onChanged(session.token)
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 401) {
        onSignOut()
        return
      }
      setFailure({ err })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin-gate">
      <div className="admin-gate-stack">
        <div className="admin-gate-card">
          <span className="admin-gate-mark" aria-hidden="true">
            <ShieldCheck size={22} />
          </span>
          <h1>{t('gate.password.title')}</h1>
          <p>{t('gate.password.hint', { min: PASSWORD_MIN, max: PASSWORD_MAX })}</p>
          <form onSubmit={submit}>
            <input
              aria-label={t('gate.password.current')}
              autoComplete="current-password"
              autoFocus
              onChange={(e) => setCurrent(e.target.value)}
              placeholder={t('gate.password.current')}
              type="password"
              value={current}
            />
            <input
              aria-label={t('gate.password.new')}
              autoComplete="new-password"
              onChange={(e) => setNext(e.target.value)}
              placeholder={t('gate.password.new')}
              type="password"
              value={next}
            />
            <input
              aria-label={t('gate.password.confirm')}
              autoComplete="new-password"
              onChange={(e) => setConfirm(e.target.value)}
              placeholder={t('gate.password.confirm')}
              type="password"
              value={confirm}
            />
            {failure && (
              <div className="admin-gate-error" role="alert">
                {failureText(failure)}
              </div>
            )}
            <button disabled={busy || !current || !next || !confirm} type="submit">
              {busy ? t('common.saving') : t('gate.password.submit')}
            </button>
          </form>
          <button className="admin-gate-link" onClick={onSignOut} type="button">
            {t('nav.signOut')}
          </button>
        </div>
        <LangSwitch />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

function Dashboard({ token, onSignOut }: { token: string; onSignOut: () => void }) {
  const [view, setView] = useState<AdminView>('overview')
  const [overview, setOverview] = useState<PlatformOverview | null>(null)
  const [orgs, setOrgs] = useState<OrganizationStatsRow[]>([])
  const [modelCatalog, setModelCatalog] = useState<ModelCatalogEntry[]>([])
  const [loading, setLoading] = useState(true)
  // The failure itself, not its text, so the message follows a language switch.
  const [failure, setFailure] = useState<{ err: unknown } | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const { t, tCode } = useLang()

  const load = useCallback(
    async (isRefresh: boolean) => {
      isRefresh ? setRefreshing(true) : setLoading(true)
      setFailure(null)
      try {
        const [o, orgList, catalog] = await Promise.all([
          getOverview(token),
          getOrganizations(token, { limit: 100 }),
          getModelCatalog(token),
        ])
        setOverview(o)
        setOrgs(orgList.items)
        setModelCatalog(catalog)
      } catch (err) {
        if (err instanceof AdminApiError && err.status === 401) {
          onSignOut()
          return
        }
        setFailure({ err })
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [token, onSignOut],
  )

  useEffect(() => {
    void load(false)
  }, [load])

  const activeLabelKey = NAV_ITEMS.find((n) => n.key === view)?.labelKey ?? 'nav.overview'

  return (
    <div className="admin-shell admin-shell-with-rail">
      <aside className="admin-rail">
        <div className="admin-rail-brand">
          <ShieldCheck size={18} />
          <span>{t('app.brand')}</span>
        </div>
        <nav className="admin-rail-nav">
          {NAV_ITEMS.map(({ key, labelKey, icon: Icon }) => (
            <button
              key={key}
              className={view === key ? 'admin-rail-item active' : 'admin-rail-item'}
              onClick={() => setView(key)}
              type="button"
            >
              <Icon size={16} />
              <span>{t(labelKey)}</span>
            </button>
          ))}
        </nav>
        <div className="admin-rail-spacer" />
        <LangSwitch />
        <button className="admin-rail-item" onClick={onSignOut} type="button">
          <LogOut size={16} />
          <span>{t('nav.signOut')}</span>
        </button>
      </aside>

      <div className="admin-shell-main">
        <header className="admin-topbar">
          <span className="admin-topbar-brand">
            <Cpu size={16} /> {t(activeLabelKey)}
          </span>
          <div className="admin-topbar-actions">
            <button disabled={refreshing} onClick={() => void load(true)} type="button">
              <RefreshCw size={14} className={refreshing ? 'spin' : undefined} />{' '}
              {t('common.refresh')}
            </button>
          </div>
        </header>

        <main className="admin-main">
          {loading && <div className="admin-state">{t('app.loadingStats')}</div>}
          {failure && !loading && (
            <div className="admin-state admin-state-error">
              <AlertTriangle size={16} /> {errorMessage(failure.err, tCode, t)}
            </div>
          )}

          {!loading && !failure && overview && view === 'overview' && (
            <OverviewView overview={overview} />
          )}
          {!loading && !failure && view === 'organizations' && (
            <OrganizationsView orgs={orgs} token={token} modelCatalog={modelCatalog} />
          )}
          {!loading && !failure && view === 'models' && (
            <ModelsView token={token} modelCatalog={modelCatalog} />
          )}
          {!loading && !failure && view === 'clients' && <ClientsView token={token} />}
          {!loading && !failure && view === 'traffic' && (
            <TrafficView token={token} onSignOut={onSignOut} />
          )}
        </main>
      </div>
    </div>
  )
}
