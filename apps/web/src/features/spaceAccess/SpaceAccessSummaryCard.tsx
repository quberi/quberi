import { Users } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { AccessSources } from './AccessSources'
import type { UseSpaceAccessSummaryResult } from './useSpaceAccess'
import type { SpaceRoleCode } from './types'

const ROLES: SpaceRoleCode[] = ['owner', 'admin', 'editor', 'viewer']

/**
 * The first thing on a space's access panel: how many people can read it, in which roles,
 * who it is open to, and how many documents have their own exceptions. Everything below is
 * the detail behind these numbers.
 */
export function SpaceAccessSummaryCard({ state }: { state: UseSpaceAccessSummaryResult['state'] }) {
  const { t } = useTranslation()
  if (state.kind === 'idle' || state.kind === 'loading') {
    return <div className="access-summary access-summary-loading">{t('spaceAccess.summary.loading')}</div>
  }
  if (state.kind === 'error') {
    return <div className="state-panel error-state">{state.message}</div>
  }
  const s = state.summary
  return (
    <div className="access-summary" data-testid="space-access-summary">
      <div className="access-summary-head">
        <span className="access-summary-count">
          <Users size={16} />
          <strong>{s.people}</strong>
          <span>{t('spaceAccess.summary.people')}</span>
        </span>
        <span className="access-summary-roles">
          {ROLES.filter((r) => (s.by_role[r] ?? 0) > 0).map((r) => (
            <span className={`badge role-${r}`} key={r}>
              {t(`members.role.${r}`)} {s.by_role[r]}
            </span>
          ))}
        </span>
      </div>
      <div className="access-summary-line">
        <span className="access-summary-label">{t('spaceAccess.summary.openTo')}</span>
        <AccessSources sources={s.sources} />
      </div>
      <div className="access-summary-line">
        <span className="access-summary-label">{t('spaceAccess.summary.exceptions')}</span>
        <span>
          {s.documents_with_exceptions === 0
            ? t('spaceAccess.summary.noExceptions', { count: s.documents })
            : t('spaceAccess.summary.someExceptions', {
                documents: s.documents,
                count: s.documents_with_exceptions,
                allow: s.allow_rules,
                deny: s.deny_rules,
              })}
        </span>
      </div>
    </div>
  )
}
