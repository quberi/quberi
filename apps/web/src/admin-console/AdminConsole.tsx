import {
  ArrowLeft,
  Cpu,
  LayoutDashboard,
  Library,
  Network,
  Plug,
  ScrollText,
  Users,
} from 'lucide-react'
import type { ComponentType, ReactNode } from 'react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { AuditPanel } from '../features/audit/AuditPanel'
import { useAuth } from '../features/auth/AuthContext'
import { IntegrationsPanel } from '../features/integrations/IntegrationsPanel'
import { MembersPanel } from '../features/members/MembersPanel'
import { ModelSettingsPanel } from '../features/settings/ModelSettingsPanel'
import { OrganizationPanel } from '../features/organization/OrganizationPanel'
import { OverviewDashboard } from '../features/overview/OverviewDashboard'
import { OrgSpacesPanel } from '../features/spaces/OrgSpacesPanel'

export type AdminView =
  | 'overview'
  | 'organization'
  | 'members'
  | 'spaces'
  | 'models'
  | 'integrations'
  | 'audit'

interface AdminNavItem {
  key: AdminView
  icon: ComponentType<{ size?: number; strokeWidth?: number }>
}

const ADMIN_NAV: AdminNavItem[] = [
  { key: 'overview', icon: LayoutDashboard },
  { key: 'organization', icon: Network },
  { key: 'members', icon: Users },
  { key: 'spaces', icon: Library },
  { key: 'models', icon: Cpu },
  { key: 'integrations', icon: Plug },
  { key: 'audit', icon: ScrollText },
]

/**
 * The organisation admin console — a second top-level "mode" alongside the
 * day-to-day workspace (see ``App.tsx``'s ``ModeSwitcher``). Houses every
 * org-management surface that used to live in the workspace rail:
 * organisation structure, members, model governance, integrations.
 *
 * Deliberately reuses the exact same CSS vocabulary as the workspace shell
 * (``app-shell`` / ``rail`` / ``topbar`` / ``main-content`` / ``panel`` /
 * etc. from ``styles.css``) rather than introducing a parallel visual
 * language — the brief was "same look as the customer-facing app", not a
 * separate design system. The only new class is ``rail-exit`` for the
 * "back to workspace" affordance.
 *
 * Entry is gated client-side by ``AppShell`` (owner/admin, team tier only)
 * and re-checked server-side on every single endpoint this console calls —
 * this component itself does no authorization, it just can't be *reached*
 * by anyone the server would reject anyway.
 */
export function AdminConsole({ onExit }: { onExit: () => void }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [view, setView] = useState<AdminView>('overview')
  // Which enterprise this console is administering — shown in the rail and
  // the top bar so an admin who belongs to several orgs always knows. Falls
  // back to the slug for dev-impersonation credentials, which carry no name.
  const orgName =
    credentials?.organizationName || credentials?.organizationSlug || t('adminConsole.title')

  return (
    <div className="app-shell admin-console rail-expanded">
      <div className="ambient ambient-one" aria-hidden="true" />
      <div className="ambient ambient-two" aria-hidden="true" />

      <aside className="rail" aria-label="Admin console navigation">
        <div className="rail-brand">
          <span className="rail-brand-mark" aria-hidden="true">
            <img src="/logo.svg" alt="" />
          </span>
          <span className="rail-brand-label" title={orgName}>
            <strong>{orgName}</strong>
            <small>{t('adminConsole.title')}</small>
          </span>
        </div>

        <div className="rail-group">
          {ADMIN_NAV.map(({ key, icon: Icon }) => {
            const active = view === key
            return (
              <button
                aria-current={active ? 'page' : undefined}
                aria-label={t(`adminConsole.nav.${key}`)}
                className={active ? 'rail-item active' : 'rail-item'}
                key={key}
                onClick={() => setView(key)}
                title={t(`adminConsole.nav.${key}`)}
                type="button"
              >
                {active && <span className="rail-item-glyph" aria-hidden="true" />}
                <span className="rail-item-icon">
                  <Icon size={18} strokeWidth={1.9} />
                </span>
                <span className="rail-item-label">{t(`adminConsole.nav.${key}`)}</span>
              </button>
            )
          })}
        </div>

        <div className="rail-spacer" />

        <button className="rail-item rail-exit" onClick={onExit} type="button">
          <span className="rail-item-icon">
            <ArrowLeft size={18} strokeWidth={1.9} />
          </span>
          <span className="rail-item-label">{t('adminConsole.exit')}</span>
        </button>
      </aside>

      <header className="topbar">
        <div className="workspace-path">
          <span className="live-dot" />
          <strong>{orgName}</strong>
          <i aria-hidden="true">/</i>
          <span>{t(`adminConsole.nav.${view}`)}</span>
        </div>
      </header>

      <main className="main-content main-content-wide">
        <AdminViewSwitch onNavigate={setView} view={view} />
      </main>
    </div>
  )
}

function AdminViewSwitch({
  view,
  onNavigate,
}: {
  view: AdminView
  onNavigate: (to: AdminView) => void
}): ReactNode {
  switch (view) {
    case 'overview':
      return <OverviewDashboard onNavigate={onNavigate} />
    case 'organization':
      return <OrganizationPanel />
    case 'members':
      return <MembersPanel />
    case 'spaces':
      return <OrgSpacesPanel />
    case 'models':
      return <ModelSettingsPanel />
    case 'integrations':
      return <IntegrationsPanel />
    case 'audit':
      return <AuditPanel />
  }
}
