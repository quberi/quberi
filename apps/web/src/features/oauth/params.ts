import type { AuthorizeParams } from './api'

/** The consent page lives at this path; a tool opens it in the browser. */
export const CONSENT_PATH = '/oauth/authorize'

export function isConsentRoute(pathname: string): boolean {
  return pathname === CONSENT_PATH || pathname === `${CONSENT_PATH}/`
}

/**
 * Read what the tool asked for. ``null`` when the request is missing a piece
 * we cannot do without: in that case the page shows an error and sends the
 * browser nowhere (we never redirect on a request we could not validate).
 */
export function readAuthorizeParams(search: string): AuthorizeParams | null {
  const q = new URLSearchParams(search)
  const clientId = q.get('client_id') ?? ''
  const redirectUri = q.get('redirect_uri') ?? ''
  const codeChallenge = q.get('code_challenge') ?? ''
  if (!clientId || !redirectUri || !codeChallenge) return null
  if (q.get('response_type') && q.get('response_type') !== 'code') return null
  return {
    clientId,
    redirectUri,
    scope: q.get('scope') ?? '',
    state: q.get('state') ?? '',
    codeChallenge,
    codeChallengeMethod: q.get('code_challenge_method') ?? 'S256',
  }
}

/** Where to send the browser when the person says no (RFC 6749 §4.1.2.1). */
export function deniedAddress(redirectUri: string, state: string): string {
  const query = new URLSearchParams({ error: 'access_denied' })
  if (state) query.set('state', state)
  return `${redirectUri}?${query.toString()}`
}
