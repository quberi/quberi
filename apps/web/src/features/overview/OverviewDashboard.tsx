import {
  AlertTriangle,
  ArrowRight,
  FileText,
  Library,
  Loader2,
  Plug,
  RefreshCw,
  Users,
} from 'lucide-react'
import type { ComponentType, ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { LivePulseCard } from '../audit/LivePulseCard'
import { TrendBars } from './TrendBars'
import { useOverview } from './useOverview'
import type { Overview, OverviewAttention, OverviewCitedDocument } from './types'

/** Where each kind of attention item is dealt with. */
export type OverviewTarget = 'members' | 'spaces' | 'integrations'

const ATTENTION_TARGET: Record<string, OverviewTarget> = {
  pending_reviews: 'spaces',
  access_requests: 'spaces',
  index_failed: 'spaces',
  suspended_owning_spaces: 'members',
  org_wide_spaces: 'spaces',
}

/**
 * The admin console landing page: how big the organisation is, what needs attention, how it
 * has been used lately, which documents answers lean on, and the live activity feed.
 */
export function OverviewDashboard({ onNavigate }: { onNavigate: (to: OverviewTarget) => void }) {
  const { t } = useTranslation()
  const { state, refreshing, reload } = useOverview()

  return (
    <section className="panel overview">
      <div className="panel-heading">
        <div>
          <span className="section-label">{t('adminConsole.nav.overview')}</span>
          <p>{t('adminConsole.overview.description')}</p>
        </div>
        <div className="panel-actions">
          <button
            aria-label={t('audit.recent.refresh')}
            className="secondary-button icon-button"
            disabled={refreshing}
            onClick={() => void reload()}
            title={t('audit.recent.refresh')}
            type="button"
          >
            <RefreshCw className={refreshing ? 'spin' : undefined} size={13} />
          </button>
        </div>
      </div>

      {state.kind === 'loading' && (
        <div className="state-panel">
          <Loader2 className="spin" size={14} />
        </div>
      )}
      {state.kind === 'error' && <div className="state-panel error-state">{state.message}</div>}
      {state.kind === 'ready' && (
        <>
          <StatCards onNavigate={onNavigate} overview={state.overview} />
          <AttentionList items={state.overview.attention} onNavigate={onNavigate} />
          <div className="overview-grid">
            <TrendCard overview={state.overview} />
            <TopCitedCard days={state.overview.cited_days} items={state.overview.top_cited} />
          </div>
        </>
      )}

      <LivePulseCard />
    </section>
  )
}

// ---------------------------------------------------------------------------

function StatCards({
  overview,
  onNavigate,
}: {
  overview: Overview
  onNavigate: (to: OverviewTarget) => void
}) {
  const { t } = useTranslation()
  const { members, spaces, documents, external } = overview
  return (
    <div className="overview-stats">
      <StatCard
        icon={Users}
        label={t('overview.stat.members')}
        onClick={() => onNavigate('members')}
        sub={[
          t('overview.stat.membersActive', { count: members.active }),
          members.invited > 0 ? t('overview.stat.membersInvited', { count: members.invited }) : null,
          members.suspended > 0
            ? t('overview.stat.membersSuspended', { count: members.suspended })
            : null,
        ]}
        value={members.total}
      />
      <StatCard
        icon={Library}
        label={t('overview.stat.spaces')}
        onClick={() => onNavigate('spaces')}
        sub={[
          t('overview.stat.spacesEnterprise', { count: spaces.enterprise }),
          t('overview.stat.spacesTeam', { count: spaces.team }),
          t('overview.stat.spacesPersonal', { count: spaces.personal }),
          spaces.archived > 0 ? t('overview.stat.spacesArchived', { count: spaces.archived }) : null,
        ]}
        value={spaces.total}
      />
      <StatCard
        icon={FileText}
        label={t('overview.stat.documents')}
        sub={[
          t('overview.stat.docsIndexed', { count: documents.indexed }),
          documents.waiting > 0 ? t('overview.stat.docsWaiting', { count: documents.waiting }) : null,
          documents.failed > 0 ? t('overview.stat.docsFailed', { count: documents.failed }) : null,
        ]}
        tone={documents.failed > 0 ? 'warn' : undefined}
        value={documents.total}
      />
      <StatCard
        icon={Plug}
        label={t('overview.stat.external')}
        onClick={() => onNavigate('integrations')}
        sub={[t('overview.stat.externalCalls', { count: external.calls_7d })]}
        value={external.connected_tools}
      />
    </div>
  )
}

function StatCard({
  icon: Icon,
  label,
  value,
  sub,
  onClick,
  tone,
}: {
  icon: ComponentType<{ size?: number }>
  label: string
  value: number
  sub: Array<string | null>
  onClick?: () => void
  tone?: 'warn'
}) {
  const body: ReactNode = (
    <>
      <span className="overview-stat-label">
        <Icon size={13} /> {label}
      </span>
      <strong className="overview-stat-value">{value}</strong>
      <small className="overview-stat-sub">
        {sub
          .filter((s): s is string => s !== null)
          .map((s, i) => (
            <span key={i}>{s}</span>
          ))}
      </small>
    </>
  )
  const className = `overview-stat${tone === 'warn' ? ' overview-stat-warn' : ''}`
  return onClick ? (
    <button className={`${className} overview-stat-link`} onClick={onClick} type="button">
      {body}
    </button>
  ) : (
    <div className={className}>{body}</div>
  )
}

// ---------------------------------------------------------------------------

function AttentionList({
  items,
  onNavigate,
}: {
  items: OverviewAttention[]
  onNavigate: (to: OverviewTarget) => void
}) {
  const { t, i18n } = useTranslation()
  if (items.length === 0) return null
  // ": A, B" (or "：A、B"), or nothing at all when there are no names to give.
  const suffix = (names: string[]) =>
    names.length === 0
      ? ''
      : `${i18n.language.startsWith('zh') ? '：' : ': '}${new Intl.ListFormat(i18n.language, {
          style: 'narrow',
          type: 'conjunction',
        }).format(names)}`
  return (
    <div className="overview-attention" role="region" aria-label={t('overview.attention.title')}>
      <h3>
        <AlertTriangle size={14} /> {t('overview.attention.title')}
      </h3>
      <ul>
        {items.map((a) => {
          const target = ATTENTION_TARGET[a.code]
          return (
            <li key={a.code}>
              <span>
                {t(`overview.attention.${a.code}`, {
                  count: a.count,
                  names: suffix(a.names),
                  defaultValue: `${a.code}: ${a.count}`,
                })}
              </span>
              {target && (
                <button className="link-button" onClick={() => onNavigate(target)} type="button">
                  {t(`overview.attention.go_${a.code}`, {
                    defaultValue: t('overview.attention.go'),
                  })}{' '}
                  <ArrowRight size={12} />
                </button>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ---------------------------------------------------------------------------

function TrendCard({ overview }: { overview: Overview }) {
  const { t } = useTranslation()
  const sum = (pick: (p: Overview['trend'][number]) => number) =>
    overview.trend.reduce((acc, p) => acc + pick(p), 0)
  return (
    <div className="overview-card overview-card-trend">
      <h3>{t('overview.trend.title', { days: overview.trend.length })}</h3>
      <div className="overview-trends">
        <TrendBars
          label={t('overview.trend.documents')}
          pick={(p) => p.documents}
          points={overview.trend}
          total={String(sum((p) => p.documents))}
        />
        <TrendBars
          label={t('overview.trend.questions')}
          pick={(p) => p.questions}
          points={overview.trend}
          total={String(sum((p) => p.questions))}
        />
      </div>
    </div>
  )
}

function TopCitedCard({ items, days }: { items: OverviewCitedDocument[]; days: number }) {
  const { t } = useTranslation()
  const max = Math.max(1, ...items.map((i) => i.citations))
  return (
    <div className="overview-card">
      <h3>{t('overview.cited.title', { days })}</h3>
      {items.length === 0 ? (
        <p className="overview-empty">{t('overview.cited.empty')}</p>
      ) : (
        <ol className="overview-cited">
          {items.map((d) => (
            <li key={d.document_id}>
              <span className="overview-cited-name">
                <strong title={d.name}>{d.name}</strong>
                <small>{d.space_name}</small>
              </span>
              <span className="overview-cited-count">
                <span className="overview-cited-bar" aria-hidden="true">
                  <span style={{ width: `${Math.round((d.citations / max) * 100)}%` }} />
                </span>
                <b>{d.citations}</b>
              </span>
            </li>
          ))}
        </ol>
      )}
      <small className="overview-note">{t('overview.cited.note')}</small>
    </div>
  )
}
