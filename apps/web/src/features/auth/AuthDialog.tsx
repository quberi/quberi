import { X } from 'lucide-react'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'

import { SignIn } from './SignIn'

interface Props {
  open: boolean
  onClose: () => void
  /** An error to show when the dialog opens (e.g. a failed Google redirect). */
  initialError?: string | null
}

/**
 * Modal wrapper around the sign-in / sign-up flow. Opened from the username
 * slot so a guest can log in or register without leaving the page they're
 * browsing. Closes itself on successful authentication.
 */
export function AuthDialog({ open, onClose, initialError = null }: Props) {
  const { t } = useTranslation()

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="auth-modal-backdrop" onClick={onClose} role="presentation">
      <div
        className="auth-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <button
          className="auth-modal-close"
          onClick={onClose}
          type="button"
          aria-label={t('common.close')}
        >
          <X size={18} />
        </button>
        <SignIn embedded initialError={initialError} onAuthenticated={onClose} />
      </div>
    </div>
  )
}
