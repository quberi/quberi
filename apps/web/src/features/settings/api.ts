import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

// Mirrors backend ProviderBindingView (api/v1/organizations/ai_providers.py).
export interface ProviderBinding {
  id: string
  organization_id: string | null
  provider_kind: string
  /** Which company this binding talks to (deepseek, openai, ...) — null
   * for a binding that predates the vendor field or has none set. */
  vendor: string | null
  display_name: string
  base_url: string | null
  api_key_present: boolean
  status: string
  is_platform_default: boolean
  /** Lower tries first among several platform-default keys for the same
   * vendor. Only meaningful for platform bindings. */
  priority: number
  extra_config: Record<string, unknown>
  models: string[]
  created_at: string
  updated_at: string
}

export interface ModelCapability {
  model_id: string
  provider_kind: string
  purpose: 'chat' | 'embedding' | 'reranker'
  display_name: string
  vector_dim: number | null
  context_window: number
  notes: string
}

export interface ModelSelectionSlot {
  model_id: string | null
  binding_id: string | null
}

export interface ModelSelections {
  chat: ModelSelectionSlot
  embedding: ModelSelectionSlot
  reranker: ModelSelectionSlot
  /** The model that describes pictures; empty until an owner/admin chooses one. */
  vision: ModelSelectionSlot
}

const BASE = '/api/v1/organizations/current'

export interface UsageStatus {
  metered: boolean
  limit: number
  used: number
  remaining: number // -1 when unlimited
  is_guest: boolean
}

export function getUsage(credentials: DevCredentials): Promise<UsageStatus> {
  return apiFetch<UsageStatus>('/api/v1/usage', { credentials })
}

export function listProviderBindings(
  credentials: DevCredentials,
): Promise<{ items: ProviderBinding[] }> {
  return apiFetch<{ items: ProviderBinding[] }>(`${BASE}/ai-providers`, {
    credentials,
  })
}

export function createProviderBinding(
  credentials: DevCredentials,
  input: {
    provider_kind: string
    display_name: string
    base_url?: string | null
    api_key?: string | null
    models?: string[]
  },
): Promise<ProviderBinding> {
  return apiFetch<ProviderBinding>(`${BASE}/ai-providers`, {
    method: 'POST',
    body: input,
    credentials,
  })
}

/** Fetch the OpenAI-compatible /v1/models list for a provider. */
export function probeProviderModels(
  credentials: DevCredentials,
  input: { base_url?: string; api_key?: string; binding_id?: string },
): Promise<{ models: string[] }> {
  return apiFetch<{ models: string[] }>(`${BASE}/ai-providers/probe-models`, {
    method: 'POST',
    body: input,
    credentials,
    timeoutMs: 25_000,
  })
}

export interface UsableChatModel {
  model_id: string
  display_name: string
  binding_id: string
  binding_name: string
  is_platform_default: boolean
  /** Which company serves this model (deepseek, openai, ...) — null for
   * an org-owned/private binding whose vendor wasn't set. */
  vendor: string | null
  /** True for exactly the one platform model that is the org's effective
   * default (QUBERI_DEFAULT_CHAT_MODEL) — disambiguates "the" default
   * now that several platform models can coexist. */
  is_default: boolean
  is_private: boolean
  /** Whether the model reads pictures: only those can be chosen as the vision model. */
  supports_vision?: boolean
}

export function listUsableChatModels(
  credentials: DevCredentials,
  forMe = false,
): Promise<{ items: UsableChatModel[] }> {
  return apiFetch<{ items: UsableChatModel[] }>(
    `${BASE}/ai-usable-chat-models${forMe ? '?for_me=true' : ''}`,
    { credentials },
  )
}

export type ModelAccessEffect = 'allow' | 'deny'

export interface ModelAccessPrincipal {
  principal_type: 'user' | 'group' | 'organization_unit' | 'position'
  principal_id: string
  /** "allow" (default) or "deny" — a deny row excludes that principal
   * even if they also match an allow grant through a different
   * principal (e.g. their group). */
  effect?: ModelAccessEffect
}

export function getModelAccess(
  credentials: DevCredentials,
  bindingId: string,
  modelId: string,
): Promise<{ binding_id: string; model_id: string; principals: ModelAccessPrincipal[] }> {
  const q = `?binding_id=${encodeURIComponent(bindingId)}&model_id=${encodeURIComponent(modelId)}`
  return apiFetch(`${BASE}/ai-model-access${q}`, { credentials })
}

export function setModelAccess(
  credentials: DevCredentials,
  input: { binding_id: string; model_id: string; principals: ModelAccessPrincipal[] },
): Promise<{ binding_id: string; model_id: string; principals: ModelAccessPrincipal[] }> {
  return apiFetch(`${BASE}/ai-model-access`, {
    method: 'PUT',
    body: input,
    credentials,
  })
}

export function updateProviderBinding(
  credentials: DevCredentials,
  bindingId: string,
  input: {
    display_name?: string
    base_url?: string | null
    /** Omit or leave blank to keep the existing key. */
    api_key?: string
    models?: string[]
  },
): Promise<ProviderBinding> {
  return apiFetch<ProviderBinding>(`${BASE}/ai-providers/${bindingId}`, {
    method: 'PATCH',
    body: input,
    credentials,
  })
}

export function deleteProviderBinding(
  credentials: DevCredentials,
  bindingId: string,
): Promise<void> {
  return apiFetch<void>(`${BASE}/ai-providers/${bindingId}`, {
    method: 'DELETE',
    credentials,
  })
}

export function listModels(
  credentials: DevCredentials,
): Promise<ModelCapability[]> {
  return apiFetch<ModelCapability[]>(`${BASE}/ai-models`, { credentials })
}

export function getModelSelections(
  credentials: DevCredentials,
): Promise<ModelSelections> {
  return apiFetch<ModelSelections>(`${BASE}/ai-model-selections`, {
    credentials,
  })
}

export function setModelSelection(
  credentials: DevCredentials,
  purpose: 'chat' | 'embedding' | 'reranker' | 'vision',
  modelId: string,
  bindingId: string,
): Promise<void> {
  return apiFetch<void>(`${BASE}/ai-model-selections/${purpose}`, {
    method: 'PUT',
    body: { model_id: modelId, binding_id: bindingId },
    credentials,
  })
}

export function clearModelSelection(
  credentials: DevCredentials,
  purpose: 'chat' | 'embedding' | 'reranker' | 'vision',
): Promise<void> {
  return apiFetch<void>(`${BASE}/ai-model-selections/${purpose}`, {
    method: 'DELETE',
    credentials,
  })
}

export interface OrganizationSettings {
  allow_byok: boolean
  /** Outside tools (e.g. DeepSeek Harness) may read this organisation's knowledge. */
  allow_external_tools: boolean
  /** Pictures may be sent to the chosen vision model to be described. Off by default. */
  image_vision_enabled?: boolean
}

export function getOrganizationSettings(
  credentials: DevCredentials,
): Promise<OrganizationSettings> {
  return apiFetch<OrganizationSettings>(`${BASE}/settings`, { credentials })
}

export function updateOrganizationSettings(
  credentials: DevCredentials,
  input: {
    allow_byok?: boolean
    allow_external_tools?: boolean
    image_vision_enabled?: boolean
  },
): Promise<OrganizationSettings> {
  return apiFetch<OrganizationSettings>(`${BASE}/settings`, {
    method: 'PATCH',
    body: input,
    credentials,
  })
}
