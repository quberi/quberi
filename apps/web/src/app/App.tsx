import { useEffect, useState } from 'react'

import { AdminConsole } from '../admin-console/AdminConsole'
import { AppShell } from '../components/layout/AppShell'
import { requestGuestSession } from '../features/auth/api'
import { AuthModalProvider, useAuthModal } from '../features/auth/AuthModalContext'
import { AuthProvider, useAuth } from '../features/auth/AuthContext'
import { SignIn } from '../features/auth/SignIn'
import { AgentPanel } from '../features/agent/AgentPanel'
import { AgentsListView } from '../features/agent/AgentsListView'
import type { AgentSummary } from '../features/agent/types'
import { ChatPanel } from '../features/chat/ChatPanel'
import { ChatProvider } from '../features/chat/ChatContext'
import { KnowledgePanel } from '../features/documents/KnowledgePanel'
import { SpaceKnowledgeView } from '../features/documents/SpaceKnowledgeView'
import { GroupsProvider } from '../features/groups/GroupsContext'
import { MembersProvider } from '../features/members/MembersContext'
import { OrgUnitsProvider } from '../features/orgUnits/OrgUnitsContext'
import { AccountPanel } from '../features/account/AccountPanel'
import { MyModelsPanel } from '../features/settings/MyModelsPanel'
import { PositionsProvider } from '../features/positions/PositionsContext'
import { SpacesProvider } from '../features/spaces/SpacesContext'
import { OAuthConnected, isConnectedRoute } from '../features/oauth/OAuthConnected'
import { OAuthConsent } from '../features/oauth/OAuthConsent'
import { isConsentRoute } from '../features/oauth/params'

export type WorkspaceView =
  | 'chat'
  | 'agents'
  | 'knowledge'
  | 'myModels'
  | 'account'

export function App() {
  return (
    <AuthProvider>
      <Routed />
    </AuthProvider>
  )
}

/**
 * Pick the page from the address.
 *
 * This must run *inside* ``AuthProvider``, not in ``App``: coming back from a
 * full-page Google sign-in, it is the provider's start-up that puts the
 * address bar back to the page the person came from (for the tool consent page,
 * ``/oauth/authorize?...``). Reading the address any earlier sees ``/`` and
 * drops them in the workspace instead of finishing the tool's sign-in.
 */
function Routed() {
  const { pathname } = window.location
  // The page the browser lands on once a tool's sign-in is over: no account needed.
  if (isConnectedRoute(pathname)) return <OAuthConnected />
  // An outside tool (e.g. DeepSeek Harness) opens this page to ask for access.
  if (isConsentRoute(pathname)) return <OAuthConsent />
  return <AuthGate />
}

function AuthGate() {
  const { credentials, signInAsGuest } = useAuth()
  const [guestFailed, setGuestFailed] = useState(false)

  // No credentials → acquire an anonymous guest session so the app shell
  // renders with the read-only demo org ("see and use before you sign in").
  useEffect(() => {
    if (credentials) return
    let cancelled = false
    requestGuestSession()
      .then(({ token, user }) => {
        if (!cancelled) signInAsGuest(token, user)
      })
      .catch(() => {
        if (!cancelled) setGuestFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [credentials, signInAsGuest])

  if (!credentials) {
    // If the guest session couldn't be minted (e.g. API down), fall back to
    // the full sign-in page so people can still log in.
    if (guestFailed) return <SignIn />
    return (
      <div className="app-boot">
        <span className="brand-mark large">
          <img src="/logo.svg" alt="Quberi" />
        </span>
      </div>
    )
  }
  return (
    <AuthModalProvider>
      <SpacesProvider>
        <MembersProvider>
          <OrgUnitsProvider>
            <PositionsProvider>
              <GroupsProvider>
                <ChatProvider>
                  <ModeSwitcher />
                </ChatProvider>
              </GroupsProvider>
            </PositionsProvider>
          </OrgUnitsProvider>
        </MembersProvider>
      </SpacesProvider>
    </AuthModalProvider>
  )
}

/**
 * Top-level mode: the day-to-day "workspace" (chat, knowledge, agents) or
 * the "admin console" (organization, members, model governance,
 * integrations — see ``admin-console/AdminConsole``). Both render inside
 * the same SPA; switching modes is a plain state flip, not a route change,
 * since there is no router. The admin console is its own visually-distinct
 * shell (same design language, different nav) so a signed-in owner/admin
 * always knows which "mode" they're in.
 */
function ModeSwitcher() {
  const [mode, setMode] = useState<'workspace' | 'admin'>('workspace')
  if (mode === 'admin') {
    return <AdminConsole onExit={() => setMode('workspace')} />
  }
  return <Workspace onEnterAdmin={() => setMode('admin')} />
}

function Workspace({ onEnterAdmin }: { onEnterAdmin: () => void }) {
  const { openAuth } = useAuthModal()
  const [view, setViewState] = useState<WorkspaceView>('chat')
  // When set, we're inside the Space Knowledge detail page for that
  // space. Cleared whenever the user navigates via the rail or clicks
  // "back" from the detail header.
  const [knowledgeSpaceId, setKnowledgeSpaceId] = useState<string | null>(
    null,
  )
  // When set, we're inside a specific agent's conversation; cleared to
  // return to the agent gallery.
  const [selectedAgent, setSelectedAgent] = useState<AgentSummary | null>(null)

  const setView = (next: WorkspaceView) => {
    setViewState(next)
    // Any rail navigation drops the detail pages — they belong strictly
    // under their own top-level view.
    if (next !== 'knowledge') setKnowledgeSpaceId(null)
    if (next !== 'agents') setSelectedAgent(null)
  }

  return (
    <AppShell
      currentView={view}
      onSelectView={setView}
      onOpenAuth={openAuth}
      onEnterAdmin={onEnterAdmin}
    >
      {view === 'chat' && <ChatPanel />}
      {view === 'agents' && selectedAgent === null && (
        <AgentsListView onOpen={setSelectedAgent} />
      )}
      {view === 'agents' && selectedAgent !== null && (
        <AgentPanel
          agent={selectedAgent}
          onBack={() => setSelectedAgent(null)}
        />
      )}
      {view === 'knowledge' && knowledgeSpaceId === null && (
        <KnowledgePanel onOpenSpace={setKnowledgeSpaceId} />
      )}
      {view === 'knowledge' && knowledgeSpaceId !== null && (
        <SpaceKnowledgeView
          spaceId={knowledgeSpaceId}
          onBack={() => setKnowledgeSpaceId(null)}
        />
      )}
      {view === 'myModels' && <MyModelsPanel />}
      {view === 'account' && <AccountPanel />}
    </AppShell>
  )
}
