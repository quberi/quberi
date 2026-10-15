/**
 * Feishu integration REST wrappers (OAuth + folder browse).
 * In lock-step with apps/api/src/quberi_api/api/v1/integrations/feishu.py.
 */

import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'

export interface FeishuConnectionStatus {
  connected: boolean
  external_user_id: string | null
  scopes: string | null
  expires_at: string | null
}

export interface FeishuFolderNode {
  token: string
  name: string
  type: string
  has_children: boolean
}

export interface FeishuFolderBrowse {
  items: FeishuFolderNode[]
  needs_auth: boolean
}

export function feishuOAuthStatus(
  credentials: DevCredentials,
): Promise<FeishuConnectionStatus> {
  return apiFetch<FeishuConnectionStatus>(
    '/api/v1/integrations/feishu/oauth/status',
    { credentials },
  )
}

export function feishuAuthorizeUrl(
  credentials: DevCredentials,
): Promise<{ authorize_url: string }> {
  return apiFetch<{ authorize_url: string }>(
    '/api/v1/integrations/feishu/oauth/authorize-url',
    { method: 'POST', body: {}, credentials },
  )
}

export function feishuBrowseFolders(
  credentials: DevCredentials,
  folderToken?: string,
): Promise<FeishuFolderBrowse> {
  const suffix = folderToken
    ? `?folder_token=${encodeURIComponent(folderToken)}`
    : ''
  return apiFetch<FeishuFolderBrowse>(
    `/api/v1/integrations/feishu/folders${suffix}`,
    { credentials },
  )
}
