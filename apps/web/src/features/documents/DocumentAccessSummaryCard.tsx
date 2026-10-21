import { Users } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { AccessSources } from '../spaceAccess/AccessSources'
import type { DocumentAccessSummary } from '../spaceAccess/types'

/**
 * Top of a document's access tab: how many people can read it in the end, how that number
 * is made up, what it inherits from its space, and how many exception rules it has.
 */
export function DocumentAccessSummaryCard({ summary }: { summary: DocumentAccessSummary }) {
  const { t } = useTranslation()
  const s = summary
  return (
    <div className="access-summary" data-testid="document-access-summary">
      <div className="access-summary-head">
        {s.readers === null ? (
          <span className="access-summary-note">
            {s.review_state === 'approved'
              ? t('docAccess.summary.tooLarge')
              : t('docAccess.summary.notApproved')}
          </span>
        ) : (
          <span className="access-summary-count">
            <Users size={16} />
            <strong>{s.readers}</strong>
            <span>{t('docAccess.summary.readers')}</span>
          </span>
        )}
      </div>
      {s.readers !== null && (
        <div className="access-summary-line">
          <span className="access-summary-label">{t('docAccess.summary.makeup')}</span>
          <span>
            {t('docAccess.summary.fromSpace', { count: s.space_people })}
            {s.shared_only > 0 && (
              <span className="access-delta access-delta-plus">
                {t('docAccess.summary.sharedOnly', { count: s.shared_only })}
              </span>
            )}
            {s.hidden > 0 && (
              <span className="access-delta access-delta-minus">
                {t('docAccess.summary.hidden', { count: s.hidden })}
              </span>
            )}
          </span>
        </div>
      )}
      <div className="access-summary-line">
        <span className="access-summary-label">{t('docAccess.summary.inherited')}</span>
        <AccessSources sources={s.sources} />
      </div>
      <div className="access-summary-line">
        <span className="access-summary-label">{t('docAccess.summary.exceptions')}</span>
        <span>
          {s.allow_rules + s.deny_rules === 0
            ? t('docAccess.summary.noExceptions')
            : t('docAccess.summary.someExceptions', { allow: s.allow_rules, deny: s.deny_rules })}
        </span>
      </div>
    </div>
  )
}
