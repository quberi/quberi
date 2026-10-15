import { Briefcase, Network, Users } from 'lucide-react'
import type { ComponentType } from 'react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useGroups } from '../groups/GroupsContext'
import { GroupsPanel } from '../groups/GroupsPanel'
import { useOrgUnits } from '../orgUnits/OrgUnitsContext'
import { OrgUnitsPanel } from '../orgUnits/OrganizationPanel'
import { usePositions } from '../positions/PositionsContext'
import { PositionsPanel } from '../positions/PositionsPanel'

/**
 * The "Organization" workspace view.
 *
 * Owns its own sub-tab state internally: the app-level ContextPanel that
 * previously hosted the sub-nav is gone, so the tab bar lives inline at
 * the top of this panel. Each tab shows a live count of its entities.
 */

type OrgSubTab = 'units' | 'positions' | 'groups'

interface TabEntry {
  key: OrgSubTab
  icon: ComponentType<{ size?: number }>
  count: number | null
}

export function OrganizationPanel() {
  const { t } = useTranslation()
  const [subTab, setSubTab] = useState<OrgSubTab>('units')

  const { units } = useOrgUnits()
  const { positions } = usePositions()
  const { groups } = useGroups()

  const entries: TabEntry[] = [
    {
      key: 'units',
      icon: Network,
      count: units.kind === 'ready' ? units.items.length : null,
    },
    {
      key: 'positions',
      icon: Briefcase,
      count: positions.kind === 'ready' ? positions.items.length : null,
    },
    {
      key: 'groups',
      icon: Users,
      count: groups.kind === 'ready' ? groups.items.length : null,
    },
  ]

  return (
    <section className="panel org-panel">
      <div className="chat-panel-header org-panel-header">
        <div>
          <span className="section-label">
            <Network size={15} /> {t('organization.title')}
          </span>
          <p>{t('organization.description')}</p>
        </div>
        <nav className="org-tab-bar" aria-label={t('organization.tabsLabel')}>
          {entries.map(({ key, icon: Icon, count }) => (
            <button
              aria-current={subTab === key ? 'page' : undefined}
              className={subTab === key ? 'org-tab active' : 'org-tab'}
              key={key}
              onClick={() => setSubTab(key)}
              type="button"
            >
              <Icon size={13} />
              <span>{t(`organization.tabs.${key}`)}</span>
              <span className="org-tab-count">{count ?? '—'}</span>
            </button>
          ))}
        </nav>
      </div>

      {subTab === 'units' && <OrgUnitsPanel />}
      {subTab === 'positions' && <PositionsPanel />}
      {subTab === 'groups' && <GroupsPanel />}
    </section>
  )
}
