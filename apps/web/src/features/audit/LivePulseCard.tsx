import {
  Activity,
  Archive,
  Boxes,
  Briefcase,
  Building2,
  KeyRound,
  Layers,
  MessageSquareText,
  Network,
  RefreshCw,
  Settings,
  ShieldCheck,
  Sparkles,
  UserPlus,
  Users,
} from 'lucide-react'
import type { ComponentType } from 'react'
import type { TFunction } from 'i18next'
import { useTranslation } from 'react-i18next'

import { eventSentence } from './sentence'
import { useRecentEvents } from './useRecentEvents'
import type { RecentEvent } from './types'
import { useFormatDate } from '../../i18n/formatDate'

/**
 * "Live Org Pulse" — a compact scroll of the newest audit events for the
 * organisation. Fits inside the sidebar's context panel and auto-polls
 * so a manager glancing at the sidebar sees changes without a refresh.
 *
 * Rendering strategy
 * ------------------
 * Every action code has a matching ``audit.recent.action.<code>`` i18n
 * key with three interpolations: ``{{actor}}``, ``{{resource}}``, and
 * ``{{space}}``. Unknown action codes gracefully fall back to a generic
 * ``"{{actor}} {{action}} {{resource}}"`` template so a newly-added
 * server-side action isn't invisible until the frontend is updated.
 */
export function LivePulseCard() {
  const { t } = useTranslation()
  const { state, reload } = useRecentEvents({ limit: 15 })

  return (
    <div className="live-pulse-card">
      <header className="live-pulse-head">
        <span className="live-pulse-title">
          <Activity size={13} />
          <span>{t('audit.recent.title')}</span>
        </span>
        <button
          aria-label={t('audit.recent.refresh')}
          className="live-pulse-refresh"
          onClick={() => void reload()}
          title={t('audit.recent.refresh')}
          type="button"
        >
          <RefreshCw
            size={11}
            className={state.kind === 'loading' ? 'spin' : undefined}
          />
        </button>
      </header>

      {state.kind === 'loading' && (
        <div className="live-pulse-empty">{t('audit.recent.loading')}</div>
      )}
      {state.kind === 'error' && (
        <div className="live-pulse-empty live-pulse-error">
          {t('audit.recent.loadError')}
        </div>
      )}
      {state.kind === 'ready' && state.items.length === 0 && (
        <div className="live-pulse-empty">{t('audit.recent.empty')}</div>
      )}
      {state.kind === 'ready' && state.items.length > 0 && (
        <ul className="live-pulse-list">
          {state.items.map((ev) => (
            <PulseRow event={ev} key={ev.id} />
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// One event row
// ---------------------------------------------------------------------------

const ACTION_ICON: Record<string, ComponentType<{ size?: number }>> = {
  'member.invited': UserPlus,
  'position.created': Briefcase,
  'position.updated': Settings,
  'position.archived': Archive,
  'position.assigned': Users,
  'position.ended': Archive,
  'position.primary_set': ShieldCheck,
  'group.created': Boxes,
  'group.updated': Settings,
  'group.archived': Archive,
  'group.member_added': UserPlus,
  'group.member_removed': Archive,
  'org_unit.created': Building2,
  'org_unit.updated': Settings,
  'org_unit.archived': Archive,
  'org_unit.moved': Network,
  'org_unit.primary_set': ShieldCheck,
  'org_unit.member_removed': Archive,
  'space.created': Layers,
  'space.archived': Archive,
  'space.access_granted': KeyRound,
  'space.access_revoked': Archive,
  'organization.bootstrapped': Sparkles,
  'conversation.created': MessageSquareText,
  'message.sent': MessageSquareText,
}

function iconForAction(action: string) {
  return ACTION_ICON[action] ?? Activity
}

function PulseRow({ event }: { event: RecentEvent }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const Icon = iconForAction(event.action)
  const sentence = eventSentence(t, event)
  return (
    <li className="live-pulse-row" title={formatDate(event.created_at)}>
      <span className={`live-pulse-icon action-${event.action.split('.')[0]}`}>
        <Icon size={12} />
      </span>
      <span className="live-pulse-body">
        <span className="live-pulse-sentence">{sentence}</span>
        <small className="live-pulse-time">{relativeTime(event.created_at, t, formatDate)}</small>
      </span>
    </li>
  )
}

/**
 * Compact "5m ago" style formatting. Falls back to a locale-formatted
 * date once we're past ~4 days ago to avoid grotesque relative strings
 * like "27 days ago".
 */
function relativeTime(
  iso: string,
  t: TFunction,
  formatDate: ReturnType<typeof useFormatDate>,
): string {
  const then = new Date(iso).getTime()
  const now = Date.now()
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 45) return t('audit.recent.time.now')
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return t('audit.recent.time.minutes', { count: minutes })
  const hours = Math.round(minutes / 60)
  if (hours < 24) return t('audit.recent.time.hours', { count: hours })
  const days = Math.round(hours / 24)
  if (days < 4) return t('audit.recent.time.days', { count: days })
  return formatDate(iso, 'date')
}
