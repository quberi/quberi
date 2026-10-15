import type { SessionUser } from './AuthContext'

/**
 * Full-page Google sign-in.
 *
 * Instead of a small popup, the whole tab goes to Google. The API callback
 * then redirects back to the app with the outcome in the URL *fragment*:
 *
 *   #google_login=<base64url JSON {token, user}>   — success
 *   #google_login_error=<url-encoded message>      — failure / cancelled
 *
 * A fragment is never sent to a server or written to access logs. This
 * module reads it once at startup, strips it from the address bar straight
 * away, and puts the user back on the page they were on before signing in.
 */

const OK_PREFIX = 'google_login='
const ERROR_PREFIX = 'google_login_error='
const RETURN_PATH_KEY = 'quberi.postLoginPath'

export type GoogleRedirectResult =
  | { kind: 'ok'; token: string; user: SessionUser }
  | { kind: 'error'; message: string }

/** Remember where the user was, so a successful sign-in lands them back there. */
export function rememberReturnPath(): void {
  try {
    window.sessionStorage.setItem(
      RETURN_PATH_KEY,
      window.location.pathname + window.location.search,
    )
  } catch {
    /* storage unavailable (private mode): fall back to the home page */
  }
}

function decodeBase64UrlJson(value: string): unknown {
  const b64 = value.replace(/-/g, '+').replace(/_/g, '/')
  const binary = window.atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0))
  return JSON.parse(new TextDecoder().decode(bytes))
}

function isSessionUser(value: unknown): value is SessionUser {
  if (typeof value !== 'object' || value === null) return false
  const u = value as Record<string, unknown>
  return (
    typeof u.user_id === 'string' &&
    typeof u.email === 'string' &&
    typeof u.organization_slug === 'string'
  )
}

function readAndClear(): GoogleRedirectResult | null {
  const hash = window.location.hash.replace(/^#/, '')
  const isOk = hash.startsWith(OK_PREFIX)
  const isError = hash.startsWith(ERROR_PREFIX)
  if (!isOk && !isError) return null

  let result: GoogleRedirectResult
  if (isOk) {
    try {
      const payload = decodeBase64UrlJson(hash.slice(OK_PREFIX.length)) as {
        token?: unknown
        user?: unknown
      }
      result =
        typeof payload.token === 'string' && isSessionUser(payload.user)
          ? { kind: 'ok', token: payload.token, user: payload.user }
          : { kind: 'error', message: 'Sign-in response was incomplete.' }
    } catch {
      result = { kind: 'error', message: 'Sign-in response could not be read.' }
    }
  } else {
    let message = 'Google sign-in failed.'
    try {
      message = decodeURIComponent(hash.slice(ERROR_PREFIX.length)) || message
    } catch {
      /* keep the generic message */
    }
    result = { kind: 'error', message }
  }

  // Leave the address bar clean (and drop the token from history) at once.
  let target = window.location.pathname + window.location.search
  try {
    const remembered = window.sessionStorage.getItem(RETURN_PATH_KEY)
    window.sessionStorage.removeItem(RETURN_PATH_KEY)
    // Same-origin paths only; ignore anything that could be protocol-relative.
    const sameOrigin = remembered !== null && /^\/(?!\/)/.test(remembered)
    // A failure also goes back to a tool's consent page, so the person sees
    // the error where they were and can try again; other pages keep the old behaviour.
    const backToConsent = sameOrigin && remembered.startsWith('/oauth/authorize')
    if (sameOrigin && remembered && (result.kind === 'ok' || backToConsent)) {
      target = remembered
    }
  } catch {
    /* ignore */
  }
  window.history.replaceState(null, '', target)
  return result
}

// Read at most once per page load. React may run state initialisers twice in
// development, and the first read removes the fragment, so memoise it.
let cached: GoogleRedirectResult | null | undefined

export function consumeGoogleRedirectResult(): GoogleRedirectResult | null {
  if (cached === undefined) cached = readAndClear()
  return cached
}

/** The failure message from a redirect sign-in, if that is what just happened. */
export function googleRedirectError(): string | null {
  const result = consumeGoogleRedirectResult()
  return result?.kind === 'error' ? result.message : null
}
