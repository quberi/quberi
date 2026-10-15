import { ChevronDown, Plus, Users } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { listMyOrganizations, switchOrganization } from '../../features/auth/api'
import { useAuth } from '../../features/auth/AuthContext'

/**
 * Rail brand-area organization switcher. Replaces the static "brand name /
 * workspace" label with a clickable trigger once the signed-in identity
 * belongs to more than one org (created or invited into). For a single-org
 * identity (the common case) it falls back to the old static label so the
 * rail doesn't sprout a pointless dropdown affordance.
 */
export function OrgSwitcher({ label, sublabel }: { label: string; sublabel: string }) {
  const { t } = useTranslation()
  const { credentials, signInWithSession, isGuest } = useAuth()
  const [open, setOpen] = useState(false)
  const [orgs, setOrgs] = useState<
    { organization_id: string; name: string; tier: string; is_current: boolean }[]
  >([])
  const [busy, setBusy] = useState(false)
  const anchorRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!credentials || isGuest) return
    let cancelled = false
    listMyOrganizations(credentials)
      .then((res) => {
        if (!cancelled) setOrgs(res.items)
      })
      .catch(() => {
        /* non-critical: fall back to the static label */
      })
    return () => {
      cancelled = true
    }
  }, [credentials, isGuest])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (anchorRef.current && !anchorRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const pick = async (organizationId: string) => {
    if (!credentials || busy || organizationId === credentials.organizationSlug) return
    setBusy(true)
    try {
      const { token, user } = await switchOrganization(credentials, organizationId)
      signInWithSession(token, user)
      setOpen(false)
    } catch {
      /* leave the menu open so the user can retry */
    } finally {
      setBusy(false)
    }
  }

  // Guests, or an identity that only belongs to one org, get the plain
  // static label — no dropdown affordance for nothing to switch between.
  if (isGuest || orgs.length < 2) {
    return (
      <div className="rail-brand">
        <span className="rail-brand-mark" aria-hidden="true">
          <BrandLogo />
        </span>
        <span className="rail-brand-label">
          <strong>{label}</strong>
          <small>{sublabel}</small>
        </span>
      </div>
    )
  }

  return (
    <div className="rail-brand rail-brand-switcher" ref={anchorRef}>
      <button
        className="rail-brand-trigger"
        onClick={() => setOpen((o) => !o)}
        type="button"
      >
        <span className="rail-brand-mark" aria-hidden="true">
          <BrandLogo />
        </span>
        <span className="rail-brand-label">
          <strong>{label}</strong>
          <small>{sublabel}</small>
        </span>
        <ChevronDown size={14} className="rail-brand-chevron" />
      </button>
      {open && (
        <div className="rail-org-menu" role="listbox">
          {/* Only the *other* orgs are pickable — the current one is
              already shown in the trigger above, listing it again as a
              "switch to" option would be redundant and confusing. */}
          {orgs
            .filter((o) => !o.is_current)
            .map((o) => (
              <button
                className="rail-org-menu-item"
                disabled={busy}
                key={o.organization_id}
                onClick={() => void pick(o.organization_id)}
                role="option"
                type="button"
              >
                <span className="rail-org-menu-icon" aria-hidden="true">
                  <Users size={13} />
                </span>
                <span className="rail-org-menu-name">{o.name}</span>
              </button>
            ))}
          <div className="rail-org-menu-hint">
            <Plus size={12} />
            <span>{t('orgSwitcher.createHint')}</span>
          </div>
        </div>
      )}
    </div>
  )
}

function BrandLogo() {
  return <img src="/logo.svg" alt="" />
}
