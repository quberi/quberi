import { Activity, AlertTriangle, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AdminApiError,
  getTraffic,
  type TrafficDisabledReason,
  type TrafficKind,
  type TrafficSummary,
} from './api'

const EXCLUDE_KEY = 'quberi.admin.traffic.exclude'

const WINDOWS: { hours: number; label: string }[] = [
  { hours: 1, label: '1h' },
  { hours: 24, label: '24h' },
  { hours: 168, label: '7d' },
  { hours: 336, label: '14d' },
]

const KIND_INFO: Record<TrafficKind, { label: string; meaning: string }> = {
  direct_or_unknown_host: {
    label: 'Direct or unknown host',
    meaning: 'Addressed to a bare IP or to a host that is not one of your sites. Scanners.',
  },
  search_engine: {
    label: 'Search engines',
    meaning: 'Search engines and well-known crawlers that name themselves.',
  },
  mcp_directory_or_probe: {
    label: 'MCP directories and probes',
    meaning: 'Directories, health checks and trust or ranking services.',
  },
  scanner_or_tool: {
    label: 'Scanners and tools',
    meaning: 'Command-line tools, HTTP libraries, security scanners, or no User-Agent.',
  },
  mcp_client: {
    label: 'MCP clients',
    meaning: 'Tools connecting to the MCP endpoint (Codex, Claude, Cursor, ...).',
  },
  browser_like: {
    label: 'Browser-like',
    meaning: 'A browser User-Agent on one of your sites. It can still be a crawler.',
  },
  other: { label: 'Other', meaning: 'Anything the rules above did not match.' },
}

const REASON_TEXT: Record<TrafficDisabledReason, string> = {
  not_configured: 'No access log is configured, so there is nothing to summarise yet.',
  file_missing:
    'An access log path is set, but there is no file there. The proxy’s log volume is probably not mounted into the API container.',
  unreadable:
    'The access log exists, but the API container is not allowed to read it. Check the file’s permissions.',
}

function readStoredExclude(): string {
  try {
    return window.localStorage.getItem(EXCLUDE_KEY) ?? ''
  } catch {
    return ''
  }
}

function storeExclude(value: string) {
  try {
    if (value) window.localStorage.setItem(EXCLUDE_KEY, value)
    else window.localStorage.removeItem(EXCLUDE_KEY)
  } catch {
    // Storage can be unavailable (private mode); the filter then lasts for this visit only.
  }
}

export interface ChartBar {
  key: string
  label: string
  requests: number
  automated: number
}

/**
 * The hourly series as chart bars: one per hour for windows up to 48 hours, one per UTC day
 * for longer windows (a 14-day window would otherwise be 337 slivers).
 */
export function bucketSeries(hourly: TrafficSummary['hourly'], hours: number): ChartBar[] {
  if (hours <= 48) {
    return hourly.map((h) => ({
      key: h.hour,
      label: h.hour.slice(11, 13),
      requests: h.requests,
      automated: h.automated_requests,
    }))
  }
  const days = new Map<string, ChartBar>()
  for (const h of hourly) {
    const day = h.hour.slice(0, 10)
    const bar = days.get(day) ?? { key: day, label: day.slice(5), requests: 0, automated: 0 }
    bar.requests += h.requests
    bar.automated += h.automated_requests
    days.set(day, bar)
  }
  return [...days.values()]
}

const formatTime = (iso: string) => iso.replace('T', ' ').slice(0, 16)
const percent = (part: number, whole: number) =>
  whole > 0 ? `${((part / whole) * 100).toFixed(1)}%` : '0.0%'

/**
 * Who is visiting, from the reverse proxy's access log.
 *
 * The kinds are inferred from the User-Agent and the request pattern, so a browser-like visit
 * can still be a crawler; addresses arrive masked. Every string that came from a request (User-Agent,
 * path, host) is shown as plain text and nothing here builds HTML from it.
 */
