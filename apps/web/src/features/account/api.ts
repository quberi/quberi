import { API_BASE, apiErrorFrom, apiFetch, buildAuthHeaders } from '../api/client'
import type { DevCredentials } from '../auth/types'

export interface AccountProfile {
  display_name: string
  avatar_url: string | null
  email: string
  email_is_placeholder: boolean
  email_verified: boolean
  has_email_login: boolean
  phone: string | null
  has_phone: boolean
  feishu_linked: boolean
  feishu_account: string | null
  google_linked: boolean
  google_account: string | null
}

/** Absolute URL for an avatar path returned by the API (or null). */
export function avatarSrc(url: string | null): string | null {
  if (!url) return null
  return url.startsWith('http') ? url : `${API_BASE}${url}`
}

export function updateDisplayName(
  credentials: DevCredentials,
  displayName: string,
): Promise<AccountProfile> {
  return apiFetch<AccountProfile>('/api/v1/account/name', {
    method: 'POST',
    body: { display_name: displayName },
    credentials,
  })
}

export async function uploadAvatar(
  credentials: DevCredentials,
  file: File,
): Promise<AccountProfile> {
  const form = new FormData()
  form.append('file', file, file.name)
  const resp = await fetch(`${API_BASE}/api/v1/account/avatar`, {
    method: 'POST',
    headers: buildAuthHeaders(credentials),
    body: form,
  })
  if (!resp.ok) {
    throw await apiErrorFrom(resp)
  }
  return (await resp.json()) as AccountProfile
}

export function getAccount(credentials: DevCredentials): Promise<AccountProfile> {
  return apiFetch<AccountProfile>('/api/v1/account', { credentials })
}

export function requestAccountEmailCode(
  credentials: DevCredentials,
  email: string,
): Promise<{ ok: boolean; message: string }> {
  return apiFetch<{ ok: boolean; message: string }>(
    '/api/v1/account/email/request-code',
    { method: 'POST', body: { email }, credentials, timeoutMs: 25_000 },
  )
}

export function verifyAccountEmail(
  credentials: DevCredentials,
  email: string,
  code: string,
): Promise<AccountProfile> {
  return apiFetch<AccountProfile>('/api/v1/account/email/verify', {
    method: 'POST',
    body: { email, code },
    credentials,
  })
}

export function requestAccountPhoneCode(
  credentials: DevCredentials,
  phone: string,
): Promise<{ ok: boolean; message: string }> {
  return apiFetch<{ ok: boolean; message: string }>(
    '/api/v1/account/phone/request-code',
    { method: 'POST', body: { phone }, credentials, timeoutMs: 25_000 },
  )
}

export function verifyAccountPhone(
  credentials: DevCredentials,
  phone: string,
  code: string,
): Promise<AccountProfile> {
  return apiFetch<AccountProfile>('/api/v1/account/phone/verify', {
    method: 'POST',
    body: { phone, code },
    credentials,
  })
}

export function feishuLinkUrl(
  credentials: DevCredentials,
): Promise<{ authorize_url: string }> {
  return apiFetch<{ authorize_url: string }>('/api/v1/account/feishu/authorize-url', {
    method: 'POST',
    credentials,
  })
}

export function googleLinkUrl(
  credentials: DevCredentials,
): Promise<{ authorize_url: string }> {
  return apiFetch<{ authorize_url: string }>('/api/v1/account/google/authorize-url', {
    method: 'POST',
    credentials,
  })
}

export function googleUnlink(credentials: DevCredentials): Promise<AccountProfile> {
  return apiFetch<AccountProfile>('/api/v1/account/google/unlink', {
    method: 'POST',
    credentials,
  })
}
