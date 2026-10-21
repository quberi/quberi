import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

export interface ConsentOrganization {
  organization_id: string
  name: string
  tier: 'personal' | 'team'
  is_current: boolean
  external_tools_enabled: boolean
  /** An owner/admin may switch outside tools on from the consent page. */
  can_enable: boolean
}

export interface ConsentView {
  client_id: string
  client_name: string
  scope: string
  /** Validated by the server; the only address this page may send the browser to. */
  redirect_uri: string
  organizations: ConsentOrganization[]
}

/** The request a tool opens the consent page with (read from the address bar). */
export interface AuthorizeParams {
  clientId: string
  redirectUri: string
  scope: string
  state: string
  codeChallenge: string
  codeChallengeMethod: string
}

export function getConsent(
  credentials: DevCredentials,
  params: AuthorizeParams,
): Promise<ConsentView> {
  const query = new URLSearchParams({
    client_id: params.clientId,
    redirect_uri: params.redirectUri,
  })
  if (params.scope) query.set('scope', params.scope)
  return apiFetch<ConsentView>(`/api/v1/oauth/consent?${query.toString()}`, { credentials })
}

/** Approve: returns the address to send the browser to (it carries the one-time code). */
export function authorize(
  credentials: DevCredentials,
  params: AuthorizeParams,
  organizationId: string,
): Promise<{ redirect_to: string }> {
  return apiFetch<{ redirect_to: string }>('/api/v1/oauth/authorize', {
    method: 'POST',
    credentials,
    body: {
      client_id: params.clientId,
      redirect_uri: params.redirectUri,
      ...(params.scope ? { scope: params.scope } : {}),
      state: params.state,
      code_challenge: params.codeChallenge,
      code_challenge_method: params.codeChallengeMethod || 'S256',
      organization_id: organizationId,
    },
  })
}

export function enableExternalTools(
  credentials: DevCredentials,
  organizationId: string,
): Promise<void> {
  return apiFetch<void>('/api/v1/oauth/enable-external-tools', {
    method: 'POST',
    credentials,
    body: { organization_id: organizationId },
  })
}

export interface ExternalGrant {
  id: string
  client_name: string
  /** Which program is calling, as it introduced itself (display only); null until first connect. */
  caller_label?: string | null
  organization_id: string
  organization_name: string
  scope: string
  created_at: string
  last_used_at: string | null
  expires_at: string
}

export function listExternalAccess(
  credentials: DevCredentials,
): Promise<{ items: ExternalGrant[] }> {
  return apiFetch<{ items: ExternalGrant[] }>('/api/v1/me/external-access', { credentials })
}

export function revokeExternalAccess(
  credentials: DevCredentials,
  grantId: string,
): Promise<void> {
  return apiFetch<void>(`/api/v1/me/external-access/${grantId}`, {
    method: 'DELETE',
    credentials,
  })
}
