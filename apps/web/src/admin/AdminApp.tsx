import {
  AlertTriangle,
  Cpu,
  LayoutDashboard,
  LogOut,
  RefreshCw,
  ShieldCheck,
  Users,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import {
  AdminApiError,
  getModelCatalog,
  getOrganizations,
  getOverview,
  verifyToken,
  type ModelCatalogEntry,
  type OrganizationStatsRow,
  type PlatformOverview,
} from './api'
import { ModelsView } from './ModelsView'
import { OrganizationsView } from './OrganizationsView'
import { OverviewView } from './OverviewView'

const TOKEN_KEY = 'quberi.admin.token'

type AdminView = 'overview' | 'organizations' | 'models'

const NAV_ITEMS: { key: AdminView; label: string; icon: typeof LayoutDashboard }[] = [
  { key: 'overview', label: 'Overview', icon: LayoutDashboard },
  { key: 'organizations', label: 'Organizations', icon: Users },
  { key: 'models', label: 'Models', icon: Cpu },
]

export function AdminApp() {
  const [token, setToken] = useState<string | null>(() =>
    window.sessionStorage.getItem(TOKEN_KEY),
  )

  if (!token) {
    return <TokenGate onVerified={(t) => setToken(t)} />
  }
  return (
    <Dashboard
      token={token}
      onSignOut={() => {
        window.sessionStorage.removeItem(TOKEN_KEY)
        setToken(null)
      }}
    />
  )
}

// ---------------------------------------------------------------------------
// Token gate — paste the platform admin bearer token, verified against a
// real endpoint before it's trusted and stored for the tab's lifetime.
// ---------------------------------------------------------------------------

function TokenGate({ onVerified }: { onVerified: (token: string) => void }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const t = value.trim()
    if (!t || busy) return
    setBusy(true)
    setError(null)
    try {
      await verifyToken(t)
      window.sessionStorage.setItem(TOKEN_KEY, t)
      onVerified(t)
    } catch (err) {
      setError(
        err instanceof AdminApiError && err.status === 401
          ? 'Invalid admin token.'
          : 'Could not reach the admin API. Check the token and try again.',
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
        <h1>Quberi Platform Admin</h1>
        <p>
          Paste the <code>QUBERI_PLATFORM_ADMIN_TOKEN</code> bearer token for this
          deployment. It is kept only in this tab's session storage — closing the tab
          clears it.
        </p>
        <form onSubmit={submit}>
          <input
            autoFocus
            autoComplete="off"
            onChange={(e) => setValue(e.target.value)}
            placeholder="Platform admin token"
            type="password"
            value={value}
          />
          {error && <div className="admin-gate-error">{error}</div>}
          <button disabled={!value.trim() || busy} type="submit">
            {busy ? 'Verifying…' : 'Enter'}
          </button>
        </form>
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
        </main>
      </div>
    </div>
  )
}

