import {
  Bot,
  ChevronsLeft,
  ChevronsRight,
  Cpu,
  Database,
  Info,
  Languages,
  LogIn,
  LogOut,
  MessageSquareText,
  ShieldCheck,
  UserRound,
} from 'lucide-react'
import type { ComponentType, PropsWithChildren, ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { WorkspaceView } from '../../app/App'
import { useAuth } from '../../features/auth/AuthContext'
import { OrgSwitcher } from './OrgSwitcher'
import { localizedName } from '../../i18n/localizedName'

/**
 * Three-column shell.
 *
 *   Rail (56px OR 220px)   ContextPanel (264px)   MainContent (rest)
 *
 * The Rail has two width modes:
 *
 * * ``collapsed`` (default) — 56px, icons only. Brand mark alone, item
 *   labels hidden.
 * * ``expanded`` — 220px, icons + labels. Brand mark grows to include
 *   the organisation name. Each nav row shows its label next to the
 *   icon. Toggle sits at the bottom of the Rail so it doesn't get
 *   confused with a nav item.
 *
 * State is persisted in ``localStorage``.
 */

interface RailItem {
  key: string
  icon: ComponentType<{ size?: number; strokeWidth?: number }>
  view: WorkspaceView
}

// Organization management (org structure, members, integrations, model
// governance) lives in the admin console now — see ``AdminConsole`` — not
// in this rail. The workspace rail only carries personal/day-to-day items;
// every one of these shows for both personal and team tiers.
const ACTIVE_RAIL: RailItem[] = [
  { key: 'chat', icon: MessageSquareText, view: 'chat' },
  { key: 'agents', icon: Bot, view: 'agents' },
  { key: 'knowledge', icon: Database, view: 'knowledge' },
  { key: 'myModels', icon: Cpu, view: 'myModels' },
]

// Every current workspace view renders in the wide content column. Kept
// as a set (rather than always-true) so a future narrow-measure,
// reading-oriented view can opt out without touching the layout switch.
const WIDE_VIEWS = new Set<WorkspaceView>([
  'chat',
  'agents',
  'knowledge',
  'myModels',
  'account',
])

interface Props extends PropsWithChildren {
  currentView: WorkspaceView
  onSelectView: (view: WorkspaceView) => void
  headerSlot?: ReactNode
  /** Open the login/signup modal (used by the guest username slot). */
  onOpenAuth?: () => void
  /** Switch the whole app into the admin console. Only rendered as an
   * entry point for owners/admins of a team-tier org. */
  onEnterAdmin?: () => void
}

const RAIL_EXPANDED_KEY = 'quberi.railExpanded'

export function AppShell({
  children,
  currentView,
  onSelectView,
  headerSlot,
  onOpenAuth,
  onEnterAdmin,
}: Props) {
  const { i18n, t } = useTranslation()
  const { credentials, isGuest, signOut } = useAuth()
  const currentLocale = i18n.resolvedLanguage === 'zh-CN' ? 'zh-CN' : 'en-US'

  const [railExpanded, setRailExpanded] = useState<boolean>(() => {
    const stored = window.localStorage.getItem(RAIL_EXPANDED_KEY)
    return stored === '1'
  })
  useEffect(() => {
    window.localStorage.setItem(RAIL_EXPANDED_KEY, railExpanded ? '1' : '0')
  }, [railExpanded])

  const switchLocale = () => {
    const nextLocale = currentLocale === 'en-US' ? 'zh-CN' : 'en-US'
    window.localStorage.setItem('quberi.locale', nextLocale)
    void i18n.changeLanguage(nextLocale)
  }

  const toggleLabel = railExpanded
    ? t('navigation.collapsePanel')
    : t('navigation.expandPanel')

  const isPersonal = credentials?.tier === 'personal'
  const railItems = ACTIVE_RAIL
  // Personal: show the product wordmark (the user's own name lives in the
  // bottom user slot). Team: show the organisation's real name (falling
  // back to the slug for dev-impersonation credentials, which never carry
  // a name).
  const rawOrgName =
    credentials?.organizationName || credentials?.organizationSlug || t('brand.name')
  // The demo org's name is stored as "中文 / English"; show the matching half
  // to guests. A signed-in team's own name is shown exactly as they typed it.
  const orgLabel = isPersonal
    ? t('brand.name')
    : credentials?.isGuest
      ? localizedName(rawOrgName, currentLocale)
      : rawOrgName

  // Admin console entry: only a direct owner/admin of a team-tier org, and
  // never for guests. Personal orgs have no org concepts to administer, so
  // the console simply doesn't apply there. ``orgRole`` is server-issued
  // (see auth.get_direct_org_role) and re-checked on every admin-console
  // API call regardless — this only decides whether to *show* the button.
  const canEnterAdmin =
    !isGuest &&
    !isPersonal &&
    (credentials?.orgRole === 'owner' || credentials?.orgRole === 'admin')

  return (
    <div className={railExpanded ? 'app-shell rail-expanded' : 'app-shell'}>
      <div className="ambient ambient-one" aria-hidden="true" />
      <div className="ambient ambient-two" aria-hidden="true" />

      <aside className="rail" aria-label="Primary navigation">
        {/* Brand mark. In expanded mode the wordmark grows into a
            second column showing the current organisation. Becomes a
            clickable switcher once the identity belongs to 2+ orgs. */}
        <OrgSwitcher label={orgLabel} sublabel={t('brand.workspace')} />

        <div className="rail-group">
          {railItems.map(({ key, icon: Icon, view }) => {
            const active = currentView === view
            return (
              <button
                aria-current={active ? 'page' : undefined}
                aria-label={t(`navigation.${key}`)}
                className={active ? 'rail-item active' : 'rail-item'}
                key={key}
                onClick={() => onSelectView(view)}
                title={t(`navigation.${key}`)}
                type="button"
              >
                {active && <span className="rail-item-glyph" aria-hidden="true" />}
                <span className="rail-item-icon">
                  <Icon size={18} strokeWidth={1.9} />
                </span>
                <span className="rail-item-label">{t(`navigation.${key}`)}</span>
              </button>
            )
          })}
        </div>

        <div className="rail-spacer" />

        {canEnterAdmin && (
          <button
            aria-label={t('navigation.adminConsole')}
            className="rail-item rail-admin-entry"
            onClick={onEnterAdmin}
            title={t('navigation.adminConsole')}
            type="button"
          >
            <span className="rail-item-icon">
              <ShieldCheck size={18} strokeWidth={1.9} />
            </span>
            <span className="rail-item-label">{t('navigation.adminConsole')}</span>
          </button>
        )}

        <button
          aria-label={t('auth.signOut')}
          className="rail-item rail-locale"
          onClick={switchLocale}
          title={currentLocale === 'en-US' ? '中文' : 'English'}
          type="button"
        >
          <span className="rail-item-icon">
            <Languages size={16} />
          </span>
          <span className="rail-item-label">
            {currentLocale === 'en-US' ? '中文' : 'English'}
          </span>
        </button>

        {credentials && isGuest && (
          <button
            className="rail-user rail-user-guest"
            onClick={onOpenAuth}
            title={t('auth.guestCta')}
            type="button"
          >
            <span className="rail-user-avatar" aria-hidden="true">
              <UserRound size={16} />
            </span>
            <span className="rail-user-body">
              <strong>{t('auth.guest')}</strong>
              <small>{t('auth.guestCta')}</small>
            </span>
            <LogIn size={14} />
          </button>
        )}

        {credentials && !isGuest && (
          <div className="rail-user" title={credentials.userEmail}>
            <button
              className="rail-user-main"
              onClick={() => onSelectView('account')}
              title={t('account.open')}
              type="button"
            >
              <span className="rail-user-avatar" aria-hidden="true">
                {(credentials.displayName?.[0] ?? credentials.userEmail[0] ?? '?').toUpperCase()}
              </span>
              <span className="rail-user-body">
                <strong>{credentials.displayName || credentials.userEmail}</strong>
              </span>
            </button>
            <button
              aria-label={t('auth.signOut')}
              className="rail-user-signout"
              onClick={signOut}
              title={t('auth.signOut')}
              type="button"
            >
              <LogOut size={13} />
            </button>
          </div>
        )}

        {/* Expand / collapse toggle. Sits at the very bottom so it
            doesn't compete with the nav items for the eye. */}
        <button
          aria-expanded={railExpanded}
          aria-label={toggleLabel}
          className="rail-toggle"
          onClick={() => setRailExpanded((v) => !v)}
          title={toggleLabel}
          type="button"
        >
          <span className="rail-item-icon">
            {railExpanded ? (
              <ChevronsLeft size={16} />
            ) : (
              <ChevronsRight size={16} />
            )}
          </span>
          <span className="rail-item-label">{toggleLabel}</span>
        </button>
      </aside>

      <header className="topbar">
        <div className="workspace-path">
          <span className="live-dot" />
          <strong>{t(`navigation.${currentView}`)}</strong>
        </div>
        {/* A guest is on a personal trial account, not on an organisation's knowledge base:
            say so on every page, and where to go to see the real thing. */}
        {credentials && isGuest && (
          <div className="guest-trial-note" role="note">
            <Info size={14} aria-hidden="true" />
            <span>{t('auth.guestTrialNote')}</span>
            {onOpenAuth && (
              <button className="link-button" onClick={onOpenAuth} type="button">
                {t('auth.guestTrialSignIn')}
              </button>
            )}
          </div>
        )}
        <div className="topbar-actions">{headerSlot}</div>
      </header>

      <main className={WIDE_VIEWS.has(currentView) ? 'main-content main-content-wide' : 'main-content'}>
        {children}
      </main>
    </div>
  )
}