export function TrafficView({ token, onSignOut }: { token: string; onSignOut: () => void }) {
  const [hours, setHours] = useState(24)
  const [excludeInput, setExcludeInput] = useState(readStoredExclude)
  const [excludeApplied, setExcludeApplied] = useState(readStoredExclude)
  const [data, setData] = useState<TrafficSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // A slow older answer must never replace a newer one.
  const latest = useRef(0)

  const load = useCallback(
    async (isRefresh: boolean) => {
      const mine = ++latest.current
      if (isRefresh) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const result = await getTraffic(token, { hours, excludeIps: excludeApplied })
        if (mine !== latest.current) return
        setData(result)
      } catch (err) {
        if (mine !== latest.current) return
        if (err instanceof AdminApiError && err.status === 401) {
          onSignOut()
          return
        }
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        if (mine === latest.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [token, hours, excludeApplied, onSignOut],
  )

  useEffect(() => {
    void load(false)
  }, [load])

  const applyExclude = (e: React.FormEvent) => {
    e.preventDefault()
    const value = excludeInput.trim()
    storeExclude(value)
    setExcludeApplied(value)
  }

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <h2>
          <Activity size={16} /> Traffic
        </h2>
        <div className="admin-section-actions">
          <button disabled={refreshing} onClick={() => void load(true)} type="button">
            <RefreshCw size={14} className={refreshing ? 'spin' : undefined} /> Refresh
          </button>
        </div>
      </div>
      <p className="admin-section-hint">
        Who is visiting, from the reverse proxy’s access log: real people against crawlers,
        scanners, MCP directories and MCP clients.
      </p>

      <div className="admin-traffic-controls">
        <div className="admin-traffic-window" role="group" aria-label="Time window">
          {WINDOWS.map((w) => (
            <button
              aria-pressed={hours === w.hours}
              className={hours === w.hours ? 'active' : undefined}
              key={w.hours}
              onClick={() => setHours(w.hours)}
              type="button"
            >
              {w.label}
            </button>
          ))}
        </div>
        <form className="admin-traffic-exclude" onSubmit={applyExclude}>
          <label className="admin-field">
            Exclude my addresses
            <input
              onChange={(e) => setExcludeInput(e.target.value)}
              placeholder="203.0.113.9, 2001:db8::1"
              spellCheck={false}
              type="text"
              value={excludeInput}
            />
          </label>
          <button type="submit">Apply</button>
        </form>
        <small className="admin-traffic-exclude-hint">
          Full addresses, comma-separated. Their requests are left out of every count. Saved in this
          browser only.
        </small>
      </div>

      {loading && <div className="admin-state">Loading traffic…</div>}
      {error && !loading && (
        <div className="admin-state admin-state-error">
          <AlertTriangle size={16} /> {error}
        </div>
      )}
      {!loading && !error && data && !data.enabled && <DisabledPanel reason={data.reason} />}
      {!loading && !error && data && data.enabled && <TrafficReport data={data} hours={hours} />}
    </section>
  )
}

function DisabledPanel({ reason }: { reason: TrafficDisabledReason | null }) {
  return (
    <div className="admin-traffic-disabled" role="status">
      <h3>Traffic is not set up</h3>
      <p>{reason ? REASON_TEXT[reason] : REASON_TEXT.not_configured}</p>
      <p>To turn it on:</p>
      <ol>
        <li>
          Mount the proxy’s log volume into the API container, read-only:{' '}
          <code>caddy-data:/caddy-data:ro</code>.
        </li>
        <li>
          Set <code>QUBERI_ACCESS_LOG_PATH</code> (for example <code>/caddy-data/access.log</code>)
          and <code>QUBERI_TRAFFIC_SITE_HOSTS</code> (the host names you serve, comma-separated).
        </li>
        <li>Recreate the API container so it picks up the mount and the settings.</li>
      </ol>
      <p>
        <code>deploy/compose.prod.yaml</code> already does the first two for the production stack.
        See “Access log and the admin Traffic page” in <code>deploy/README.md</code>.
      </p>
    </div>
  )
}

function TrafficReport({ data, hours }: { data: TrafficSummary; hours: number }) {
  const total = data.totals.requests
  const bars = bucketSeries(data.hourly, hours)
  const peak = Math.max(1, ...bars.map((b) => b.requests))
  const labelEvery = Math.max(1, Math.ceil(bars.length / 12))
  const unit = hours <= 48 ? 'hour' : 'day'

  return (
    <>
      <p className="admin-traffic-window-line">
        {formatTime(data.window.from)} to {formatTime(data.window.to)} UTC · {data.files_read}{' '}
        {data.files_read === 1 ? 'log file' : 'log files'} read
      </p>
      {data.truncated_by_cap && (
        <div className="admin-traffic-note admin-traffic-note-warn" role="status">
          The log is larger than the read limit, so only its newest part is included. Counts for
          this window are partial.
        </div>
      )}

      <div className="admin-kpi-grid admin-traffic-kpis-3">
        <Kpi label="Requests" value={total.toLocaleString()} />
        <Kpi
          label="Distinct visitors"
          value={data.totals.distinct_visitors.toLocaleString()}
          sub="by address"
        />
        <Kpi
          label="Automated share"
          value={`${(data.totals.automated_share * 100).toFixed(1)}%`}
          sub={`${data.totals.automated_requests.toLocaleString()} automated requests`}
        />
      </div>
      <h3 className="admin-traffic-subhead">Signs of real use (bots left out)</h3>
      <div className="admin-kpi-grid admin-traffic-kpis-4">
        <Kpi
          label="Guest questions"
          value={data.signals.guest_questions.toLocaleString()}
          sub="successful, not from bots"
        />
        <Kpi
          label="Sign-in requests"
          value={data.signals.sign_in_requests.toLocaleString()}
          sub="email or phone code"
        />
        <Kpi
          label="OAuth requests"
          value={data.signals.oauth_requests.toLocaleString()}
          sub="authorize or token"
        />
        <Kpi
          label="MCP calls accepted"
          value={data.signals.mcp_successful_posts.toLocaleString()}
          sub="successful, not from bots"
        />
      </div>

      <div className="admin-trend">
        <h3>Requests per {unit} (UTC)</h3>
        <div
          className="admin-trend-chart admin-traffic-chart"
          role="img"
          aria-label={`Requests per ${unit}: ${total} in total, ${data.totals.automated_requests} automated`}
        >
          {bars.map((b, i) => {
            const height = b.requests > 0 ? Math.max(2, Math.round((b.requests / peak) * 64)) : 0
            const autoHeight = b.requests > 0 ? Math.round((b.automated / b.requests) * height) : 0
            return (
              <div
                className="admin-trend-bar"
                key={b.key}
                title={`${b.key}: ${b.requests} requests, ${b.automated} automated`}
              >
                <div className="admin-traffic-bar-stack" style={{ height: `${height}px` }}>
                  <div className="admin-traffic-bar-rest" style={{ flex: height - autoHeight }} />
                  <div className="admin-traffic-bar-auto" style={{ flex: autoHeight }} />
                </div>
                <small>{i % labelEvery === 0 ? b.label : ''}</small>
              </div>
            )
          })}
        </div>
        <div className="admin-traffic-legend">
          <span>
            <i className="admin-traffic-swatch admin-traffic-swatch-auto" /> Automated
          </span>
          <span>
            <i className="admin-traffic-swatch admin-traffic-swatch-rest" /> Everything else
          </span>
        </div>
      </div>

      <div className="admin-traffic-grid">
        <Block title="By kind of visitor">
          <table className="admin-table" aria-label="Requests by kind of visitor">
            <thead>
              <tr>
                <th scope="col">Kind</th>
                <th scope="col" className="admin-traffic-num">
                  Requests
                </th>
                <th scope="col" className="admin-traffic-num">
                  Share
                </th>
              </tr>
            </thead>
            <tbody>
              {data.by_kind.map((k) => (
                <tr key={k.kind}>
                  <td>
                    <strong>{KIND_INFO[k.kind]?.label ?? k.kind}</strong>
                    <small>{KIND_INFO[k.kind]?.meaning}</small>
                  </td>
                  <td className="admin-traffic-num">{k.requests.toLocaleString()}</td>
                  <td className="admin-traffic-num">{percent(k.requests, total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Block>

        <Block title="By site">
          <SimpleTable
            label="Requests by site"
            head="Site"
            rows={data.by_site.map((s) => [s.host, s.requests])}
            code
          />
        </Block>

        <Block title="By country (top 10)">
          <SimpleTable
            label="Requests by country"
            head="Country"
            rows={data.by_country.map((c) => [c.country, c.requests])}
          />
        </Block>

        <Block title="By response">
          <SimpleTable
            label="Requests by response class"
            head="Status"
            rows={data.by_status_class.map((s) => [s.status_class, s.requests])}
          />
        </Block>

        <Block title="MCP: who is calling">
          <table className="admin-table" aria-label="MCP user agents">
            <thead>
              <tr>
                <th scope="col">User-Agent</th>
                <th scope="col">Kind</th>
                <th scope="col" className="admin-traffic-num">
                  Requests
                </th>
              </tr>
            </thead>
            <tbody>
              {data.mcp.top_user_agents.map((u, i) => (
                <tr key={`${i}-${u.user_agent}`}>
                  <td>
                    <code className="admin-code-break">{u.user_agent || '(none)'}</code>
                  </td>
                  <td>{KIND_INFO[u.kind]?.label ?? u.kind}</td>
                  <td className="admin-traffic-num">{u.requests.toLocaleString()}</td>
                </tr>
              ))}
              {data.mcp.top_user_agents.length === 0 && <EmptyRow cols={3} />}
            </tbody>
          </table>
        </Block>

        <Block title="MCP: what they ask for">
          <table className="admin-table" aria-label="MCP paths">
            <thead>
              <tr>
                <th scope="col">Method</th>
                <th scope="col">Path</th>
                <th scope="col" className="admin-traffic-num">
                  Status
                </th>
                <th scope="col" className="admin-traffic-num">
                  Requests
                </th>
              </tr>
            </thead>
            <tbody>
              {data.mcp.top_paths.map((p, i) => (
                <tr key={`${i}-${p.method}-${p.path}-${p.status}`}>
                  <td>{p.method}</td>
                  <td>
                    <code className="admin-code-break">{p.path}</code>
                  </td>
                  <td className="admin-traffic-num">{p.status}</td>
                  <td className="admin-traffic-num">{p.requests.toLocaleString()}</td>
                </tr>
              ))}
              {data.mcp.top_paths.length === 0 && <EmptyRow cols={4} />}
            </tbody>
          </table>
        </Block>

        <Block title="Top visitors" wide>
          <table className="admin-table" aria-label="Top visitors">
            <thead>
              <tr>
                <th scope="col">Address</th>
                <th scope="col">Source</th>
                <th scope="col">Kind</th>
                <th scope="col">Country</th>
                <th scope="col">User-Agent</th>
                <th scope="col" className="admin-traffic-num">
                  Requests
                </th>
              </tr>
            </thead>
            <tbody>
              {data.top_visitors.map((v, i) => (
                <tr key={`${i}-${v.address}`}>
                  <td>
                    <code>{v.address}</code>
                  </td>
                  <td>
                    <span
                      className="admin-chip"
                      title={
                        v.address_source === 'header'
                          ? 'Taken from a request header set by the CDN'
                          : 'The connecting address'
                      }
                    >
                      {v.address_source}
                    </span>
                  </td>
                  <td>{KIND_INFO[v.kind]?.label ?? v.kind}</td>
                  <td>{v.country}</td>
                  <td>
                    <code className="admin-code-break">{v.user_agent || '(none)'}</code>
                  </td>
                  <td className="admin-traffic-num">{v.requests.toLocaleString()}</td>
                </tr>
              ))}
              {data.top_visitors.length === 0 && <EmptyRow cols={6} />}
            </tbody>
          </table>
        </Block>

        <Block title="Pages on the web site (browser-like and search engines)" wide>
          <table className="admin-table" aria-label="Web pages">
            <thead>
              <tr>
                <th scope="col">Path</th>
                <th scope="col" className="admin-traffic-num">
                  Browser-like
                </th>
                <th scope="col" className="admin-traffic-num">
                  Search engines
                </th>
                <th scope="col" className="admin-traffic-num">
                  Total
                </th>
              </tr>
            </thead>
            <tbody>
              {data.web_pages.map((p, i) => (
                <tr key={`${i}-${p.path}`}>
                  <td>
                    <code className="admin-code-break">{p.path}</code>
                  </td>
                  <td className="admin-traffic-num">{p.browser_like.toLocaleString()}</td>
                  <td className="admin-traffic-num">{p.search_engine.toLocaleString()}</td>
                  <td className="admin-traffic-num">{p.total.toLocaleString()}</td>
                </tr>
              ))}
              {data.web_pages.length === 0 && <EmptyRow cols={4} />}
            </tbody>
          </table>
        </Block>
      </div>

      <div className="admin-traffic-note">
        Kinds are inferred from the User-Agent and the request pattern; a browser-like visit can
        still be a crawler. {data.address_note}
      </div>
    </>
  )
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="admin-kpi-card">
      <span className="admin-kpi-label">{label}</span>
      <span className="admin-kpi-value">{value}</span>
      {sub && <span className="admin-kpi-sub">{sub}</span>}
    </div>
  )
}

function Block({
  title,
  wide,
  children,
}: {
  title: string
  wide?: boolean
  children: React.ReactNode
}) {
  return (
    <div className={wide ? 'admin-traffic-block admin-traffic-wide' : 'admin-traffic-block'}>
      <h3>{title}</h3>
      <div className="admin-table-wrap">{children}</div>
    </div>
  )
}

function EmptyRow({ cols }: { cols: number }) {
  return (
    <tr>
      <td className="admin-table-empty" colSpan={cols}>
        No requests in this window.
      </td>
    </tr>
  )
}

function SimpleTable({
  label,
  head,
  rows,
  code,
}: {
  label: string
  head: string
  rows: [string, number][]
  code?: boolean
}) {
  return (
    <table className="admin-table" aria-label={label}>
      <thead>
        <tr>
          <th scope="col">{head}</th>
          <th scope="col" className="admin-traffic-num">
            Requests
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([name, count], i) => (
          <tr key={`${i}-${name}`}>
            <td>{code ? <code className="admin-code-break">{name}</code> : name}</td>
            <td className="admin-traffic-num">{count.toLocaleString()}</td>
          </tr>
        ))}
        {rows.length === 0 && <EmptyRow cols={2} />}
      </tbody>
    </table>
  )
}
