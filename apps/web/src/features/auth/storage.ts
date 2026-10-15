import type { DevCredentials } from './types'

const STORAGE_KEY = 'quberi.dev.credentials'

/**
 * Read persisted credentials. Accepts either a real session blob
 * (``sessionToken`` present) or the legacy dev blob (``adminToken`` +
 * org + email). Returns ``null`` for missing/malformed data — the caller
 * routes the user to the sign-in page.
 */
export function loadCredentials(): DevCredentials | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<DevCredentials>

    // Real session blob.
    if (parsed.sessionToken) {
      return {
        adminToken: parsed.adminToken ?? '',
        organizationSlug: parsed.organizationSlug ?? '',
        userEmail: parsed.userEmail ?? '',
        sessionToken: parsed.sessionToken,
        displayName: parsed.displayName,
        tier: parsed.tier ?? 'team',
        isGuest: parsed.isGuest ?? false,
        orgRole: parsed.orgRole ?? '',
      }
    }

    // Legacy dev impersonation blob.
    if (parsed.adminToken && parsed.organizationSlug && parsed.userEmail) {
      return {
        adminToken: parsed.adminToken,
        organizationSlug: parsed.organizationSlug,
        userEmail: parsed.userEmail,
      }
    }
    return null
  } catch {
    return null
  }
}

export function saveCredentials(creds: DevCredentials): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(creds))
}

export function clearCredentials(): void {
  window.localStorage.removeItem(STORAGE_KEY)
}
