import { Activity, AlertTriangle, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AdminApiError,
  getTraffic,
  type TrafficDisabledReason,
  type TrafficKind,
  type TrafficSummary,
} from './api'
import { useLang, type I18n, type MessageKey } from './i18n'
import { errorMessage } from './i18n/errors'
import { T } from './i18n/Rich'

const EXCLUDE_KEY = 'quberi.admin.traffic.exclude'

const WINDOWS: { hours: number; label: string }[] = [
  { hours: 1, label: '1h' },
  { hours: 24, label: '24h' },
  { hours: 168, label: '7d' },
  { hours: 336, label: '14d' },
]

const KINDS = [
  'direct_or_unknown_host',
  'search_engine',
  'mcp_directory_or_probe',
  'scanner_or_tool',
  'mcp_client',
  'browser_like',
  'other',
] as const satisfies readonly TrafficKind[]

/** Display name of a kind in the active language; a kind this console does not know shows as sent. */
function kindLabel(t: I18n['t'], kind: string): string {
  const known = KINDS.find((k) => k === kind)
  return known ? t(`traffic.kind.${known}.label`) : kind
}

/** Names the site/country/status/address values the API sends in place of a real value. */
function sentinel(t: I18n['t'], value: string, map: Record<string, MessageKey>): string {
  const key = Object.prototype.hasOwnProperty.call(map, value) ? map[value] : undefined
  return key ? t(key) : value
}

const SITE_SENTINELS: Record<string, MessageKey> = {
  '(unknown host)': 'traffic.site.unknown',
  '(other host)': 'traffic.site.other',
}
const UNKNOWN_SENTINELS: Record<string, MessageKey> = { unknown: 'traffic.unknown' }
const STATUS_SENTINELS: Record<string, MessageKey> = { other: 'traffic.statusOther' }

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
  const { t, tCode } = useLang()
  const [hours, setHours] = useState(24)
  const [excludeInput, setExcludeInput] = useState(readStoredExclude)
  const [excludeApplied, setExcludeApplied] = useState(readStoredExclude)
  const [data, setData] = useState<TrafficSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<{ err: unknown } | null>(null)
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
        setError({ err })
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
          <Activity size={16} /> {t('nav.traffic')}
        </h2>
        <div className="admin-section-actions">
          <button disabled={refreshing} onClick={() => void load(true)} type="button">
            <RefreshCw size={14} className={refreshing ? 'spin' : undefined} />{' '}
            {t('common.refresh')}
          </button>
        </div>
      </div>
      <p className="admin-section-hint">
        {t('traffic.hint')}
      </p>

      <div className="admin-traffic-controls">
        <div className="admin-traffic-window" role="group" aria-label={t('traffic.window.aria')}>
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
            {t('traffic.exclude.label')}
            <input
              onChange={(e) => setExcludeInput(e.target.value)}
              placeholder="203.0.113.9, 2001:db8::1"
              spellCheck={false}
              type="text"
              value={excludeInput}
            />
          </label>
          <button type="submit">{t('traffic.exclude.apply')}</button>
        </form>
        <small className="admin-traffic-exclude-hint">
          {t('traffic.exclude.hint')}
        </small>
      </div>

      {loading && <div className="admin-state">{t('traffic.loading')}</div>}
      {error && !loading && (
        <div className="admin-state admin-state-error">
          <AlertTriangle size={16} /> {errorMessage(error.err, tCode, t)}
        </div>
      )}
      {!loading && !error && data && !data.enabled && <DisabledPanel reason={data.reason} />}
      {!loading && !error && data && data.enabled && <TrafficReport data={data} hours={hours} />}
    </section>
  )
}

function DisabledPanel({ reason }: { reason: TrafficDisabledReason | null }) {
  const { t } = useLang()
  return (
    <div className="admin-traffic-disabled" role="status">
      <h3>{t('traffic.disabled.title')}</h3>
      <p>{t(`traffic.reason.${reason ?? 'not_configured'}`)}</p>
      <p>{t('traffic.disabled.turnOn')}</p>
      <ol>
        <li>
          <T k="traffic.disabled.step1" />
        </li>
        <li>
          <T k="traffic.disabled.step2" />
        </li>
        <li>{t('traffic.disabled.step3')}</li>
      </ol>
      <p>
        <T k="traffic.disabled.footer" />
      </p>
    </div>
  )
}

