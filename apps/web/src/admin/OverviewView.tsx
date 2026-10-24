import type { PlatformOverview } from './api'
import { useLang } from './i18n'

/**
 * KPI cards + 14-day signup sparkline. Extracted from the original
 * single-file ``AdminApp`` so the admin shell can route between several
 * views via the sidebar instead of rendering everything on one page.
 */
export function OverviewView({ overview }: { overview: PlatformOverview }) {
  const { t } = useLang()
  const { totals } = overview
  const maxSignup = Math.max(
    1,
    ...overview.signups_by_day.map((p) => p.organizations + p.identities),
  )

  return (
    <section className="admin-section">
      <h2>{t('overview.title')}</h2>
      <div className="admin-kpi-grid">
        <KpiCard
          label={t('overview.kpi.organizations')}
          value={totals.organizations}
          sub={t('overview.sub.organizations', {
            personal: totals.organizations_personal,
            team: totals.organizations_team,
          })}
        />
        <KpiCard
          label={t('overview.kpi.newOrgs')}
          value={overview.new_organizations_24h}
          sub={t('overview.sub.newOrgs', { n: overview.new_organizations_7d })}
          highlight={overview.new_organizations_24h > 0}
        />
        <KpiCard
          label={t('overview.kpi.identities')}
          value={totals.identities}
          sub={t('overview.sub.identities', {
            a: overview.new_identities_24h,
            b: overview.new_identities_7d,
          })}
        />
        <KpiCard
          label={t('overview.kpi.activeOrgs')}
          value={overview.active_organizations_7d}
          sub={t('overview.sub.activeOrgs')}
        />
        <KpiCard label={t('overview.kpi.spaces')} value={totals.spaces} />
        <KpiCard
          label={t('overview.kpi.documents')}
          value={totals.documents}
          sub={t('overview.sub.documents', { n: totals.documents_indexed })}
        />
        <KpiCard label={t('overview.kpi.conversations')} value={totals.conversations} />
        <KpiCard label={t('overview.kpi.messages')} value={totals.messages} />
      </div>

      <div className="admin-trend">
        <h3>{t('overview.trend.title')}</h3>
        <div className="admin-trend-chart" role="img" aria-label={t('overview.trend.aria')}>
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
  const { formatNumber } = useLang()
  return (
    <div className={highlight ? 'admin-kpi-card is-highlight' : 'admin-kpi-card'}>
      <span className="admin-kpi-label">{label}</span>
      <span className="admin-kpi-value">{formatNumber(value)}</span>
      {sub && <span className="admin-kpi-sub">{sub}</span>}
    </div>
  )
}
