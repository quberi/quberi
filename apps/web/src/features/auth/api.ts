import { API_BASE, apiFetch } from '../api/client'
import type { DevCredentials } from './types'

import type { SessionUser } from './AuthContext'

/**
 * Login-or-signup in one: sends a code to the email. Whether that email
 * already has an account is decided at verify time, not here — see
 * ``verifyLoginCode``.
 */
export function requestLoginCode(email: string): Promise<{ ok: boolean; message: string }> {
  return apiFetch<{ ok: boolean; message: string }>('/api/v1/auth/request-code', {
    method: 'POST',
    body: { email },
    // Sending the code goes through SMTP synchronously; give it headroom
    // beyond the default 10s client timeout.
    timeoutMs: 25_000,
  })
}

/**
 * Verify the code; the backend logs into an existing account for this
 * email, or auto-provisions a new personal one if it's brand-new.
 */
export function verifyLoginCode(
  email: string,
  code: string,
): Promise<{ token: string; user: SessionUser }> {
  return apiFetch<{ token: string; user: SessionUser }>('/api/v1/auth/verify', {
    method: 'POST',
    body: { email, code },
  })
}

export function feishuAuthorizeUrl(): Promise<{ authorize_url: string }> {
  return apiFetch<{ authorize_url: string }>('/api/v1/auth/feishu/authorize-url', {
    method: 'POST',
  })
}

/**
 * ``redirect``: the whole tab goes to Google and comes back to the app
 * (see ``googleRedirect.ts``). ``popup``: legacy small-window flow.
 */
export function googleAuthorizeUrl(
  mode: 'popup' | 'redirect' = 'popup',
): Promise<{ authorize_url: string }> {
  return apiFetch<{ authorize_url: string }>('/api/v1/auth/google/authorize-url', {
    method: 'POST',
    body: { mode },
  })
}

/**
 * After Feishu consent for a *new* user, send a verification code to the
 * email they want to bind. Works whether or not that email already has an
 * account (verify then links-or-creates).
 */
export function feishuBindRequestCode(
  ticket: string,
  email: string,
): Promise<{ ok: boolean; message: string }> {
  return apiFetch<{ ok: boolean; message: string }>('/api/v1/auth/feishu/bind/request-code', {
    method: 'POST',
    body: { ticket, email },
    timeoutMs: 25_000,
  })
}

/** Verify the bind email code → create-or-link the Feishu account, sign in. */
export function feishuBindVerify(
  ticket: string,
  email: string,
  code: string,
): Promise<{ token: string; user: SessionUser }> {
  return apiFetch<{ token: string; user: SessionUser }>('/api/v1/auth/feishu/bind/verify', {
    method: 'POST',
    body: { ticket, email, code },
  })
}

export function requestGuestSession(): Promise<{ token: string; user: SessionUser }> {
  return apiFetch<{ token: string; user: SessionUser }>('/api/v1/auth/guest', {
    method: 'POST',
  })
}

export interface MyOrganization {
  organization_id: string
  name: string
  slug: string
  tier: 'personal' | 'team'
  is_current: boolean
  is_pending: boolean
}

/** Every organization the signed-in identity belongs to (own + invited). */
export function listMyOrganizations(
  credentials: DevCredentials,
): Promise<{ items: MyOrganization[] }> {
  return apiFetch<{ items: MyOrganization[] }>('/api/v1/organizations/mine', { credentials })
}

/** Create a new team organization; the caller becomes its owner. */
export function createTeamOrganization(
  credentials: DevCredentials,
  name: string,
): Promise<{ organization_id: string; name: string; slug: string; tier: string }> {
  return apiFetch('/api/v1/organizations', {
    method: 'POST',
    body: { name },
    credentials,
  })
}

/** Re-issue a session bound to a different org the caller already belongs to. */
export function switchOrganization(
  credentials: DevCredentials,
  organizationId: string,
): Promise<{ token: string; user: SessionUser }> {
  return apiFetch<{ token: string; user: SessionUser }>('/api/v1/auth/switch-organization', {
    method: 'POST',
    body: { organization_id: organizationId },
    credentials,
  })
}

/**
 * End this session on the server so the token stops working everywhere. Best effort:
 * the caller drops its own copy of the credentials whether or not this succeeds.
 */
export async function logout(token: string): Promise<void> {
  await fetch(`${API_BASE}/api/v1/auth/logout`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    keepalive: true,
  })
}
