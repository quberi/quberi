import type { PropsWithChildren } from 'react'
import { createContext, useCallback, useContext, useMemo, useState } from 'react'

import { AuthDialog } from './AuthDialog'
import { googleRedirectError } from './googleRedirect'

interface AuthModalValue {
  /** Open the login/signup modal (e.g. when a guest attempts a write). */
  openAuth: () => void
}

const AuthModalContext = createContext<AuthModalValue | null>(null)

/**
 * Owns the login/signup modal and exposes ``openAuth`` app-wide, so any
 * write CTA can prompt a guest to sign in instead of firing a request the
 * backend would reject.
 */
export function AuthModalProvider({ children }: PropsWithChildren) {
  // If Google just sent the browser back with an error (cancelled consent,
  // expired state, ...), reopen the sign-in dialog showing that message
  // instead of dropping the user on the page with no explanation.
  const [redirectError, setRedirectError] = useState<string | null>(() =>
    googleRedirectError(),
  )
  const [open, setOpen] = useState(() => redirectError !== null)
  const openAuth = useCallback(() => setOpen(true), [])
  const closeAuth = useCallback(() => {
    setOpen(false)
    setRedirectError(null)
  }, [])
  const value = useMemo(() => ({ openAuth }), [openAuth])

  return (
    <AuthModalContext.Provider value={value}>
      {children}
      <AuthDialog open={open} onClose={closeAuth} initialError={redirectError} />
    </AuthModalContext.Provider>
  )
}

export function useAuthModal(): AuthModalValue {
  const value = useContext(AuthModalContext)
  if (value === null) {
    throw new Error('useAuthModal must be used within an AuthModalProvider')
  }
  return value
}
