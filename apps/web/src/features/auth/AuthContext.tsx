import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useMemo, useState } from 'react'

import { consumeGoogleRedirectResult } from './googleRedirect'
import { clearCredentials, loadCredentials, saveCredentials } from './storage'
import type { DevCredentials } from './types'

export interface SessionUser {
  user_id: string
  email: string
  display_name: string
  organization_id: string
  organization_slug: string
  organization_name?: string
  organization_tier?: 'personal' | 'team'
  is_guest?: boolean
  /** Direct org-level role: "owner" / "admin" / "member" / "" for none. */
  org_role?: string
}

interface AuthContextValue {
  credentials: DevCredentials | null
  /** True while the current session is an anonymous guest (demo, read-only). */
  isGuest: boolean
  signIn: (creds: DevCredentials) => void
  signInWithSession: (token: string, user: SessionUser) => void
  /** Guest session: held in memory only (short-lived; re-fetched on reload). */
  signInAsGuest: (token: string, user: SessionUser) => void
  /** Patch the cached display name (after the user edits it on the account page). */
  updateDisplayName: (displayName: string) => void
  signOut: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

function sessionCredentials(token: string, user: SessionUser): DevCredentials {
  return {
    adminToken: '',
    organizationSlug: user.organization_slug,
    organizationName: user.organization_name,
    userEmail: user.email,
    sessionToken: token,
    displayName: user.display_name,
    tier: user.organization_tier ?? 'team',
    isGuest: user.is_guest ?? false,
    orgRole: user.org_role ?? '',
  }
}

export function AuthProvider({ children }: PropsWithChildren) {
  const [credentials, setCredentials] = useState<DevCredentials | null>(() => {
    // Returning from a full-page Google sign-in: the session is in the URL
    // fragment, so sign in straight away — no guest session, no flash of the
    // logged-out app.
    const redirected = consumeGoogleRedirectResult()
    if (redirected?.kind === 'ok') {
      const creds = sessionCredentials(redirected.token, redirected.user)
      saveCredentials(creds)
      return creds
    }
    return loadCredentials()
  })

  const signIn = useCallback((next: DevCredentials) => {
    saveCredentials(next)
    setCredentials(next)
  }, [])

  const signInWithSession = useCallback((token: string, user: SessionUser) => {
    const creds = sessionCredentials(token, user)
    saveCredentials(creds)
    setCredentials(creds)
  }, [])

  const signInAsGuest = useCallback((token: string, user: SessionUser) => {
    // Guest sessions are short-lived and re-minted on every load, so we keep
    // them in memory only — never persisted, never a stale token on reload.
    setCredentials({
      adminToken: '',
      organizationSlug: user.organization_slug,
      organizationName: user.organization_name,
      userEmail: user.email,
      sessionToken: token,
      displayName: user.display_name,
      tier: user.organization_tier ?? 'personal',
      isGuest: true,
      orgRole: '',
    })
  }, [])

  const updateDisplayName = useCallback((displayName: string) => {
    setCredentials((prev) => {
      if (!prev) return prev
      const next = { ...prev, displayName }
      // Persist only for real (non-guest) sessions, mirroring sign-in behaviour.
      if (!next.isGuest) saveCredentials(next)
      return next
    })
  }, [])

  const signOut = useCallback(() => {
    clearCredentials()
    setCredentials(null)
  }, [])

  const value = useMemo(
    () => ({
      credentials,
      isGuest: credentials?.isGuest ?? false,
      signIn,
      signInWithSession,
      signInAsGuest,
      updateDisplayName,
      signOut,
    }),
    [credentials, signIn, signInWithSession, signInAsGuest, updateDisplayName, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return value
}
