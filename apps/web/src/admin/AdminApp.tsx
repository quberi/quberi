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
import { ModelsView } from './ModelsView'
import { OrganizationsView } from './OrganizationsView'
import { OverviewView } from './OverviewView'
import { TrafficView } from './TrafficView'

const TOKEN_KEY = 'quberi.admin.token'
// Set while the signed-in account still has to choose a new password.
const MUST_CHANGE_KEY = 'quberi.admin.mustChange'

type AdminView = 'overview' | 'organizations' | 'models' | 'clients' | 'traffic'

const NAV_ITEMS: { key: AdminView; label: string; icon: typeof LayoutDashboard }[] = [
  { key: 'overview', label: 'Overview', icon: LayoutDashboard },
  { key: 'organizations', label: 'Organizations', icon: Users },
  { key: 'models', label: 'Models', icon: Cpu },
  { key: 'clients', label: 'Outside tools', icon: Plug },
  { key: 'traffic', label: 'Traffic', icon: Activity },
]

export function AdminApp() {
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
  const signOut = () => {
    window.sessionStorage.removeItem(TOKEN_KEY)
    window.sessionStorage.removeItem(MUST_CHANGE_KEY)
    setToken(null)
    setMustChange(false)
  }

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
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || !username.trim() || !password) return
    setBusy(true)
    setError(null)
    try {
      const session = await adminLogin(username.trim(), password)
      onSignedIn(session.token, session.must_change_password)
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 429) {
        setError('Too many attempts. Wait a few minutes and try again.')
      } else if (err instanceof AdminApiError && err.status === 401) {
        setError('Invalid username or password.')
      } else {
        setError('Could not reach the admin API. Try again in a moment.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin-gate">
      <div className="admin-gate-card">
        <span className="admin-gate-mark" aria-hidden="true">
          <ShieldCheck size={22} />
        </span>
        <h1>Quberi Platform Admin</h1>
        <p>
          Sign in with a platform admin account. On a new deployment the first
          password is written once to the API log.
        </p>
        <form onSubmit={submit}>
          <input
            aria-label="Username"
            autoComplete="username"
            autoFocus
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Username"
            type="text"
            value={username}
          />
          <input
            aria-label="Password"
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            type="password"
            value={password}
          />
          {error && (
            <div className="admin-gate-error" role="alert">
              {error}
            </div>
          )}
          <button disabled={busy || !password || !username.trim()} type="submit">
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
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
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (next !== confirm) {
      setError('The new passwords do not match.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const session = await changeAdminPassword(token, current, next)
      onChanged(session.token)
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 401) {
        onSignOut()
        return
      }
      setError(
        err instanceof AdminApiError && err.message
          ? err.message
          : 'Could not change the password. Try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin-gate">
      <div className="admin-gate-card">
        <span className="admin-gate-mark" aria-hidden="true">
          <ShieldCheck size={22} />
        </span>
        <h1>Choose a new password</h1>
        <p>
          This account still uses a password it was given. Choose your own (12 to 128
          characters) to continue.
        </p>
        <form onSubmit={submit}>
          <input
            aria-label="Current password"
            autoComplete="current-password"
            autoFocus
            onChange={(e) => setCurrent(e.target.value)}
            placeholder="Current password"
            type="password"
            value={current}
          />
          <input
            aria-label="New password"
            autoComplete="new-password"
            onChange={(e) => setNext(e.target.value)}
            placeholder="New password"
            type="password"
            value={next}
          />
          <input
            aria-label="Confirm new password"
            autoComplete="new-password"
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="Confirm new password"
            type="password"
            value={confirm}
          />
          {error && (
            <div className="admin-gate-error" role="alert">
              {error}
            </div>
          )}
          <button disabled={busy || !current || !next || !confirm} type="submit">
            {busy ? 'Saving…' : 'Change password'}
          </button>
        </form>
        <button className="admin-gate-link" onClick={onSignOut} type="button">
          Sign out
        </button>
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
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(
    async (isRefresh: boolean) => {
      isRefresh ? setRefreshing(true) : setLoading(true)
      setError(null)
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
        setError(err instanceof Error ? err.message : String(err))
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

  return (
    <div className="admin-shell admin-shell-with-rail">
      <aside className="admin-rail">
        <div className="admin-rail-brand">
          <ShieldCheck size={18} />
          <span>Quberi Admin</span>
        </div>
        <nav className="admin-rail-nav">
          {NAV_ITEMS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              className={view === key ? 'admin-rail-item active' : 'admin-rail-item'}
              onClick={() => setView(key)}
              type="button"
            >
              <Icon size={16} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="admin-rail-spacer" />
        <button className="admin-rail-item" onClick={onSignOut} type="button">
          <LogOut size={16} />
          <span>Sign out</span>
        </button>
      </aside>

      <div className="admin-shell-main">
        <header className="admin-topbar">
          <span className="admin-topbar-brand">
            <Cpu size={16} /> {NAV_ITEMS.find((n) => n.key === view)?.label}
          </span>
          <div className="admin-topbar-actions">
            <button disabled={refreshing} onClick={() => void load(true)} type="button">
              <RefreshCw size={14} className={refreshing ? 'spin' : undefined} /> Refresh
            </button>
          </div>
        </header>

        <main className="admin-main">
          {loading && <div className="admin-state">Loading platform stats…</div>}
          {error && !loading && (
            <div className="admin-state admin-state-error">
              <AlertTriangle size={16} /> {error}
            </div>
          )}

          {!loading && !error && overview && view === 'overview' && (
            <OverviewView overview={overview} />
          )}
          {!loading && !error && view === 'organizations' && (
            <OrganizationsView orgs={orgs} token={token} modelCatalog={modelCatalog} />
          )}
          {!loading && !error && view === 'models' && (
            <ModelsView token={token} modelCatalog={modelCatalog} />
          )}
          {!loading && !error && view === 'clients' && <ClientsView token={token} />}
          {!loading && !error && view === 'traffic' && (
            <TrafficView token={token} onSignOut={onSignOut} />
          )}
        </main>
      </div>
    </div>
  )
}

