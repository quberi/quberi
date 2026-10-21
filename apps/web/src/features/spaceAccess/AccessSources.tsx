import { useTranslation } from 'react-i18next'

import type { AccessSource, AccessSubjectType } from './types'

// The order people think in: everyone, then structure, then individuals.
const ORDER: AccessSubjectType[] = ['organization', 'organization_unit', 'position', 'group', 'user']
const SHOWN_PEOPLE = 4

/**
 * What a space is open to, grouped by kind: the whole organisation, departments (organisation
 * units), positions, groups and individual people. Individual names are capped so a space
 * shared with fifty people by name does not push everything else off the screen.
 */
export function AccessSources({ sources }: { sources: AccessSource[] }) {
  const { t } = useTranslation()
  if (sources.length === 0) {
    return <span className="access-sources-none">{t('spaceAccess.summary.nobody')}</span>
  }
  return (
    <div className="access-sources">
      {ORDER.map((kind) => {
        const items = sources.filter((s) => s.subject_type === kind)
        if (items.length === 0) return null
        const shown = kind === 'user' ? items.slice(0, SHOWN_PEOPLE) : items
        const more = items.length - shown.length
        return (
          <div className="access-sources-row" key={kind}>
            <span className="access-sources-kind">{t(`spaceAccess.subjectTypeShort.${kind}`)}</span>
            <span className="access-sources-chips">
              {shown.map((s) => (
                <span className={`access-chip role-${s.role_code}`} key={`${s.subject_type}-${s.subject_id}`}>
                  {kind === 'organization' ? t('spaceAccess.summary.wholeOrg') : s.name}
                  <small>{t(`members.role.${s.role_code}`)}</small>
                </span>
              ))}
              {more > 0 && (
                <span className="access-chip access-chip-more">
                  {t('spaceAccess.summary.moreUsers', { count: more })}
                </span>
              )}
            </span>
          </div>
        )
      })}
    </div>
  )
}
