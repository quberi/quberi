import type { PlatformOverview } from './api'

/**
 * KPI cards + 14-day signup sparkline. Extracted from the original
 * single-file ``AdminApp`` so the admin shell can route between several
 * views via the sidebar instead of rendering everything on one page.
 */
export function OverviewView({ overview }: { overview: PlatformOverview }) {
  const { totals } = overview
  const maxSignup = Math.max(
    1,
    ...overview.signups_by_day.map((p) => p.organizations + p.identities),
  )

  return (
    <section className="admin-section">
      <h2>Overview</h2>
      <div className="admin-kpi-grid">
        <KpiCard
          label="Organizations"
          value={totals.organizations}
          sub={`${totals.organizations_personal} personal · ${totals.organizations_team} team`}
        />
        <KpiCard
          label="New orgs (24h / 7d)"
          value={overview.new_organizations_24h}
          sub={`${overview.new_organizations_7d} in last 7 days`}
          highlight={overview.new_organizations_24h > 0}
        />
        <KpiCard
          label="Identities (people)"
          value={totals.identities}
          sub={`${overview.new_identities_24h} new in 24h · ${overview.new_identities_7d} in 7d`}
        />
        <KpiCard
          label="Active orgs (7d)"
          value={overview.active_organizations_7d}
          sub="sent at least one message"
        />
        <KpiCard label="Spaces" value={totals.spaces} />
        <KpiCard
          label="Documents"
          value={totals.documents}
          sub={`${totals.documents_indexed} indexed`}
        />
        <KpiCard label="Conversations" value={totals.conversations} />
        <KpiCard label="Messages" value={totals.messages} />
      </div>

      <div className="admin-trend">
        <h3>Signups — last 14 days</h3>
        <div className="admin-trend-chart" role="img" aria-label="Daily signups, last 14 days">
          {overview.signups_by_day.map((p) => {
            const total = p.organizations + p.identities
            const h = Math.max(2, Math.round((total / maxSignup) * 64))
            return (
              <div className="admin-trend-bar" key={p.day} title={`${p.day}: ${total}`}>
                <div className="admin-trend-bar-fill" style={{ height: `${h}px` }} />
                <small>{p.day.slice(5)}</small>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}

function KpiCard({
  label,
  value,
  sub,
  highlight,
}: {
  label: string
  value: number
  sub?: string
  highlight?: boolean
}) {
  return (
    <div className={highlight ? 'admin-kpi-card is-highlight' : 'admin-kpi-card'}>
      <span className="admin-kpi-label">{label}</span>
      <span className="admin-kpi-value">{value.toLocaleString()}</span>
      {sub && <span className="admin-kpi-sub">{sub}</span>}
    </div>
  )
}