function TrafficReport({ data, hours }: { data: TrafficSummary; hours: number }) {
  const { t, tCode, formatNumber } = useLang()
  const total = data.totals.requests
  const bars = bucketSeries(data.hourly, hours)
  const peak = Math.max(1, ...bars.map((b) => b.requests))
  const labelEvery = Math.max(1, Math.ceil(bars.length / 12))
  const unit: 'hour' | 'day' = hours <= 48 ? 'hour' : 'day'
  const addressNote =
    (data.address_note_code ? tCode(data.address_note_code) : null) ?? data.address_note

  return (
    <>
      <p className="admin-traffic-window-line">
        {t('traffic.windowLine', {
          from: formatTime(data.window.from),
          to: formatTime(data.window.to),
          n: data.files_read,
        })}
      </p>
      {data.truncated_by_cap && (
        <div className="admin-traffic-note admin-traffic-note-warn" role="status">
          {t('traffic.truncated')}
        </div>
      )}

      <div className="admin-kpi-grid admin-traffic-kpis-3">
        <Kpi label={t('traffic.kpi.requests')} value={formatNumber(total)} />
        <Kpi
          label={t('traffic.kpi.visitors')}
          value={formatNumber(data.totals.distinct_visitors)}
          sub={t('traffic.sub.byAddress')}
        />
        <Kpi
          label={t('traffic.kpi.automatedShare')}
          value={`${(data.totals.automated_share * 100).toFixed(1)}%`}
          sub={t('traffic.sub.automatedRequests', {
            n: formatNumber(data.totals.automated_requests),
          })}
        />
      </div>
      <h3 className="admin-traffic-subhead">{t('traffic.signals.title')}</h3>
      <div className="admin-kpi-grid admin-traffic-kpis-4">
        <Kpi
          label={t('traffic.kpi.guestQuestions')}
          value={formatNumber(data.signals.guest_questions)}
          sub={t('traffic.sub.notBots')}
        />
        <Kpi
          label={t('traffic.kpi.signIn')}
          value={formatNumber(data.signals.sign_in_requests)}
          sub={t('traffic.sub.signIn')}
        />
        <Kpi
          label={t('traffic.kpi.oauth')}
          value={formatNumber(data.signals.oauth_requests)}
          sub={t('traffic.sub.oauth')}
        />
        <Kpi
          label={t('traffic.kpi.mcpCalls')}
          value={formatNumber(data.signals.mcp_successful_posts)}
          sub={t('traffic.sub.notBots')}
        />
      </div>

      <div className="admin-trend">
        <h3>{t(`traffic.chart.title.${unit}`)}</h3>
        <div
          className="admin-trend-chart admin-traffic-chart"
          role="img"
          aria-label={t(`traffic.chart.aria.${unit}`, {
            total,
            automated: data.totals.automated_requests,
          })}
        >
          {bars.map((b, i) => {
            const height = b.requests > 0 ? Math.max(2, Math.round((b.requests / peak) * 64)) : 0
            const autoHeight = b.requests > 0 ? Math.round((b.automated / b.requests) * height) : 0
            return (
              <div
                className="admin-trend-bar"
                key={b.key}
                title={t('traffic.chart.bar', {
                  key: b.key,
                  requests: b.requests,
                  automated: b.automated,
                })}
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
            <i className="admin-traffic-swatch admin-traffic-swatch-auto" /> {t('traffic.legend.automated')}
          </span>
          <span>
            <i className="admin-traffic-swatch admin-traffic-swatch-rest" /> {t('traffic.legend.rest')}
          </span>
        </div>
      </div>

      <div className="admin-traffic-grid">
        <Block title={t('traffic.block.byKind')}>
          <table className="admin-table" aria-label={t('traffic.aria.byKind')}>
            <thead>
              <tr>
                <th scope="col">{t('traffic.col.kind')}</th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.col.requests')}
                </th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.col.share')}
                </th>
              </tr>
            </thead>
            <tbody>
              {data.by_kind.map((k) => (
                <tr key={k.kind}>
                  <td>
                    <strong>{kindLabel(t, k.kind)}</strong>
                    <small>
                      {KINDS.find((known) => known === k.kind)
                        ? t(`traffic.kind.${k.kind}.meaning`)
                        : null}
                    </small>
                  </td>
                  <td className="admin-traffic-num">{formatNumber(k.requests)}</td>
                  <td className="admin-traffic-num">{percent(k.requests, total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Block>

        <Block title={t('traffic.block.bySite')}>
          <SimpleTable
            label={t('traffic.aria.bySite')}
            head={t('traffic.col.site')}
            rows={data.by_site.map((s) => [sentinel(t, s.host, SITE_SENTINELS), s.requests])}
            code
          />
        </Block>

        <Block title={t('traffic.block.byCountry')}>
          <SimpleTable
            label={t('traffic.aria.byCountry')}
            head={t('traffic.col.country')}
            rows={data.by_country.map((c) => [
              sentinel(t, c.country, UNKNOWN_SENTINELS),
              c.requests,
            ])}
          />
        </Block>

        <Block title={t('traffic.block.byResponse')}>
          <SimpleTable
            label={t('traffic.aria.byResponse')}
            head={t('traffic.col.status')}
            rows={data.by_status_class.map((s) => [
              sentinel(t, s.status_class, STATUS_SENTINELS),
              s.requests,
            ])}
          />
        </Block>

        <Block title={t('traffic.block.mcpWho')}>
          <table className="admin-table" aria-label={t('traffic.aria.mcpAgents')}>
            <thead>
              <tr>
                <th scope="col">{t('traffic.col.userAgent')}</th>
                <th scope="col">{t('traffic.col.kind')}</th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.col.requests')}
                </th>
              </tr>
            </thead>
            <tbody>
              {data.mcp.top_user_agents.map((u, i) => (
                <tr key={`${i}-${u.user_agent}`}>
                  <td>
                    <code className="admin-code-break">{u.user_agent || t('common.none')}</code>
                  </td>
                  <td>{kindLabel(t, u.kind)}</td>
                  <td className="admin-traffic-num">{formatNumber(u.requests)}</td>
                </tr>
              ))}
              {data.mcp.top_user_agents.length === 0 && <EmptyRow cols={3} />}
            </tbody>
          </table>
        </Block>

        <Block title={t('traffic.block.mcpWhat')}>
          <table className="admin-table" aria-label={t('traffic.aria.mcpPaths')}>
            <thead>
              <tr>
                <th scope="col">{t('traffic.col.method')}</th>
                <th scope="col">{t('traffic.col.path')}</th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.col.status')}
                </th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.col.requests')}
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
                  <td className="admin-traffic-num">{formatNumber(p.requests)}</td>
                </tr>
              ))}
              {data.mcp.top_paths.length === 0 && <EmptyRow cols={4} />}
            </tbody>
          </table>
        </Block>

        <Block title={t('traffic.block.topVisitors')} wide>
          <table className="admin-table" aria-label={t('traffic.block.topVisitors')}>
            <thead>
              <tr>
                <th scope="col">{t('traffic.col.address')}</th>
                <th scope="col">{t('traffic.col.source')}</th>
                <th scope="col">{t('traffic.col.kind')}</th>
                <th scope="col">{t('traffic.col.country')}</th>
                <th scope="col">{t('traffic.col.userAgent')}</th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.col.requests')}
                </th>
              </tr>
            </thead>
            <tbody>
              {data.top_visitors.map((v, i) => (
                <tr key={`${i}-${v.address}`}>
                  <td>
                    <code>{sentinel(t, v.address, UNKNOWN_SENTINELS)}</code>
                  </td>
                  <td>
                    <span
                      className="admin-chip"
                      title={
                        v.address_source === 'header'
                          ? t('traffic.sourceTitle.header')
                          : t('traffic.sourceTitle.remote_ip')
                      }
                    >
                      {v.address_source === 'header' || v.address_source === 'remote_ip'
                        ? t(`traffic.source.${v.address_source}`)
                        : v.address_source}
                    </span>
                  </td>
                  <td>{kindLabel(t, v.kind)}</td>
                  <td>{sentinel(t, v.country, UNKNOWN_SENTINELS)}</td>
                  <td>
                    <code className="admin-code-break">{v.user_agent || t('common.none')}</code>
                  </td>
                  <td className="admin-traffic-num">{formatNumber(v.requests)}</td>
                </tr>
              ))}
              {data.top_visitors.length === 0 && <EmptyRow cols={6} />}
            </tbody>
          </table>
        </Block>

        <Block title={t('traffic.block.webPages')} wide>
          <table className="admin-table" aria-label={t('traffic.aria.webPages')}>
            <thead>
              <tr>
                <th scope="col">{t('traffic.col.path')}</th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.kind.browser_like.label')}
                </th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.kind.search_engine.label')}
                </th>
                <th scope="col" className="admin-traffic-num">
                  {t('traffic.col.total')}
                </th>
              </tr>
            </thead>
            <tbody>
              {data.web_pages.map((p, i) => (
                <tr key={`${i}-${p.path}`}>
                  <td>
                    <code className="admin-code-break">{p.path}</code>
                  </td>
                  <td className="admin-traffic-num">{formatNumber(p.browser_like)}</td>
                  <td className="admin-traffic-num">{formatNumber(p.search_engine)}</td>
                  <td className="admin-traffic-num">{formatNumber(p.total)}</td>
                </tr>
              ))}
              {data.web_pages.length === 0 && <EmptyRow cols={4} />}
            </tbody>
          </table>
        </Block>
      </div>

      <div className="admin-traffic-note">
        {t('traffic.footer')} {addressNote}
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
  const { t } = useLang()
  return (
    <tr>
      <td className="admin-table-empty" colSpan={cols}>
        {t('traffic.empty')}
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
  const { t, formatNumber } = useLang()
  return (
    <table className="admin-table" aria-label={label}>
      <thead>
        <tr>
          <th scope="col">{head}</th>
          <th scope="col" className="admin-traffic-num">
            {t('traffic.col.requests')}
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([name, count], i) => (
          <tr key={`${i}-${name}`}>
            <td>{code ? <code className="admin-code-break">{name}</code> : name}</td>
            <td className="admin-traffic-num">{formatNumber(count)}</td>
          </tr>
        ))}
        {rows.length === 0 && <EmptyRow cols={2} />}
      </tbody>
    </table>
  )
}
