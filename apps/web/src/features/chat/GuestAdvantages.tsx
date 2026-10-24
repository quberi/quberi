/**
 * What a guest sees above the chat: why Quberi, in eight short points.
 *
 * Security leads. The strip shows every point as a chip so nothing needs
 * opening to be seen; a chip (or "see all") opens a drawer with the full
 * wording and a way to sign in. Once the guest has asked something the strip
 * folds down to one line so the conversation gets the room back.
 */
import {
  ChevronRight,
  Eye,
  Layers,
  LogIn,
  Plug,
  Quote,
  ScrollText,
  Server,
  ShieldCheck,
  UserMinus,
  Users,
  X,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { useAuthModal } from '../auth/AuthModalContext'

/** The points, in the order they are shown. Texts: ``chat.guestAdv.items.<key>``. */
export const GUEST_ADVANTAGES: readonly { key: string; Icon: LucideIcon }[] = [
  { key: 'permission', Icon: ShieldCheck },
  { key: 'layers', Icon: Layers },
  { key: 'offboarding', Icon: UserMinus },
  { key: 'review', Icon: Eye },
  { key: 'audit', Icon: ScrollText },
  { key: 'citation', Icon: Quote },
  { key: 'agents', Icon: Plug },
  { key: 'deploy', Icon: Server },
  { key: 'editions', Icon: Users },
]

export function GuestAdvantages({ collapsed }: { collapsed: boolean }) {
  const { t } = useTranslation()
  // ``null`` = closed; otherwise the point to bring into view.
  const [open, setOpen] = useState<string | null>(null)

  return (
    <section
      aria-label={t('chat.guestAdv.stripLabel')}
      className={collapsed ? 'guest-adv guest-adv-collapsed' : 'guest-adv'}
    >
      <div className="guest-adv-head">
        <h2>
          {t('chat.guestAdv.title')}
          <em>{t('chat.guestAdv.titleEm')}</em>
        </h2>
        <button
          className="link-button guest-adv-more"
          onClick={() => setOpen(GUEST_ADVANTAGES[0].key)}
          type="button"
        >
          {t('chat.guestAdv.more')}
          <ChevronRight size={14} />
        </button>
      </div>
      {!collapsed && (
        <ul className="guest-adv-chips">
          {GUEST_ADVANTAGES.map(({ key, Icon }) => (
            <li key={key}>
              <button
                className="guest-adv-chip"
                onClick={() => setOpen(key)}
                type="button"
              >
                <Icon size={14} />
                <span>{t(`chat.guestAdv.items.${key}.chip`)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {open !== null && <GuestAdvantagesDrawer focusKey={open} onClose={() => setOpen(null)} />}
    </section>
  )
}

function GuestAdvantagesDrawer({
  focusKey,
  onClose,
}: {
  focusKey: string
  onClose: () => void
}) {
  const { t } = useTranslation()
  const { openAuth } = useAuthModal()
  const focusRef = useRef<HTMLLIElement | null>(null)

  useEffect(() => {
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [onClose])

  useEffect(() => {
    // Bring the chosen point into view (jsdom has no scrollIntoView).
    focusRef.current?.scrollIntoView?.({ block: 'start' })
  }, [focusKey])

  return createPortal(
    <div className="doc-drawer-backdrop" onClick={onClose} role="presentation">
      <aside
        aria-label={t('chat.guestAdv.drawerTitle')}
        aria-modal="true"
        className="doc-drawer guest-adv-drawer"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
      >
        <header className="doc-drawer-head">
          <div className="doc-drawer-title">
            <ShieldCheck size={16} />
            <div>
              <strong>{t('chat.guestAdv.drawerTitle')}</strong>
            </div>
          </div>
          <button
            aria-label={t('chat.guestAdv.close')}
            autoFocus
            className="icon-button"
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <div className="doc-drawer-body doc-drawer-body-top">
          <p className="guest-adv-lead">{t('chat.guestAdv.drawerLead')}</p>
          <ol className="guest-adv-list">
            {GUEST_ADVANTAGES.map(({ key, Icon }) => (
              <li key={key} ref={key === focusKey ? focusRef : undefined}>
                <span aria-hidden="true" className="guest-adv-icon">
                  <Icon size={16} />
                </span>
                <div>
                  <h3>{t(`chat.guestAdv.items.${key}.title`)}</h3>
                  <p>{t(`chat.guestAdv.items.${key}.body`)}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
        <footer className="doc-drawer-foot">
          <button
            className="primary-button"
            onClick={() => {
              onClose()
              openAuth()
            }}
            type="button"
          >
            <LogIn size={15} />
            {t('chat.guestAdv.cta')}
          </button>
        </footer>
      </aside>
    </div>,
    document.body,
  )
}
