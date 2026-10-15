/**
 * The credential the browser carries for API calls.
 *
 * Two shapes share this type so the rest of the app doesn't branch:
 *
 * - **Real session** (email/Feishu login): ``sessionToken`` is set and is
 *   sent as ``Authorization: Bearer``. ``organizationSlug`` / ``userEmail``
 *   / ``displayName`` are populated for display only.
 * - **Dev impersonation** (non-prod): ``adminToken`` + ``organizationSlug``
 *   + ``userEmail`` are sent as the legacy admin-token + header pair.
 *
 * Persisted in localStorage so a reload keeps you signed in.
 */
export interface DevCredentials {
  adminToken: string
  organizationSlug: string
  userEmail: string
  /** Real session bearer token. When present, preferred over adminToken. */
  sessionToken?: string
  /** Display name for the signed-in user (real session). */
  displayName?: string
  /** Human-readable organization name (real session). Falls back to the
   * slug in display code when absent (dev-impersonation credentials). */
  organizationName?: string
  /** Product tier: "personal" (C-end, org UI hidden) or "team" (enterprise). */
  tier?: 'personal' | 'team'
  /** True for an anonymous guest session (demo org, read-only). */
  isGuest?: boolean
  /**
   * Direct organisation-level role: "owner" / "admin" / "member" / "" for
   * none. Only ever used to decide whether to *show* the admin-console
   * entry point in the UI — every admin-console API call is re-checked
   * server-side regardless of what this says.
   */
  orgRole?: string
}
