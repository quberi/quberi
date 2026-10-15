import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

const BASE = '/api/v1/me/ai-providers'

// Mirrors backend PrivateProviderBindingView (api/v1/me_ai_providers.py).
export interface PrivateProviderBinding {
  id: string
  provider_kind: string
  display_name: string
  base_url: string | null
  api_key_present: boolean
  status: string
  models: string[]
  created_at: string
  updated_at: string
}

export function listMyProviders(
  credentials: DevCredentials,
): Promise<{ items: PrivateProviderBinding[] }> {
  return apiFetch<{ items: PrivateProviderBinding[] }>(BASE, { credentials })
}

export function createMyProvider(
  credentials: DevCredentials,
  input: {
    provider_kind: string
    display_name: string
    base_url?: string | null
    api_key?: string | null
    models?: string[]
  },
): Promise<PrivateProviderBinding> {
  return apiFetch<PrivateProviderBinding>(BASE, {
    method: 'POST',
    body: input,
    credentials,
  })
}

export function updateMyProvider(
  credentials: DevCredentials,
  bindingId: string,
  input: {
    display_name?: string
    base_url?: string | null
    api_key?: string
    models?: string[]
  },
): Promise<PrivateProviderBinding> {
  return apiFetch<PrivateProviderBinding>(`${BASE}/${bindingId}`, {
    method: 'PATCH',
    body: input,
    credentials,
  })
}

export function deleteMyProvider(
  credentials: DevCredentials,
  bindingId: string,
): Promise<void> {
  return apiFetch<void>(`${BASE}/${bindingId}`, {
    method: 'DELETE',
    credentials,
  })
}

export function probeMyModels(
  credentials: DevCredentials,
  input: { base_url?: string; api_key?: string; binding_id?: string },
): Promise<{ models: string[] }> {
  return apiFetch<{ models: string[] }>(`${BASE}/probe-models`, {
    method: 'POST',
    body: input,
    credentials,
    timeoutMs: 25_000,
  })
}
